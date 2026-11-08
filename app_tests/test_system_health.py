import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.system_health import read_system_health

#
#


class SystemHealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.thermal = self.root / "thermal"
        self.thermal.mkdir()
        self.meminfo = self.root / "meminfo"
        self.meminfo.write_text("MemTotal: 4096000 kB\nMemAvailable: 1024000 kB\n")

        for target, value in (("THERMAL_ROOT", self.thermal), ("MEMINFO_PATH", self.meminfo)):
            boundary = patch(f"app.services.system_health.{target}", value)
            boundary.start()
            self.addCleanup(boundary.stop)

    def zone(self, number, kind, temperature):
        zone = self.thermal / f"thermal_zone{number}"
        zone.mkdir()
        (zone / "type").write_text(kind)
        (zone / "temp").write_text(temperature)

    def test_reads_cpu_zone_memory_and_named_load_averages(self):
        self.zone(0, "gpu", "90000")
        self.zone(1, "cpu-thermal", "57321")

        with patch("app.services.system_health.os.getloadavg", return_value=(0.1, 0.5, 1.5), create=True):
            health = read_system_health()

        self.assertEqual(health["cpuTemperatureCelsius"], 57.321)
        self.assertEqual(health["loadAverage"], (0.1, 0.5, 1.5))
        self.assertEqual(health["memoryUsedMiB"], 3000)
        self.assertEqual(health["memoryTotalMiB"], 4000)

    def test_independent_unavailable_metrics(self):
        self.zone(0, "cpu-thermal", "not a temperature")

        with patch("app.services.system_health.os.getloadavg", side_effect=OSError, create=True):
            health = read_system_health()

        self.assertIsNone(health["cpuTemperatureCelsius"])
        self.assertIsNone(health["loadAverage"])
        self.assertEqual(health["memoryUsedMiB"], 3000)

    def test_missing_load_average_api_preserves_other_metrics(self):
        self.zone(0, "cpu-thermal", "57321")

        with patch("app.services.system_health.os", spec=[]):
            health = read_system_health()

        self.assertIsNone(health["loadAverage"])
        self.assertEqual(health["cpuTemperatureCelsius"], 57.321)
        self.assertEqual(health["memoryUsedMiB"], 3000)

    def test_invalid_memory_does_not_hide_temperature(self):
        self.zone(0, "cpu-thermal", "0")

        for content in ("", "MemTotal: nan kB\nMemAvailable: 1 kB", "MemTotal: 100 kB\nMemAvailable: 200 kB", "MemTotal: 10 MB\nMemAvailable: 0 MB"):
            with self.subTest(content=content):
                self.meminfo.write_text(content)
                health = read_system_health()

                self.assertEqual(health["cpuTemperatureCelsius"], 0)
                self.assertIsNone(health["memoryUsedMiB"])
                self.assertIsNone(health["memoryTotalMiB"])

    def test_nonfinite_metrics_are_unavailable(self):
        self.zone(0, "cpu-thermal", "nan")

        with patch("app.services.system_health.os.getloadavg", return_value=(float("nan"), 0, 0), create=True):
            health = read_system_health()

        self.assertIsNone(health["cpuTemperatureCelsius"])
        self.assertIsNone(health["loadAverage"])

    def test_missing_and_unreadable_files_are_unavailable(self):
        with patch("app.services.system_health.Path.open", side_effect=PermissionError):
            health = read_system_health()

        self.assertIsNone(health["memoryUsedMiB"])
        self.assertIsNone(health["cpuTemperatureCelsius"])
