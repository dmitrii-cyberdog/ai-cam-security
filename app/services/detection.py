# Adapted from OpenCV Zoo NanoDet and demo.py, revision 47534e27c9851bb1128ccc0102f1145e27f23f98.
# Apache-2.0; see docs/licenses/nanodet-LICENSE. Modified for person filtering, safe errors, timing and frame ownership.

from time import perf_counter
from typing import Any, Dict, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from app.services.models import Frame, Observation, Person

#
#

STRIDES = (8, 16, 32)
INPUT_SIZE = 416
MIN_POSITIVE_FRAMES = 3
MAX_POSITIVE_GAP_SECONDS = 1.0


class PersonDetector:
    def __init__(self, config: Dict[str, Any]) -> None:
        settings = config["detection"]
        self.confidence = float(settings["confidence"])
        self.timings: dict[str, float] = {}

        try:
            self.net = cv2.dnn.readNet(str(settings["modelPath"]))
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        except Exception:
            raise ValueError("model_unavailable: check DETECTION_MODEL_PATH and NanoDet artifact") from None

        self.anchors = []

        for stride in STRIDES:
            x, y = np.meshgrid(np.arange(INPUT_SIZE // stride), np.arange(INPUT_SIZE // stride))
            self.anchors.append(np.column_stack((x.ravel(), y.ravel())) * stride + 0.5 * (stride - 1))

    def infer(self, frame: Frame) -> Observation:
        started = perf_counter()
        height, width = frame.image.shape[:2]
        scale = INPUT_SIZE / max(height, width)
        new_h, new_w = max(1, int(height * scale)), max(1, int(width * scale))
        top, left = (INPUT_SIZE - new_h) // 2, (INPUT_SIZE - new_w) // 2
        image = cv2.cvtColor(frame.image, cv2.COLOR_BGR2RGB)
        image = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
        image = cv2.copyMakeBorder(image, top, INPUT_SIZE - new_h - top, left, INPUT_SIZE - new_w - left, cv2.BORDER_CONSTANT, value=0)
        normalized = (image.astype(np.float32) - np.array([103.53, 116.28, 123.675], np.float32)) / np.array([57.375, 57.12, 58.395], np.float32)
        self.net.setInput(cv2.dnn.blobFromImage(cast(NDArray[np.float32], normalized)))
        prepared = perf_counter()

        try:
            outputs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        except Exception:
            raise ValueError("detector_inference_failed") from None

        inferred = perf_counter()
        boxes, scores = self._decode(outputs)
        people = []

        if len(boxes):
            xywh = boxes.copy()
            xywh[:, 2:] -= xywh[:, :2]
            indices = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), 0.0, 0.6)

            for index in np.asarray(indices).reshape(-1):
                box = boxes[index].copy()
                box[[0, 2]] = np.clip((box[[0, 2]] - left) * width / new_w, 0, width)
                box[[1, 3]] = np.clip((box[[1, 3]] - top) * height / new_h, 0, height)
                x1, y1, x2, y2 = (int(value) for value in box)

                if x2 > x1 and y2 > y1:
                    people.append(Person(float(scores[index]), (x1, y1, x2, y2)))

        self.timings = {"preprocess": prepared - started, "inference": inferred - prepared, "postprocess": perf_counter() - inferred}

        return Observation(frame, tuple(people))

    def _decode(self, outputs: Any) -> tuple[NDArray[np.float64], NDArray[np.float32]]:
        if len(outputs) != 6:
            raise ValueError("detector_output_contract")

        boxes, scores = [], []

        for stride, anchors, classes, regression in zip(STRIDES, self.anchors, outputs[::2], outputs[1::2]):
            classes = np.asarray(classes).reshape(-1, 80)
            regression = np.asarray(regression).reshape(-1, 4, 8)

            if len(classes) != len(anchors) or len(regression) != len(anchors):
                raise ValueError("detector_output_contract")

            indices = classes.max(axis=1).argsort()[::-1][:1000]
            indices = indices[(classes[indices].argmax(axis=1) == 0) & (classes[indices, 0] >= self.confidence)]
            logits = regression[indices]
            exp = np.exp(logits - logits.max(axis=2, keepdims=True))
            distances = (exp / exp.sum(axis=2, keepdims=True)) @ np.arange(8) * stride
            selected = anchors[indices]
            boxes.append(np.clip(np.column_stack((selected - distances[:, :2], selected + distances[:, 2:])), 0, INPUT_SIZE))
            scores.append(classes[indices, 0])

        return np.concatenate(boxes), np.concatenate(scores)
