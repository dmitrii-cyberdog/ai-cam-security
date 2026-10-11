import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.runtime import Runtime, check_readiness
from app.services.models import VisionFault
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

    async def asyncTearDown(self):
        await self.runtime._stop_audio()

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
        self.assertEqual(self.events, [])

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
            ["activated", "alarm_photo", "deactivated"],
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
