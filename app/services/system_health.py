import math
import os
from pathlib import Path
from typing import Any, Dict

#
#

THERMAL_ROOT = Path("/sys/class/thermal")
MEMINFO_PATH = Path("/proc/meminfo")


def _read_text(path: Path, limit: int) -> str:
    with path.open(encoding="utf-8") as source:
        text = source.read(limit)

    if len(text) == limit:
        raise ValueError("Metric file exceeds read limit")

    return text


def read_system_health() -> Dict[str, Any]:
    health: Dict[str, Any] = {
        "cpuTemperatureCelsius": None,
        "loadAverage": None,
        "memoryUsedMiB": None,
        "memoryTotalMiB": None,
    }

    try:
        for zone in sorted(THERMAL_ROOT.glob("thermal_zone*")):
            try:
                kind = _read_text(zone / "type", 256).strip()

                if kind not in {"cpu-thermal", "cpu_thermal", "x86_pkg_temp"}:
                    continue

                temperature = int(_read_text(zone / "temp", 256).strip()) / 1000

                if -273.15 <= temperature <= 250:
                    health["cpuTemperatureCelsius"] = temperature
                    break
            except (OSError, ValueError):
                continue
    except OSError:
        pass

    try:
        load = os.getloadavg()

        if all(math.isfinite(value) and value >= 0 for value in load):
            health["loadAverage"] = load
    except (AttributeError, OSError):
        pass

    try:
        memory = {}

        for line in _read_text(MEMINFO_PATH, 65536).splitlines():
            fields = line.split()

            if fields and fields[0] in {"MemTotal:", "MemAvailable:"}:
                if len(fields) != 3 or fields[2] != "kB":
                    raise ValueError("Invalid memory metric")

                memory[fields[0]] = int(fields[1])

        total, available = memory["MemTotal:"], memory["MemAvailable:"]

        if 0 <= available <= total and total > 0:
            health["memoryTotalMiB"] = total / 1024
            health["memoryUsedMiB"] = (total - available) / 1024
    except (OSError, ValueError, KeyError):
        pass

    return health
