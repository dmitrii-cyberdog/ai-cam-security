import asyncio
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
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
        with patch("app.services.audio.subprocess.run") as run:
            await AudioPlayer(self.config).set_volume()

        run.assert_not_called()

    async def test_volume_targets_selected_card_pcm_control(self):
        self.settings["device"] = "plughw:CARD=UACDemoV10,DEV=0"
        self.settings["volume"] = 75

        with patch("app.services.audio.subprocess.run") as run:
            await AudioPlayer(self.config).set_volume()

        run.assert_called_once_with(
            ["amixer", "-c", "UACDemoV10", "-q", "sset", "PCM", "playback", "75%"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )

    async def test_default_mixer_supports_zero_volume(self):
        self.settings["volume"] = 0

        with patch("app.services.audio.subprocess.run") as run:
            await AudioPlayer(self.config).set_volume()

        self.assertEqual(run.call_args.args[0], ["amixer", "-q", "sset", "PCM", "playback", "0%"])

    async def test_volume_rejects_device_without_card_identity(self):
        self.settings["volume"] = 75
        self.settings["device"] = "custom-output"

        with patch("app.services.audio.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "AUDIO_DEVICE"):
                await AudioPlayer(self.config).set_volume()

        run.assert_not_called()

    async def test_mixer_failures_are_reported(self):
        self.settings["volume"] = 75

        for error, message in (
            (FileNotFoundError(), "backend unavailable"),
            (PermissionError(), "command failed"),
            (subprocess.CalledProcessError(1, ["amixer"]), "command failed"),
            (subprocess.TimeoutExpired(["amixer"], 2), "timed out"),
        ):
            with self.subTest(error=error), patch("app.services.audio.subprocess.run", side_effect=error):
                with self.assertRaisesRegex(ValueError, message):
                    await AudioPlayer(self.config).set_volume()

    async def test_immediate_failure_never_reports_started(self):
        process = AsyncMock()
        process.returncode = 1
        process.wait.return_value = 1
        calls = []
        with patch("app.services.audio.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaisesRegex(ValueError, "audio_exit_1"):
                await AudioPlayer(self.config).play("alarm", repeat=True, on_started=lambda: calls.append(True))
        self.assertEqual(calls, [])

    async def test_short_alarm_file_repeats_until_stopped(self):
        finished = Mock(returncode=0, wait=AsyncMock(return_value=0))
        playing = asyncio.Event()
        stopped = asyncio.Event()
        active = Mock(returncode=None, wait=AsyncMock())

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
