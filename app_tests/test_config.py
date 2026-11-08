import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config._ import PROJECT_ROOT, load_config
from app.services.audio import resolve_sequence

#
#


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_defaults_use_project_root(self) -> None:
        config = load_config(root=self.root)
        self.assertFalse(config["debug"])
        self.assertEqual(config["logs"]["level"], "INFO")
        self.assertEqual(config["server"]["storageFolder"], self.root / "volume")

    def test_public_configuration_has_no_credentials_or_telegram_ids(self) -> None:
        for name in (".env", ".env.dev", ".env.prod"):
            (self.root / name).write_text(
                (PROJECT_ROOT / name).read_text(encoding="utf-8"), encoding="utf-8"
            )

        for environment in (None, "dev", "prod"):
            with self.subTest(environment=environment):
                config = load_config(environment, root=self.root)

                self.assertEqual(config["camera"]["password"], "")
                self.assertEqual(config["camera"]["host"], "")
                self.assertEqual(config["telegram"]["token"], "")
                self.assertEqual(config["telegram"]["allowedUserIds"], [])
                self.assertIsNone(config["telegram"]["notificationChatId"])

    def test_layers_and_process_environment(self) -> None:
        layers = {
            ".env": "DEBUG=1\nSTORAGE_FOLDER=base\n",
            ".env.local": "STORAGE_FOLDER=local\n",
            ".env.dev": "STORAGE_FOLDER=dev\n",
            ".env.dev.local": "STORAGE_FOLDER=dev-local\n",
        }
        for name, content in layers.items():
            (self.root / name).write_text(content, encoding="utf-8")
        self.assertEqual(
            load_config("dev", root=self.root)["server"]["storageFolder"],
            self.root / "dev-local",
        )
        with patch.dict(os.environ, {"DEBUG": "false", "STORAGE_FOLDER": "runtime"}):
            config = load_config("dev", root=self.root)
        self.assertFalse(config["debug"])
        self.assertEqual(config["server"]["storageFolder"], self.root / "runtime")

    def test_explicit_environment_overrides_env_variable(self) -> None:
        (self.root / ".env.prod").write_text("DEBUG=0\n", encoding="utf-8")
        (self.root / ".env.dev").write_text("DEBUG=1\n", encoding="utf-8")
        with patch.dict(os.environ, {"ENV": "prod"}):
            self.assertFalse(load_config(root=self.root)["debug"])
            self.assertTrue(load_config("dev", root=self.root)["debug"])

    def test_invalid_external_settings_fail(self) -> None:
        for values in (
            {"DEBUG": "maybe"},
            {"LOG_LEVEL": "verbose"},
            {"ENV": "../secret"},
        ):
            with self.subTest(values=values), patch.dict(os.environ, values):
                with self.assertRaises(ValueError):
                    load_config(root=self.root)

    def test_detection_defaults_and_validation(self) -> None:
        detection = load_config(root=self.root)["detection"]
        self.assertEqual(
            detection["modelPath"], self.root / "system/models/person.onnx"
        )
        self.assertEqual(detection["confidence"], 0.5)
        self.assertEqual(detection["dayConfirmSeconds"], 2)
        self.assertEqual(detection["nightConfirmSeconds"], 3)
        for key, value in (
            ("DETECTION_CONFIDENCE", "nan"),
            ("DETECTION_CONFIDENCE", "1.1"),
            ("DETECTION_DAY_CONFIRM_SECONDS", "0"),
            ("DETECTION_TIMEZONE", "fake/zone"),
            ("DETECTION_NIGHT_START", "24:01"),
        ):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaisesRegex(ValueError, key):
                    load_config(root=self.root)

    def test_alarm_photo_interval_defaults_and_validation(self):
        name = "DETECTION_ON_ALARM_SEND_PHOTO_EVERY_MS"

        self.assertEqual(
            load_config(root=self.root)["detection"]["onAlarmSendPhotoEveryMillisecs"],
            0,
        )

        for value in ("0", "500", "10000", "3600000"):
            with self.subTest(value=value), patch.dict(os.environ, {name: value}):
                self.assertEqual(
                    load_config(root=self.root)["detection"][
                        "onAlarmSendPhotoEveryMillisecs"
                    ],
                    float(value),
                )

        for value in ("-1", "3600001", "nan", "inf", "often"):
            with self.subTest(value=value), patch.dict(os.environ, {name: value}):
                with self.assertRaisesRegex(ValueError, name):
                    load_config(root=self.root)

    def test_alarm_photo_interval_environment_precedence(self):
        name = "DETECTION_ON_ALARM_SEND_PHOTO_EVERY_MS"
        (self.root / ".env").write_text(f"{name}=10000\n", encoding="utf-8")
        (self.root / ".env.prod.local").write_text(f"{name}=20000\n", encoding="utf-8")

        self.assertEqual(
            load_config("prod", root=self.root)["detection"][
                "onAlarmSendPhotoEveryMillisecs"
            ],
            20000,
        )

        with patch.dict(os.environ, {name: "30000"}):
            self.assertEqual(
                load_config("prod", root=self.root)["detection"][
                    "onAlarmSendPhotoEveryMillisecs"
                ],
                30000,
            )

    def test_rtsp_url_from_parts(self):
        with patch.dict(
            os.environ,
            {"RTSP_PASSWORD": "verification_code", "RTSP_HOST": "192.168.1.100"},
        ):
            self.assertEqual(
                load_config(root=self.root)["camera"]["rtspUrl"],
                "rtsp://admin:verification_code@192.168.1.100:554/ch1/main",
            )

    def test_rtsp_credentials_are_encoded(self):
        with patch.dict(
            os.environ, {"RTSP_PASSWORD": "a@b:c/d#e%", "RTSP_HOST": "::1"}
        ):
            self.assertEqual(
                load_config(root=self.root)["camera"]["rtspUrl"],
                "rtsp://admin:a%40b%3Ac%2Fd%23e%25@[::1]:554/ch1/main",
            )

    def test_rtsp_missing_credentials(self):
        self.assertEqual(load_config(root=self.root)["camera"]["rtspUrl"], "")

    def test_rtsp_invalid_fields(self):
        for key, value in (
            ("RTSP_PORT", "0"),
            ("RTSP_PORT", "65536"),
            ("RTSP_PORT", "1.5"),
            ("RTSP_HOST", "rtsp://secret"),
            ("RTSP_HOST", "example:554"),
            ("RTSP_HOST", "user@host"),
            ("RTSP_HOST", "host/path"),
            ("RTSP_PATH", "secret"),
        ):
            with (
                self.subTest(key=key, value=value),
                patch.dict(os.environ, {key: value}),
            ):
                with self.assertRaisesRegex(ValueError, key) as error:
                    load_config(root=self.root)
                self.assertNotIn(value, str(error.exception))

    def test_rtsp_env_precedence(self):
        (self.root / ".env").write_text("RTSP_PASSWORD=base\nRTSP_HOST=camera\n")
        (self.root / ".env.dev.local").write_text("RTSP_PASSWORD=local\n")
        self.assertIn(
            ":local@", load_config("dev", root=self.root)["camera"]["rtspUrl"]
        )
        with patch.dict(os.environ, {"RTSP_PASSWORD": "process"}):
            self.assertIn(
                ":process@", load_config("dev", root=self.root)["camera"]["rtspUrl"]
            )

    def test_audio_defaults_and_invalid_names(self):
        self.assertEqual(load_config(root=self.root)["audio"]["mainLanguage"], "en")
        self.assertEqual(load_config(root=self.root)["audio"]["languages"], ["en"])
        for key, value in (
            ("AUDIO_MAIN_LANGUAGE", ""),
            ("AUDIO_MAIN_LANGUAGE", "../en"),
            ("AUDIO_SEQUENCE", "alarm,first_warning"),
            ("AUDIO_SEQUENCE", "first_warning,a,a,alarm"),
            ("AUDIO_MAX_ALARM_SECONDS", "inf"),
        ):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaisesRegex(ValueError, key):
                    load_config(root=self.root)

    def test_single_audio_language_becomes_default_main_language(self):
        (self.root / ".env").write_text(
            (PROJECT_ROOT / ".env").read_text(encoding="utf-8"), encoding="utf-8"
        )

        with patch.dict(os.environ, {"AUDIO_LANGUAGES": " cz "}):
            self.assertEqual(load_config(root=self.root)["audio"]["mainLanguage"], "cz")

    def test_audio_main_language_override_and_multilingual_fallback(self):
        for values, expected in (
            ({"AUDIO_LANGUAGES": "fr", "AUDIO_MAIN_LANGUAGE": "cz"}, "cz"),
            ({"AUDIO_LANGUAGES": "cz,fr"}, "en"),
        ):
            with self.subTest(values=values), patch.dict(os.environ, values):
                self.assertEqual(
                    load_config(root=self.root)["audio"]["mainLanguage"], expected
                )

    def test_bundled_sounds_resolve_with_default_audio_settings(self):
        config = load_config(root=self.root)
        config["audio"]["soundsFolder"] = PROJECT_ROOT / "system/sounds"

        paths = [
            path.relative_to(config["audio"]["soundsFolder"]).as_posix()
            for files in resolve_sequence(config).values()
            for path in files
        ]

        self.assertEqual(paths, [
            "en/first_warning.wav",
            "en/countdown_1.wav",
            "en/countdown_2.wav",
            "en/countdown_3.wav",
            "common/alarm.wav",
        ])

    def test_audio_device_defaults_when_missing_or_empty(self):
        self.assertEqual(load_config(root=self.root)["audio"]["device"], "default")

        for content in ("AUDIO_DEVICE=\n", "AUDIO_DEVICE\n"):
            with self.subTest(content=content):
                (self.root / ".env").write_text(content, encoding="utf-8")

                self.assertEqual(
                    load_config(root=self.root)["audio"]["device"], "default"
                )

    def test_audio_device_process_override(self):
        (self.root / ".env").write_text(
            "AUDIO_DEVICE=plughw:CARD=UACDemoV10,DEV=0\n", encoding="utf-8"
        )

        for value, expected in (
            ("", "default"),
            ("default", "default"),
            ("plughw:CARD=Speaker,DEV=0", "plughw:CARD=Speaker,DEV=0"),
        ):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"AUDIO_DEVICE": value}),
            ):
                self.assertEqual(
                    load_config(root=self.root)["audio"]["device"], expected
                )

    def test_audio_volume_is_optional_integer_percentage(self):
        self.assertIsNone(load_config(root=self.root)["audio"]["volume"])

        for value, expected in (("", None), ("0", 0), ("75", 75), ("100", 100)):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"AUDIO_VOLUME": value}),
            ):
                self.assertEqual(
                    load_config(root=self.root)["audio"]["volume"], expected
                )

        for value in ("-1", "101", "75%", "1.5", "nan", "loud"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"AUDIO_VOLUME": value}),
            ):
                with self.assertRaisesRegex(ValueError, "AUDIO_VOLUME"):
                    load_config(root=self.root)

    def test_audio_volume_production_override_and_process_precedence(self):
        (self.root / ".env").write_text("AUDIO_VOLUME=50\n", encoding="utf-8")
        (self.root / ".env.prod.local").write_text(
            "AUDIO_VOLUME=75\n", encoding="utf-8"
        )

        self.assertEqual(load_config("prod", root=self.root)["audio"]["volume"], 75)

        with patch.dict(os.environ, {"AUDIO_VOLUME": "90"}):
            self.assertEqual(load_config("prod", root=self.root)["audio"]["volume"], 90)

    def test_telegram_ids_and_defaults(self):
        self.assertEqual(load_config(root=self.root)["telegram"]["allowedUserIds"], [])
        with patch.dict(
            os.environ,
            {"TELEGRAM_ALLOWED_USER_IDS": "42, 43", "TELEGRAM_CHAT_ID": "-100"},
        ):
            self.assertEqual(
                load_config(root=self.root)["telegram"]["allowedUserIds"], [42, 43]
            )
            self.assertEqual(
                load_config(root=self.root)["telegram"]["notificationChatId"], -100
            )
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "secret"}):
            with self.assertRaisesRegex(ValueError, "TELEGRAM_ALLOWED_USER_IDS"):
                load_config(root=self.root)

    def test_alarm_cycle_limit_is_a_positive_integer(self):
        self.assertEqual(load_config(root=self.root)["audio"]["maxAlarmCycles"], 3)

        with patch.dict(os.environ, {"AUDIO_MAX_ALARM_CYCLES": "2"}):
            self.assertEqual(
                load_config(root=self.root)["audio"].get("maxAlarmCycles"), 2
            )

        for value in ("0", "-1", "1.5", "nan", "unlimited"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"AUDIO_MAX_ALARM_CYCLES": value}),
            ):
                with self.assertRaisesRegex(ValueError, "AUDIO_MAX_ALARM_CYCLES"):
                    load_config(root=self.root)

    def test_confidence_override_loads_in_production(self):
        (self.root / ".env").write_text("DETECTION_CONFIDENCE=0.35\n", encoding="utf-8")

        self.assertEqual(
            load_config("prod", root=self.root)["detection"]["confidence"], 0.35
        )

        with patch.dict(os.environ, {"DETECTION_CONFIDENCE": "0.6"}):
            self.assertEqual(
                load_config("prod", root=self.root)["detection"]["confidence"], 0.6
            )
