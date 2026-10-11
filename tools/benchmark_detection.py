import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from app.services.detection import PersonDetector
from app.services.models import Frame

#
#


def benchmark(video: Path, model: Path, confidence: float) -> dict[str, object]:
    if not video.is_file() or not model.is_file():
        raise ValueError("Video and model must be local files")
    if not 0 < confidence <= 1:
        raise ValueError("Invalid confidence")

    detector = PersonDetector({"detection": {"modelPath": model, "confidence": confidence}})
    capture = cv2.VideoCapture(str(video))
    timings: dict[str, list[float]] = {key: [] for key in ("preprocess", "inference", "postprocess")}
    detections: list[dict[str, int | float | None]] = []

    try:
        ok, image = capture.read()

        if not ok:
            raise ValueError("Video could not be decoded")

        for _ in range(5):
            detector.infer(Frame(0, 1, time.monotonic(), datetime.now(timezone.utc), cast(NDArray[np.uint8], image)))

        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        fps = capture.get(cv2.CAP_PROP_FPS)
        started, cpu_started = time.perf_counter(), time.process_time()

        while True:
            ok, image = capture.read()

            if not ok:
                break

            number = len(detections) + 1
            result = detector.infer(Frame(number, 1, time.monotonic(), datetime.now(timezone.utc), cast(NDArray[np.uint8], image)))
            detections.append({"frame": number, "video_seconds": (number - 1) / fps if fps else None, "people": len(result.people)})

            for key in timings:
                timings[key].append(detector.timings[key] * 1000)

        elapsed, cpu = time.perf_counter() - started, time.process_time() - cpu_started
    finally:
        capture.release()

    if not detections:
        raise ValueError("No measured frames")

    rss = None

    if sys.platform == "linux":
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

    temperature = None
    thermal = Path("/sys/class/thermal/thermal_zone0/temp")

    if thermal.exists():
        temperature = float(thermal.read_text()) / 1000

    throttling = None

    try:
        throttling = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    return {
        "python": platform.python_version(), "opencv": cv2.__version__, "numpy": np.__version__,
        "os": platform.platform(), "machine": platform.machine(), "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
        "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "confidence": confidence,
        "frames": len(detections), "clip_seconds": len(detections) / fps if fps else None,
        "elapsed_seconds": elapsed, "effective_results_per_second": len(detections) / elapsed,
        "cpu_percent_one_core": cpu / elapsed * 100, "peak_rss_bytes": rss,
        "temperature_c_end": temperature, "throttling_end": throttling,
        "latency_ms": {key: {"p50": float(np.percentile(values, 50)), "p95": float(np.percentile(values, 95))} for key, values in timings.items()},
        "frames_for_label_review": detections, "accuracy_review": "pending manual person/empty labels",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Local NanoDet recorded-video benchmark; no audio or Telegram")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--confidence", type=float, default=0.5)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    result: dict[str, Any] = benchmark(args.video, args.model, args.confidence)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
