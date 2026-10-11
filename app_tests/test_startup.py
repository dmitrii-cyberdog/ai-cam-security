import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.cli import main
from app.config._ import PROJECT_ROOT
from app.services.logger import configure_logging

#
#


class StartupTests(unittest.TestCase):
    def test_every_module_imports_without_parsing_arguments(self) -> None:
        modules = [
            ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts)
            for path in (PROJECT_ROOT / "app").rglob("*.py")
        ]
        code = "import importlib, sys; sys.argv = ['test', '--unknown']; "
        code += f"[importlib.import_module(name) for name in {modules!r}]"
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_startup_check_creates_storage_and_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / "storage"
            environment = {
                **os.environ,
                "STORAGE_FOLDER": str(storage),
                "LOG_LEVEL": "INFO",
                "DEBUG": "0",
            }
            for module in ("app", "app.debugger"):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--env", "dev", "--check"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)["message"], "Startup check passed"
                )
            self.assertTrue(storage.is_dir())

    def test_invalid_configuration_exits_with_usage_error(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "app", "--check"],
            env={**os.environ, "LOG_LEVEL": "invalid"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid LOG_LEVEL", result.stderr)

    def test_check_ready_offline_and_missing_assets(self):
        from app.cli import start

        with (
            patch(
                "app.cli.check_readiness", return_value=["Missing DETECTION_MODEL_PATH"]
            ),
            patch("app.cli.Runtime") as runtime,
            patch("app.cli.configure_logging"),
            patch("app.cli.logger"),
        ):
            with self.assertRaises(SystemExit) as error:
                start(["--check-ready"])
        self.assertEqual(error.exception.code, 1)
        runtime.assert_not_called()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_termination_stops_service_and_restores_handlers(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        from app_tests.test_alarm import config as test_config

        config = test_config()
        previous = signal.getsignal(signal.SIGTERM)
        task = asyncio.create_task(main(config))
        try:
            await asyncio.sleep(0)
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler))
            if callable(handler):
                handler(signal.SIGTERM, None)
            await asyncio.wait_for(task, timeout=2)
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
            messages = [
                json.loads(line)["message"] for line in stream.getvalue().splitlines()
            ]
            self.assertEqual(messages[-1], "Service stopped")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            configure_logging()
