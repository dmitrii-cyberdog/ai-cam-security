import asyncio
import sys
import tempfile
import unittest
import wave
from pathlib import Path, PureWindowsPath
from unittest.mock import AsyncMock, Mock, patch

from app.services.audio import AudioPlayer, resolve_sequence

#
#


def wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as file:
        file.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        file.writeframes(b"\x00\x00" * 16)


class AudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.settings = {"soundsFolder": self.root, "mainLanguage": "cz", "languages": ["en", "cz"], "sequence": ["first_warning", "countdown_1", "countdown_2", "countdown_3", "alarm"], "device": "default", "volume": None, "maxAlarmSeconds": 60}
        self.config = {"audio": self.settings}
        for key in self.settings["sequence"][:-1]:
            for language in ("cz", "en"):
                wav(self.root / language / f"{key}.wav")
        wav(self.root / "common/alarm.wav")

    def flattened(self):
        return [str(path.relative_to(self.root)).replace("\\", "/") for paths in resolve_sequence(self.config).values() for path in paths]

    async def test_playback_failure_preserves_bounded_alsa_stderr(self):
        spawn = asyncio.create_subprocess_exec
        started = []

        async def backend(*args, **kwargs):
            return await spawn(
                sys.executable, "-c",
                "import sys; sys.stderr.buffer.write(b'x' * 200000 + b'\\xff Device or resource busy'); sys.exit(7)",
                **kwargs,
            )

        with patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=backend):
            with self.assertRaises(ValueError) as failure:
                await asyncio.wait_for(AudioPlayer(self.config).play("alarm", on_started=lambda: started.append(True)), 3)

        reason = str(failure.exception)
        self.assertIn("audio_exit_7", reason)
        self.assertIn("Device or resource busy", reason)
        self.assertIn("default", reason)
        self.assertIn("common/alarm.wav", reason)
        self.assertLess(len(reason), 4400)
        self.assertEqual(started, [])

    async def test_spawn_failure_preserves_os_reason(self):
        with patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=PermissionError("speaker access denied")):
            with self.assertRaisesRegex(ValueError, "audio_spawn_failed.*speaker access denied"):
                await AudioPlayer(self.config).play("alarm")

    async def test_windows_asset_diagnostics_use_portable_path(self):
        root = PureWindowsPath("C:/sounds")
        asset = root / "common" / "alarm.wav"
        process = Mock(returncode=1, stderr=None, wait=AsyncMock(return_value=1))

        with (
            patch("app.services.audio.resolve_sequence", return_value={"alarm": (asset,)}),
            patch("app.services.audio.Path") as path_type,
            patch("app.services.audio.asyncio.create_subprocess_exec", return_value=process) as spawn,
        ):
            path_type.return_value.resolve.return_value = root

            with self.assertRaises(ValueError) as failure:
                await AudioPlayer(self.config).play("alarm")

        self.assertIn("asset=common/alarm.wav", str(failure.exception))
        self.assertEqual(spawn.call_args.args[-1], r"C:\sounds\common\alarm.wav")

    async def test_mixer_stderr_reports_missing_pcm(self):
        self.settings["volume"] = 25
        spawn = asyncio.create_subprocess_exec

        async def backend(*args, **kwargs):
            return await spawn(sys.executable, "-c", "import sys; sys.stderr.write('Unable to find simple control PCM'); sys.exit(1)", **kwargs)

        with patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=backend):
            with self.assertRaisesRegex(ValueError, "Audio volume command failed.*PCM"):
                await AudioPlayer(self.config).set_volume()

    async def test_stop_drains_verbose_playback_without_failure(self):
        spawn = asyncio.create_subprocess_exec
        started = asyncio.Event()
        processes = []

        async def backend(*args, **kwargs):
            process = await spawn(sys.executable, "-c", "import os;\nwhile True: os.write(2, b'x' * 8192)", **kwargs)
            processes.append(process)

            return process

        player = AudioPlayer(self.config)

        with patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=backend):
            task = asyncio.create_task(player.play("alarm", repeat=True, on_started=started.set))

            try:
                await asyncio.wait_for(started.wait(), 3)
                await asyncio.wait_for(player.stop(), 2)
                await asyncio.wait_for(task, 2)
            finally:
                await player.stop()
                await asyncio.gather(task, return_exceptions=True)

        self.assertIsNotNone(processes[0].returncode)

    async def test_cancellation_during_spawn_reaps_child_and_drains_stderr(self):
        self.settings["volume"] = 25
        spawn = asyncio.create_subprocess_exec

        for operation in ("volume", "playback"):
            with self.subTest(operation=operation):
                entered, release = asyncio.Event(), asyncio.Event()
                processes = []
                player = AudioPlayer(self.config)

                async def backend(*args, **kwargs):
                    process = await spawn(sys.executable, "-c", "import os, time; os.write(2, b'x' * 200000); time.sleep(10)", **kwargs)
                    processes.append(process)
                    entered.set()
                    await release.wait()

                    return process

                with patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=backend):
                    task = asyncio.create_task(player.set_volume() if operation == "volume" else player.play("alarm"))

                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        await asyncio.sleep(0.05)
                        task.cancel()
                        release.set()

                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 3)

                        self.assertIsNotNone(processes[0].returncode)
                        self.assertTrue(processes[0].stderr.at_eof())
                    finally:
                        release.set()

                        for process in processes:
                            if process.returncode is None:
                                process.kill()

                            await process.stderr.read()
                            await process.wait()

                        await asyncio.gather(task, return_exceptions=True)

    async def test_mixer_timeout_terminates_child_and_reports_reason(self):
        self.settings["volume"] = 25
        spawn = asyncio.create_subprocess_exec
        processes = []

        async def backend(*args, **kwargs):
            process = await spawn(sys.executable, "-c", "import sys, time; sys.stderr.write('mixer stalled'); sys.stderr.flush(); time.sleep(10)", **kwargs)
            processes.append(process)

            return process

        with patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=backend):
            with self.assertRaisesRegex(ValueError, "timed out.*mixer stalled"):
                await asyncio.wait_for(AudioPlayer(self.config).set_volume(), 4)

        self.assertIsNotNone(processes[0].returncode)

    async def test_delayed_playback_failure_keeps_reason_after_started(self):
        spawn = asyncio.create_subprocess_exec
        started = []

        async def backend(*args, **kwargs):
            return await spawn(sys.executable, "-c", "import sys, time; time.sleep(0.2); sys.stderr.write('speaker disconnected'); sys.exit(2)", **kwargs)

        with patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=backend):
            with self.assertRaisesRegex(ValueError, "audio_exit_2.*speaker disconnected"):
                await AudioPlayer(self.config).play("alarm", on_started=lambda: started.append(True))

        self.assertEqual(started, [True])

    async def test_stop_kills_unresponsive_child_and_joins_drain(self):
        done, started = asyncio.Event(), asyncio.Event()
        stream = asyncio.StreamReader()
        stream.feed_data(b"backend busy")

        class Process:
            returncode = None
            stderr = stream

            async def wait(self):
                await done.wait()

                return self.returncode

            def terminate(self):
                pass

            def kill(self):
                self.returncode = -9
                stream.feed_eof()
                done.set()

        process = Process()
        player = AudioPlayer(self.config)

        with patch("app.services.audio.asyncio.create_subprocess_exec", return_value=process):
            task = asyncio.create_task(player.play("alarm", repeat=True, on_started=started.set))

            try:
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.wait_for(player.stop(), 2)
                await asyncio.wait_for(task, 1)
            finally:
                process.kill()
                await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(process.returncode, -9)
        self.assertTrue(stream.at_eof())

    def test_common_once_language_order(self):
        self.assertEqual(self.flattened(), ["cz/first_warning.wav", "en/first_warning.wav", "cz/countdown_1.wav", "en/countdown_1.wav", "cz/countdown_2.wav", "en/countdown_2.wav", "cz/countdown_3.wav", "en/countdown_3.wav", "common/alarm.wav"])

    def test_common_warning_overrides_missing_main(self):
        self.settings["mainLanguage"] = "missing"
        for key in self.settings["sequence"]:
            wav(self.root / "common" / f"{key}.wav")
        self.assertTrue(all(name.startswith("common/") for name in self.flattened()))

    def test_missing_secondary_language_is_skipped(self):
        self.settings["languages"] = ["absent", "cz"]
        self.assertEqual(len(self.flattened()), 5)
        AudioPlayer(self.config).validate()

    def test_missing_main_language_rejects_readiness(self):
        self.settings["mainLanguage"] = "missing"
        with self.assertRaisesRegex(ValueError, "missing/first_warning.wav"):
            AudioPlayer(self.config).validate()

    def test_main_language_first(self):
        self.settings["mainLanguage"] = "en"
        self.assertEqual(self.flattened()[:2], ["en/first_warning.wav", "cz/first_warning.wav"])
        self.settings["languages"] = ["cz", "cz"]
        self.assertEqual(self.flattened()[:2], ["en/first_warning.wav", "cz/first_warning.wav"])

    def test_invalid_wav_and_traversal(self):
        (self.root / "common/alarm.wav").write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "alarm.wav"):
            AudioPlayer(self.config).validate()
        self.settings["sequence"] = ["../escape"]
        with self.assertRaises(ValueError):
            resolve_sequence(self.config)

    def test_symlink_escape_is_rejected(self):
        link = self.root / "outside"
        try:
            link.symlink_to(self.root.parent, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires Windows permission")
        self.settings["mainLanguage"] = "outside"
        with self.assertRaisesRegex(ValueError, "outside sound root"):
            resolve_sequence(self.config)

    async def test_unconfigured_volume_does_not_run_mixer(self):
        with patch("app.services.audio.asyncio.create_subprocess_exec") as spawn:
            await AudioPlayer(self.config).set_volume()

        spawn.assert_not_called()

    async def test_volume_targets_selected_card_pcm_control(self):
        self.settings["device"] = "plughw:CARD=UACDemoV10,DEV=0"
        self.settings["volume"] = 75
        process = Mock(returncode=0, stderr=None, wait=AsyncMock(return_value=0))

        with patch("app.services.audio.asyncio.create_subprocess_exec", return_value=process) as spawn:
            await AudioPlayer(self.config).set_volume()

        self.assertEqual(spawn.call_args.args, ("amixer", "-c", "UACDemoV10", "-q", "sset", "PCM", "playback", "75%"))

    async def test_default_mixer_supports_zero_volume(self):
        self.settings["volume"] = 0
        process = Mock(returncode=0, stderr=None, wait=AsyncMock(return_value=0))

        with patch("app.services.audio.asyncio.create_subprocess_exec", return_value=process) as spawn:
            await AudioPlayer(self.config).set_volume()

        self.assertEqual(spawn.call_args.args, ("amixer", "-q", "sset", "PCM", "playback", "0%"))

    async def test_explicit_volume_overrides_initial_value_without_changing_config(self):
        self.settings["volume"] = 25
        process = Mock(returncode=0, stderr=None, wait=AsyncMock(return_value=0))

        with patch("app.services.audio.asyncio.create_subprocess_exec", return_value=process) as spawn:
            await AudioPlayer(self.config).set_volume(50)

        self.assertEqual(spawn.call_args.args[-1], "50%")
        self.assertEqual(self.settings["volume"], 25)

    async def test_volume_rejects_device_without_card_identity(self):
        self.settings["volume"] = 75
        self.settings["device"] = "custom-output"

        with patch("app.services.audio.asyncio.create_subprocess_exec") as spawn:
            with self.assertRaisesRegex(ValueError, "AUDIO_DEVICE"):
                await AudioPlayer(self.config).set_volume()

        spawn.assert_not_called()

    async def test_mixer_failures_are_reported(self):
        self.settings["volume"] = 75

        for error, message in (
            (FileNotFoundError("amixer missing"), "backend unavailable.*amixer missing"),
            (PermissionError("access denied"), "command failed.*access denied"),
        ):
            with self.subTest(error=error), patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=error):
                with self.assertRaisesRegex(ValueError, message):
                    await AudioPlayer(self.config).set_volume()

    async def test_immediate_failure_never_reports_started(self):
        process = AsyncMock()
        process.stderr = None
        process.returncode = 1
        process.wait.return_value = 1
        calls = []
        with patch("app.services.audio.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaisesRegex(ValueError, "audio_exit_1"):
                await AudioPlayer(self.config).play("alarm", repeat=True, on_started=lambda: calls.append(True))
        self.assertEqual(calls, [])

    async def test_short_alarm_file_repeats_until_stopped(self):
        finished = Mock(returncode=0, stderr=None, wait=AsyncMock(return_value=0))
        playing = asyncio.Event()
        stopped = asyncio.Event()
        active = Mock(returncode=None, stderr=None, wait=AsyncMock())

        async def wait():
            playing.set()
            await stopped.wait()

            return active.returncode

        def terminate():
            active.returncode = -15
            stopped.set()

        active.wait.side_effect = wait
        active.terminate.side_effect = terminate
        calls = []
        player = AudioPlayer(self.config)

        with patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=[finished, finished, active]) as spawn:
            task = asyncio.create_task(player.play("alarm", repeat=True, on_started=lambda: calls.append(True)))

            try:
                await asyncio.wait_for(playing.wait(), 1)
                self.assertEqual(spawn.call_count, 3)
                self.assertTrue(all(call.args[-1] == str(self.root / "common/alarm.wav") for call in spawn.call_args_list))
                self.assertEqual(calls, [True])
            finally:
                await player.stop()
                await asyncio.wait_for(task, 1)

            self.assertEqual(spawn.call_count, 3)

    async def test_stop_prevents_following_files_and_repeats(self):
        class Process:
            stderr = None
            returncode = None
            def __init__(self):
                self.done = asyncio.Event()
            async def wait(self):
                await self.done.wait()
                return self.returncode
            def terminate(self):
                self.returncode = -15
                self.done.set()
            def kill(self):
                self.returncode = -9
                self.done.set()
        player = AudioPlayer(self.config)
        with patch("app.services.audio.asyncio.create_subprocess_exec", side_effect=lambda *a, **k: Process()) as spawn:
            task = asyncio.create_task(player.play("first_warning"))
            await asyncio.sleep(0.01)
            await asyncio.wait_for(player.stop(), 1)
            count = spawn.call_count
            await task
            self.assertEqual(spawn.call_count, count)
            await player.stop()
