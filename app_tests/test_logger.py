import io
import json
import logging
import unittest
from pathlib import Path

from app.services.logger import LOGGER_NAME, Logger, configure_logging

#
#


class LoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = io.StringIO()
        configure_logging(stream=self.stream)
        self.addCleanup(self.clear_handlers)

    def clear_handlers(self) -> None:
        entity = logging.getLogger(LOGGER_NAME)
        for handler in entity.handlers[:]:
            entity.removeHandler(handler)
            handler.close()

    def test_context_is_visible_and_cannot_overwrite_record_fields(self) -> None:
        logger = Logger({"camera": "front"})
        logger.info(
            "Connected",
            {"name": "camera", "message": "context", "path": Path("volume")},
        )
        entry = json.loads(self.stream.getvalue())
        self.assertEqual(entry["message"], "Connected")
        self.assertEqual(entry["context"]["camera"], "front")
        self.assertEqual(entry["context"]["message"], "context")
        self.assertEqual(entry["context"]["path"], "volume")

    def test_instances_and_reconfiguration_do_not_duplicate_messages(self) -> None:
        configure_logging(stream=self.stream)
        Logger().info("one")
        Logger().info("two")
        self.assertEqual(len(self.stream.getvalue().splitlines()), 2)
        self.assertEqual(len(logging.getLogger(LOGGER_NAME).handlers), 1)

    def test_debug_filter_and_exception_traceback(self) -> None:
        logger = Logger()
        logger.debug("hidden")
        self.assertEqual(self.stream.getvalue(), "")
        configure_logging("DEBUG", stream=self.stream)
        logger.debug("visible")
        try:
            raise RuntimeError("camera disconnected")
        except RuntimeError:
            logger.exception("Worker failed")
        entries = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        self.assertEqual(entries[0]["level"], "DEBUG")
        self.assertIn("RuntimeError: camera disconnected", entries[1]["exception"])

    def test_default_context_is_not_shared_or_mutated(self) -> None:
        context: dict[str, object] = {"camera": "front"}
        logger = Logger(context)
        context["camera"] = "back"
        logger.info("one", {"camera": "side"})
        logger.info("two")
        Logger().info("three")
        entries = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        self.assertEqual(
            [entry["context"] for entry in entries],
            [{"camera": "side"}, {"camera": "front"}, {}],
        )

    def test_redacts_final_output_and_third_party_tracebacks(self):
        secrets = [
            "rtsp://admin:p@camera/path",
            "a@b:c/d#e%",
            "123:bot-token",
            'quote"slash\\secret',
        ]
        configure_logging(stream=self.stream, secrets=secrets)
        from urllib.parse import quote

        try:
            raise RuntimeError(" ".join(secrets))
        except RuntimeError:
            Logger().exception(
                " ".join(secrets),
                {"nested": {"url": secrets[0], "encoded": quote(secrets[1], safe="")}},
            )
            logging.getLogger("httpx").warning(" ".join(secrets), exc_info=True)
        output = self.stream.getvalue()
        for secret in secrets:
            self.assertNotIn(secret, output)
            self.assertNotIn(quote(secret, safe=""), output)
        self.assertEqual(len(output.splitlines()), 2)
