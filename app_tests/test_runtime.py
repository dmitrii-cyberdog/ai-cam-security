import asyncio
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.runtime import Runtime, check_readiness
from app.services.alarm import AlarmController, PlayKey
from app.services.models import VisionFault
from app.services.notifications import prepare_notification
from app_tests.test_alarm import DAY, config, observation

#
#


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = Runtime(config())
        self.runtime.audio = Mock(
            validate=Mock(), stop=AsyncMock(), play=AsyncMock(), set_volume=AsyncMock()
        )
        self.runtime.vision = Mock(poll=Mock(return_value=None), stop=AsyncMock())
        self.events = []
        self.runtime.notifications.enqueue = self.events.append
        self.runtime._camera_health = "healthy"
        self.runtime._detector_health = "healthy"
        self.runtime._last_observation = time.monotonic()
        self.readiness = patch("app.runtime.check_readiness", return_value=[])
        self.readiness.start()
        self.addCleanup(self.readiness.stop)

        for target, value in (("app.runtime.resolve_sequence", {}), ("app.runtime.shutil.which", "available")):
            boundary = patch(target, return_value=value)
            boundary.start()
            self.addCleanup(boundary.stop)

    async def asyncTearDown(self):
        await self.runtime._stop_audio()

    async def test_audio_failure_notification_includes_its_redacted_backend_reason(self):
        self.runtime.config["telegram"]["token"] = "123:secret-token"
        self.runtime.audio.play.side_effect = ValueError(
            "audio_exit_1; device=default; key=alarm; asset=common/alarm.wav: "
            + "x" * 1000
            + " Device or resource busy <PCM> & 123%3Asecret-token"
        )

        await self.runtime.execute("alarm")
        await self.runtime._audio_task

        self.runtime._record_audio_failure("volume", ValueError("a later unrelated failure"))
        await self.runtime._step()

        event = next(event for event in self.events if event.reason == "audio_fault")
        note = prepare_notification(event, 2)

        self.assertIn("Reason: Audio playback failed", note.text)
        self.assertIn("audio_exit_1; device=default", note.text)
        self.assertIn("Device or resource busy &lt;PCM&gt; &amp;", note.text)
        self.assertIn("[REDACTED]", note.text)
        self.assertNotIn("secret-token", note.text)
        self.assertNotIn("later unrelated", note.text)

    async def test_stale_audio_failure_does_not_notify_a_new_alarm(self):
        self.runtime.audio.play.side_effect = ValueError("old playback failed")

        await self.runtime.execute("alarm")
        await self.runtime._audio_task
        await self.runtime.execute("stop")
        self.events.clear()
        self.runtime.audio.play.side_effect = None

        await self.runtime.execute("alarm")
        await self.runtime._step()

        self.assertFalse(any(event.reason == "audio_fault" for event in self.events))

    async def test_playback_failure_is_logged_and_retained(self):
        self.runtime.controller.command("alarm", 0, DAY)
        self.runtime.audio.play.side_effect = ValueError("audio_exit_1: Device busy")
        generation = self.runtime.controller.generation

        with self.assertLogs("ai-cam-security", level="ERROR") as records:
            await self.runtime._play(PlayKey(1, generation, "alarm", True))

        self.assertEqual(len(records.records), 1)
        self.assertEqual(records.records[0].context["generation"], generation)
        failure = self.runtime.status()["lastAudioFailure"]
        self.assertEqual(failure["operation"], "playback")
        self.assertIn("Device busy", failure["reason"])
        self.assertTrue(failure["atUtc"].endswith("Z"))
        self.assertEqual(self.runtime.status()["audio"], "audio_fault")

    async def test_stale_playback_failure_cannot_fault_current_audio(self):
        self.runtime.controller.command("alarm", 0, DAY)
        self.runtime.audio.play.side_effect = ValueError("old operation")

        await self.runtime._play(PlayKey(1, self.runtime.controller.generation - 1, "alarm", True))

        self.assertIsNone(self.runtime.status()["lastAudioFailure"])
        self.assertEqual(self.runtime.status()["audio"], "unchecked")

    async def test_volume_failure_is_logged_and_retained_after_recovery(self):
        self.runtime.audio.set_volume.side_effect = ValueError("Audio volume command failed: PCM missing")

        with self.assertLogs("ai-cam-security", level="ERROR"):
            await self.runtime.execute("arm")

        self.runtime.audio.set_volume.side_effect = None
        await self.runtime.execute("arm")

        self.assertIn("PCM missing", self.runtime.status()["lastAudioFailure"]["reason"])
        self.assertEqual(self.runtime.status()["lastAudioFailure"]["operation"], "volume")

    async def test_superseded_mixer_failure_cannot_fault_new_manual_alarm(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def volume():
            entered.set()
            await release.wait()
            raise ValueError("old mixer failure")

        self.runtime.audio.set_volume.side_effect = volume
        arm = asyncio.create_task(self.runtime.execute("arm"))
        await entered.wait()
        await self.runtime.execute("alarm")
        release.set()

        self.assertEqual(await arm, "Command superseded")
        self.assertIsNone(self.runtime.status()["lastAudioFailure"])
        self.assertEqual(self.runtime.status()["audio"], "ready")

    async def test_superseded_validation_failure_cannot_fault_new_alarm(self):
        entered, release = threading.Event(), threading.Event()

        def validate():
            entered.set()
            release.wait(2)
            raise ValueError("old validation failure")

        self.runtime.audio.validate.side_effect = validate
        arm = asyncio.create_task(self.runtime.execute("arm"))

        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.runtime.audio.validate.side_effect = None
            await self.runtime.execute("alarm")
        finally:
            release.set()

        self.assertEqual(await arm, "Command superseded")
        self.assertIsNone(self.runtime.status()["lastAudioFailure"])
        self.assertEqual(self.runtime.status()["audio"], "ready")

    async def test_validation_failure_after_volume_refuses_arm_with_diagnostics(self):
        self.runtime.audio.validate.side_effect = [None, ValueError("asset unreadable after mixer")]

        with self.assertLogs("ai-cam-security", level="ERROR"):
            result = await self.runtime.execute("arm")

        self.assertEqual(result, "Cannot arm: asset unreadable after mixer")
        self.assertFalse(self.runtime.status()["armed"])
        self.assertEqual(self.runtime.status()["audio"], "unavailable")
        self.assertEqual(self.runtime.status()["lastAudioFailure"]["operation"], "validation")

    async def test_manual_alarm_readiness_never_checks_camera_or_model(self):
        self.runtime._last_observation = None
        self.runtime._camera_health = self.runtime._detector_health = "unavailable"
        self.runtime.config["audio"]["volume"] = 25
        entered = asyncio.Event()

        async def play(key, **kwargs):
            kwargs["on_started"]()
            entered.set()
            await asyncio.Event().wait()

        self.runtime.audio.play.side_effect = play

        with (
            patch("app.runtime.check_readiness", side_effect=AssertionError("full readiness must not run")),
            patch("app.runtime.resolve_sequence"),
            patch("app.runtime.shutil.which", side_effect=lambda name: "aplay" if name == "aplay" else None),
        ):
            self.assertEqual(await self.runtime.execute("alarm"), "alarm")
            await asyncio.wait_for(entered.wait(), 1)
            task = self.runtime._audio_task

            for reason in ("camera_unavailable", "detector_unavailable"):
                self.runtime.vision.poll.return_value = VisionFault(reason, time.monotonic())
                await self.runtime._step()

            self.assertIs(self.runtime._audio_task, task)
            self.assertFalse(task.done())
            self.assertEqual([event.kind for event in self.events].count("activated"), 1)
            self.assertTrue(self.runtime.controller.manual)
            self.runtime.audio.set_volume.assert_not_awaited()

            await self.runtime.execute("stop")

        self.assertTrue(task.cancelled())

    async def test_manual_alarm_rejects_missing_aplay_even_without_camera(self):
        self.runtime._last_observation = None

        with patch("app.runtime.resolve_sequence"), patch("app.runtime.shutil.which", return_value=None):
            result = await self.runtime.execute("alarm")

        self.assertIn("Cannot alarm: Audio backend unavailable: aplay", result)
        self.runtime.audio.play.assert_not_awaited()

    async def test_status_uses_monotonic_uptime_and_keeps_stale_frame_age(self):
        self.runtime._started_at = 100
        self.runtime._last_observation = None

        with patch("app.runtime.time", monotonic=Mock(return_value=110)):
            self.assertEqual(self.runtime.status()["system"]["uptimeSeconds"], 10)
            self.assertIsNone(self.runtime.status()["system"]["lastFrameAgeSeconds"])

            self.runtime._last_observation = 101
            self.runtime.vision.poll.return_value = VisionFault("camera_unavailable", 110)
            await self.runtime._step()

            self.assertEqual(self.runtime.status()["system"]["lastFrameAgeSeconds"], 9)
            self.assertEqual(self.runtime.status()["camera"], "unavailable")

    async def test_slow_metrics_sample_does_not_block_stop_or_overlap(self):
        entered, release = threading.Event(), threading.Event()
        stop = asyncio.Event()
        calls = []

        def sample():
            calls.append(True)
            entered.set()
            release.wait(3)

            return {"cpuTemperatureCelsius": 55, "loadAverage": None, "memoryUsedMiB": None, "memoryTotalMiB": None}

        async def background(event):
            await event.wait()

        self.runtime.telegram.run = self.runtime.notifications.run = background

        with patch("app.runtime.read_system_health", side_effect=sample):
            task = asyncio.create_task(self.runtime.run(stop))

            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertIsNone(self.runtime.status()["system"]["cpuTemperatureCelsius"])
                self.assertEqual(await asyncio.wait_for(self.runtime.execute("stop"), 0.5), "stop")
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                stop.set()
                await asyncio.wait_for(task, 2)

        self.assertTrue(task.done())

    async def test_failed_metrics_sample_replaces_previous_values(self):
        loop = asyncio.get_running_loop()
        readings = (
            {"cpuTemperatureCelsius": 55, "loadAverage": (0, 0, 0), "memoryUsedMiB": 100, "memoryTotalMiB": 1000},
            {"cpuTemperatureCelsius": None, "loadAverage": None, "memoryUsedMiB": None, "memoryTotalMiB": None},
        )

        for reading in readings:
            stop = asyncio.Event()

            def sample():
                loop.call_soon_threadsafe(stop.set)

                return reading

            with patch("app.runtime.read_system_health", side_effect=sample):
                await asyncio.wait_for(self.runtime._sample_system_health(stop), 1)

        self.assertIsNone(self.runtime.status()["system"]["cpuTemperatureCelsius"])
        self.assertIsNone(self.runtime.status()["system"]["memoryUsedMiB"])
        self.assertFalse(self.runtime.status()["armed"])

    async def test_manual_alarm_limit_with_no_camera_and_audio_failure_stop(self):
        async def play(key, **kwargs):
            kwargs["on_started"]()
            await asyncio.Event().wait()

        self.runtime.audio.play.side_effect = play
        self.runtime._last_observation = None

        for ending in ("deadline", "disarm", "failed"):
            with self.subTest(ending=ending):
                with patch("app.runtime.time", monotonic=Mock(return_value=100)):
                    await self.runtime.execute("alarm")
                    await asyncio.sleep(0)
                    await self.runtime._step()

                task = self.runtime._audio_task

                if ending == "deadline":
                    with patch("app.runtime.time", monotonic=Mock(return_value=160)):
                        await self.runtime._step()
                elif ending == "disarm":
                    await self.runtime.execute("disarm")
                else:
                    self.runtime._audio_callback(self.runtime.controller.generation, "failed")
                    await self.runtime._step()

                self.assertTrue(task.cancelled())
                self.assertEqual(self.runtime.status()["phase"], "idle")

    async def test_failure_redacts_secrets_before_summary_truncation(self):
        self.runtime.config["telegram"]["token"] = "123:secret-token"
        self.runtime.controller.command("alarm", 0, DAY)
        self.runtime.audio.play.side_effect = ValueError("x" * 290 + "123%3Asecret-token <PCM>")

        with self.assertLogs("ai-cam-security", level="ERROR") as records:
            await self.runtime._play(PlayKey(1, self.runtime.controller.generation, "alarm", True))

        self.assertNotIn("secret-token", records.records[0].context["reason"])
        self.assertNotIn("123%3A", self.runtime.status()["lastAudioFailure"]["reason"])
        self.assertLessEqual(len(self.runtime.status()["lastAudioFailure"]["reason"]), 300)

    async def feed(self, at, number, people=1):
        await self.runtime._apply(
            self.runtime.controller.observe(observation(number, at, people), at, DAY)
        )
        await asyncio.sleep(0)

    async def confirm(self):
        await self.runtime.execute("arm")
        for number, at in enumerate((0, 0.5, 1, 1.5, 2), 1):
            await self.feed(at, number)

    async def test_photo_waits_for_new_fresh_observation_while_disarmed(self):
        self.runtime.vision.poll.return_value = observation(1, 99, 0)

        with patch("app.runtime.time", monotonic=Mock(return_value=100)):
            await self.runtime._step()
            task = asyncio.create_task(self.runtime.get_frame())
            await asyncio.sleep(0)

        self.assertFalse(task.done())
        self.runtime.vision.poll.return_value = observation(2, 100.1, 0)

        with patch("app.runtime.time", monotonic=Mock(return_value=103)):
            await self.runtime._step()
            await asyncio.sleep(0.06)

        self.assertFalse(task.done())
        fresh = observation(3, 103.1, 0)
        self.runtime.vision.poll.return_value = fresh

        with patch("app.runtime.time", monotonic=Mock(return_value=103.1)):
            await self.runtime._step()
            frame = await asyncio.wait_for(task, 0.5)

        self.assertIs(frame, fresh.frame)
        self.assertFalse(self.runtime.status()["armed"])
        self.runtime.audio.play.assert_not_awaited()
        self.assertEqual(
            [event.kind for event in self.events],
            ["camera_unavailable", "camera_available"],
        )

    async def test_photo_frame_cleared_on_vision_fault_and_shutdown_stops_wait(self):
        self.runtime.vision.poll.return_value = observation(1, 100, 0)

        with patch("app.runtime.time", monotonic=Mock(return_value=100)):
            await self.runtime._step()

        self.runtime.vision.poll.return_value = VisionFault("camera_unavailable", 101)

        with patch("app.runtime.time", monotonic=Mock(return_value=101)):
            await self.runtime._step()

        self.assertIsNone(self.runtime._latest_frame)
        self.runtime._closing = True

        self.assertIsNone(await self.runtime.get_frame())

    async def test_arm_applies_volume_before_arming(self):
        async def set_volume():
            self.assertFalse(self.runtime.status()["armed"])

        self.runtime.audio.set_volume.side_effect = set_volume

        self.assertEqual(await self.runtime.execute("arm"), "arm")
        self.runtime.audio.set_volume.assert_awaited_once()
        self.assertTrue(self.runtime.status()["armed"])

        self.runtime.audio.set_volume.side_effect = None

        await self.runtime.execute("arm")
        self.assertEqual(self.runtime.audio.set_volume.await_count, 2)

    async def test_camera_loss_and_recovery_notify_once_while_disarmed(self):
        for at, message in (
            (100, observation(1, 100, 0)),
            (101, VisionFault("camera_unavailable", 101)),
            (101.1, VisionFault("camera_unavailable", 101.1)),
            (102, observation(2, 99, 0)),
            (103, observation(3, 103, 0)),
            (104, observation(4, 104, 0)),
        ):
            self.runtime.vision.poll.return_value = message

            with patch("app.runtime.time", monotonic=Mock(return_value=at)):
                await self.runtime._step()

        self.assertEqual(
            [event.kind for event in self.events],
            ["camera_unavailable", "camera_available"],
        )
        self.assertEqual([event.at for event in self.events], [101, 103])
        self.assertTrue(all(event.frame is None for event in self.events))
        self.assertFalse(self.runtime.status()["armed"])
        self.runtime.audio.play.assert_not_awaited()

    async def test_initial_camera_failure_notifies_after_startup_budget(self):
        self.runtime._camera_health = "unavailable"
        self.runtime._last_observation = None
        self.runtime._camera_check_started_at = 100
        deadline = 115 + (
            self.runtime.config["camera"]["openTimeoutMs"]
            + self.runtime.config["camera"]["readTimeoutMs"]
        ) / 1000

        for at in (100, deadline - 0.1):
            self.runtime.vision.poll.return_value = VisionFault("camera_unavailable", at)

            with patch("app.runtime.time", monotonic=Mock(return_value=at)):
                await self.runtime._step()

        self.assertEqual(self.events, [])
        self.runtime.vision.poll.return_value = None

        for at in (deadline, deadline + 1):
            with patch("app.runtime.time", monotonic=Mock(return_value=at)):
                await self.runtime._step()

        self.assertEqual([event.kind for event in self.events], ["camera_unavailable"])
        self.runtime.vision.poll.return_value = observation(1, deadline + 2, 0)

        with patch("app.runtime.time", monotonic=Mock(return_value=deadline + 2)):
            await self.runtime._step()

        self.assertEqual(
            [event.kind for event in self.events],
            ["camera_unavailable", "camera_available"],
        )

    async def test_stale_camera_notifies_and_fresh_poll_avoids_transient_loss(self):
        for at, message in (
            (100, observation(1, 100, 0)),
            (103, observation(2, 103, 0)),
        ):
            self.runtime.vision.poll.return_value = message

            with patch("app.runtime.time", monotonic=Mock(return_value=at)):
                await self.runtime._step()

        self.assertEqual(self.events, [])

        for at, message in (
            (106, None),
            (106.1, observation(3, 103, 0)),
            (107, observation(4, 107, 0)),
        ):
            self.runtime.vision.poll.return_value = message

            with patch("app.runtime.time", monotonic=Mock(return_value=at)):
                await self.runtime._step()

        self.assertEqual(
            [event.kind for event in self.events],
            ["camera_unavailable", "camera_available"],
        )
        self.assertEqual([event.at for event in self.events], [106, 107])

    async def test_camera_notices_preserve_armed_mode_and_audio_fault_suppression(self):
        self.runtime.controller.command("arm", 0, DAY)
        self.runtime.controller.suppression = "audio_fault"

        for at, message in (
            (100, observation(1, 100, 0)),
            (101, VisionFault("camera_unavailable", 101)),
            (102, observation(2, 102, 0)),
        ):
            self.runtime.vision.poll.return_value = message

            with patch("app.runtime.time", monotonic=Mock(return_value=at)):
                await self.runtime._step()

        self.assertTrue(self.runtime.status()["armed"])
        self.assertEqual(self.runtime.status()["suppressionReason"], "audio_fault")
        self.assertEqual(
            [event.kind for event in self.events],
            ["camera_unavailable", "camera_available"],
        )
        self.runtime.audio.play.assert_not_awaited()

    async def test_camera_loss_during_volume_application_prevents_arm(self):
        async def set_volume():
            self.runtime._camera_health = "unavailable"

        self.runtime.audio.set_volume.side_effect = set_volume

        self.assertIn(
            "Cannot arm: Camera/detector needs fresh observations",
            await self.runtime.execute("arm"),
        )
        self.assertFalse(self.runtime.status()["armed"])

    async def test_volume_failure_prevents_arm_and_preserves_suppression(self):
        self.runtime.controller.suppression = "audio_fault"
        self.runtime.audio.set_volume.side_effect = ValueError("Audio volume failed")

        self.assertEqual(
            await self.runtime.execute("arm"), "Cannot arm: Audio volume failed"
        )
        self.assertFalse(self.runtime.status()["armed"])
        self.assertEqual(self.runtime.status()["suppressionReason"], "audio_fault")
        self.assertEqual(self.runtime.status()["audio"], "audio_fault")

        self.runtime.audio.set_volume.side_effect = None

        self.assertEqual(await self.runtime.execute("arm"), "arm")
        self.assertTrue(self.runtime.status()["armed"])
        self.assertFalse(self.runtime.status()["suppression"])

    async def test_failed_readiness_does_not_change_volume(self):
        self.runtime._camera_health = "unavailable"

        self.assertIn("Cannot arm", await self.runtime.execute("arm"))
        self.runtime.audio.set_volume.assert_not_awaited()

    async def test_other_commands_do_not_change_volume(self):
        for command in ("alarm", "stop", "disarm", "status"):
            await self.runtime.execute(command)

        self.runtime.audio.set_volume.assert_not_awaited()

    async def test_disarm_supersedes_slow_volume_application(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def set_volume():
            entered.set()
            await release.wait()

        self.runtime.audio.set_volume.side_effect = set_volume
        arm = asyncio.create_task(self.runtime.execute("arm"))

        await entered.wait()
        await self.runtime.execute("disarm")
        release.set()

        self.assertEqual(await arm, "Command superseded")
        self.assertFalse(self.runtime.status()["armed"])

    async def test_complete_fake_incident(self):
        await self.confirm()
        for _ in range(4):
            await self.runtime._apply(
                self.runtime.controller.audio_event(
                    self.runtime.controller.generation, "finished", 2, DAY
                )
            )
            await asyncio.sleep(0)
        await self.runtime._apply(
            self.runtime.controller.audio_event(
                self.runtime.controller.generation, "started", 2, DAY
            )
        )
        for number, at in enumerate((2.5, 3, 3.5, 4, 4.5), 6):
            await self.feed(at, number, 0)

        self.assertEqual(
            [event.kind for event in self.events],
            ["recognized", "still_there", "activated"],
        )

        for number, at in enumerate((60, 60.5, 61, 61.5, 62), 11):
            await self.feed(at, number, 0)

        await self.runtime._apply(self.runtime.controller.tick(62, DAY))

        self.assertEqual(
            [event.kind for event in self.events],
            ["recognized", "still_there", "activated", "deactivated"],
        )
        self.assertEqual(
            [call.args[0] for call in self.runtime.audio.play.call_args_list],
            ["first_warning", "countdown_1", "countdown_2", "countdown_3", "alarm"],
        )

    async def test_early_departure_and_late_finish(self):
        await self.confirm()
        old = self.runtime.controller.generation
        await self.runtime.execute("disarm")
        await self.runtime._apply(
            self.runtime.controller.audio_event(old, "finished", 3, DAY)
        )
        self.assertEqual(
            [event.kind for event in self.events], ["recognized", "deactivated"]
        )
        self.assertFalse(self.runtime.status()["armed"])
        self.assertEqual(self.runtime.audio.play.await_count, 1)

    async def test_stop_cancels_siren_and_allows_another_incident(self):
        async def play(key, **kwargs):
            if key == "alarm":
                await asyncio.Event().wait()

        self.runtime.audio.play.side_effect = play
        self.runtime.controller.audio["maxAlarmCycles"] = 1

        await self.confirm()

        for _ in range(4):
            await self.runtime._apply(
                self.runtime.controller.audio_event(
                    self.runtime.controller.generation, "finished", 2, DAY
                )
            )
            await asyncio.sleep(0)

        await self.runtime._apply(
            self.runtime.controller.audio_event(
                self.runtime.controller.generation, "started", 2, DAY
            )
        )
        task = self.runtime._audio_task
        generation = self.runtime.controller.generation

        self.assertEqual(await self.runtime.execute("stop"), "stop")
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.runtime._audio_task)
        self.assertTrue(self.runtime.status()["armed"])
        self.assertFalse(self.runtime.status()["suppression"])

        await self.runtime._apply(
            self.runtime.controller.audio_event(generation, "finished", 2.1, DAY)
        )

        for number, at in enumerate((2.5, 3, 3.5, 4), 6):
            await self.feed(at, number)

        self.assertEqual([event.kind for event in self.events].count("recognized"), 1)

        await self.feed(4.5, 10)

        self.assertEqual(
            [event.kind for event in self.events],
            ["recognized", "still_there", "activated", "deactivated", "recognized"],
        )
        self.assertEqual(self.events[-2].reason, "stop")
        self.assertEqual(self.runtime.audio.play.call_args.args[0], "first_warning")

    async def test_repeated_period_keeps_task_until_departure_deadline(self):
        async def play(key, **kwargs):
            if key == "alarm":
                await asyncio.Event().wait()

        self.runtime.audio.play.side_effect = play
        await self.confirm()

        for _ in range(4):
            await self.runtime._apply(
                self.runtime.controller.audio_event(
                    self.runtime.controller.generation, "finished", 2, DAY
                )
            )
            await asyncio.sleep(0)

        await self.runtime._apply(
            self.runtime.controller.audio_event(
                self.runtime.controller.generation, "started", 2, DAY
            )
        )
        task = self.runtime._audio_task
        stops = self.runtime.audio.stop.await_count

        await self.feed(62, 6)
        await self.runtime._apply(self.runtime.controller.tick(62, DAY))

        for number, half_second in enumerate(range(125, 244), 7):
            at = half_second / 2
            await self.feed(at, number, 0)
            await self.runtime._apply(self.runtime.controller.tick(at, DAY))

        self.assertIs(self.runtime._audio_task, task)
        self.assertFalse(task.done())
        self.assertEqual(self.runtime.audio.stop.await_count, stops)
        self.assertEqual(self.runtime.audio.play.await_count, 5)
        self.assertEqual([event.kind for event in self.events].count("activated"), 1)

        await self.feed(122, 126, 0)
        await self.runtime._apply(self.runtime.controller.tick(122, DAY))

        self.assertIsNone(self.runtime._audio_task)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.events[-1].reason, "person_left")

    async def test_siren_deadline_uses_latest_observation(self):
        for people, negative_times, phase, cycles, reasons in (
            (0, (60, 60.5, 61, 61.5), "idle", 0, ["person_left"]),
            (1, (59.5, 60, 60.5, 61, 61.5), "alarm", 2, []),
        ):
            with self.subTest(people=people):
                controller = AlarmController(config())
                self.runtime.controller = controller
                self.events.clear()
                controller.command("arm", 0, DAY)

                for number, at in enumerate((0, 0.5, 1, 1.5, 2), 1):
                    controller.observe(observation(number, at), at, DAY)

                for _ in range(4):
                    controller.audio_event(controller.generation, "finished", 2, DAY)

                controller.audio_event(controller.generation, "started", 2, DAY)

                for number, at in enumerate(negative_times, 6):
                    controller.observe(observation(number, at, 0), at, DAY)

                self.runtime.vision.poll.return_value = observation(
                    6 + len(negative_times), 62, people
                )

                with patch("app.runtime.time", monotonic=Mock(return_value=62)):
                    await self.runtime._step()

                self.assertEqual(controller.phase, phase)
                self.assertEqual(controller.status()["alarmCycles"], cycles)
                self.assertEqual([event.reason for event in self.events], reasons)

    async def test_vision_restart_preserves_armed_mode(self):
        await self.confirm()
        await self.runtime._apply(
            self.runtime.controller.vision_fault("vision_exited", 3, DAY)
        )
        self.assertTrue(self.runtime.status()["armed"])
        self.assertEqual(self.runtime.status()["phase"], "idle")
        for number, at in enumerate((3, 3.5, 4, 4.5), 1):
            await self.feed(at, number)
        self.assertEqual([event.kind for event in self.events].count("recognized"), 1)
        await self.feed(5, 5)
        self.assertEqual([event.kind for event in self.events].count("recognized"), 2)

    async def test_manual_alarm_while_disarmed_and_restart_disarmed(self):
        await self.runtime.execute("alarm")
        await asyncio.sleep(0)
        self.assertEqual(self.runtime.audio.play.call_args.args[0], "alarm")
        self.assertFalse(self.runtime.status()["armed"])
        self.assertFalse(Runtime(config()).status()["armed"])

    async def test_step_enqueues_periodic_photo_without_interrupting_siren(self):
        async def play(key, **kwargs):
            kwargs["on_started"]()
            await asyncio.Event().wait()

        self.runtime.audio.play.side_effect = play
        self.runtime.config["detection"]["onAlarmSendPhotoEveryMillisecs"] = 2000

        self.runtime._last_observation = 100

        with patch("app.runtime.time", monotonic=Mock(return_value=100)):
            await self.runtime.execute("alarm")
            await asyncio.sleep(0)
            await self.runtime._step()

        task = self.runtime._audio_task
        stops = self.runtime.audio.stop.await_count
        self.runtime.vision.poll.return_value = observation(1, 101.9)

        with patch("app.runtime.time", monotonic=Mock(return_value=101.9)):
            await self.runtime._step()

        self.runtime.vision.poll.return_value = None

        with patch("app.runtime.time", monotonic=Mock(return_value=102)):
            await self.runtime._step()

        self.assertEqual(
            [event.kind for event in self.events], ["activated", "alarm_photo"]
        )
        self.assertEqual(self.events[-1].frame.captured_at, 101.9)
        self.assertIs(self.runtime._audio_task, task)
        self.assertFalse(task.done())
        self.assertEqual(self.runtime.audio.stop.await_count, stops)

        with patch("app.runtime.time", monotonic=Mock(return_value=102.1)):
            await self.runtime.execute("stop")

        with patch("app.runtime.time", monotonic=Mock(return_value=104)):
            await self.runtime._step()

        self.assertEqual(
            [event.kind for event in self.events],
            ["activated", "alarm_photo", "deactivated", "camera_unavailable"],
        )

    async def test_unconfigured_telegram_does_not_stop_vision(self):
        self.runtime.config["camera"]["rtspUrl"] = "rtsp://fake"
        self.runtime.config["detection"]["modelPath"] = Path(__file__)
        stop = asyncio.Event()

        async def background(event):
            await event.wait()

        self.runtime.telegram.run = background
        self.runtime.notifications.run = background
        task = asyncio.create_task(self.runtime.run(stop))
        await asyncio.sleep(0.02)
        self.assertTrue(self.runtime.vision.start.called)
        stop.set()
        await asyncio.wait_for(task, 2)
        self.assertTrue(self.runtime.vision.stop.called)

    async def test_disarm_supersedes_slow_arm_readiness(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def ready(name):
            entered.set()
            await release.wait()
            return None

        with patch.object(self.runtime, "_readiness", side_effect=ready):
            arm = asyncio.create_task(self.runtime.execute("arm"))
            await entered.wait()
            await self.runtime.execute("disarm")
            release.set()
            await arm
        self.assertFalse(self.runtime.status()["armed"])

    async def test_concurrent_disarm_keeps_notification_order(self):
        self.runtime.controller.command("arm", 0, DAY)
        for number, at in enumerate((0, 0.5, 1, 1.5), 1):
            self.runtime.controller.observe(observation(number, at), at, DAY)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_stop():
            entered.set()
            await release.wait()

        self.runtime.audio.stop.side_effect = slow_stop
        first = asyncio.create_task(
            self.runtime._apply(
                self.runtime.controller.observe(observation(5, 2), 2, DAY)
            )
        )
        await entered.wait()
        stop = asyncio.create_task(self.runtime.execute("disarm"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, stop)
        self.assertEqual(
            [event.kind for event in self.events], ["recognized", "deactivated"]
        )
        self.runtime.audio.play.assert_not_awaited()

    async def test_manual_override_keeps_runtime_playback_task(self):
        self.runtime.controller.command("arm", 0, DAY)
        await self.confirm()
        for _ in range(4):
            await self.runtime._apply(
                self.runtime.controller.audio_event(
                    self.runtime.controller.generation, "finished", 2, DAY
                )
            )
            await asyncio.sleep(0)
        await self.runtime._apply(
            self.runtime.controller.audio_event(
                self.runtime.controller.generation, "started", 2, DAY
            )
        )
        task = self.runtime._audio_task
        count = self.runtime.audio.play.await_count
        await self.runtime.execute("alarm")
        self.assertIs(self.runtime._audio_task, task)
        self.assertEqual(self.runtime.audio.play.await_count, count)
        self.assertEqual([event.kind for event in self.events].count("activated"), 1)

    async def test_audio_fault_persists_through_recovery_and_absence(self):
        await self.confirm()
        await self.runtime._apply(
            self.runtime.controller.audio_event(
                self.runtime.controller.generation, "failed", 2, DAY
            )
        )
        await self.runtime._apply(
            self.runtime.controller.vision_fault("vision_exited", 3, DAY)
        )
        for number, at in enumerate((3, 3.5, 4, 4.5, 5), 1):
            await self.feed(at, number, 0)
        self.assertEqual(self.runtime.status()["suppressionReason"], "audio_fault")
        await self.runtime.execute("arm")
        self.assertFalse(self.runtime.status()["suppression"])


class ReadinessTests(unittest.TestCase):
    def test_configured_volume_requires_amixer_without_running_it(self):
        settings = config()
        settings["audio"]["volume"] = 75

        with patch(
            "app.runtime.shutil.which",
            side_effect=lambda name: None if name == "amixer" else "available",
        ):
            self.assertIn(
                "Volume backend unavailable: amixer", check_readiness(settings)
            )

        settings["audio"]["volume"] = None

        with patch(
            "app.runtime.shutil.which",
            side_effect=lambda name: None if name == "amixer" else "available",
        ):
            self.assertNotIn(
                "Volume backend unavailable: amixer", check_readiness(settings)
            )

    def test_missing_assets_are_named_without_secrets(self):
        settings = config()
        settings["camera"]["password"] = "hidden-secret"
        errors = check_readiness(settings)
        self.assertTrue(any("DETECTION_MODEL_PATH" in error for error in errors))
        self.assertTrue(any("RTSP_HOST" in error for error in errors))
        self.assertNotIn("hidden-secret", str(errors))
