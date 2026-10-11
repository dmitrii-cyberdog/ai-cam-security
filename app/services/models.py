from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

#
#


@dataclass(frozen=True)
class Frame:
    number: int
    stream_id: int
    captured_at: float
    captured_utc: datetime
    image: NDArray[np.uint8]

    def __post_init__(self) -> None:
        self.image.setflags(write=False)


@dataclass(frozen=True)
class Person:
    confidence: float
    box: tuple[int, int, int, int]


@dataclass(frozen=True)
class Observation:
    frame: Frame
    people: tuple[Person, ...]


@dataclass(frozen=True)
class VisionFault:
    reason: str
    at: float


VisionMessage = Observation | VisionFault


@dataclass(frozen=True)
class IncidentEvent:
    incident_id: int
    kind: str
    phase: str
    reason: str
    at: float
    at_utc: datetime
    frame: Frame | None


@dataclass(frozen=True)
class Notification:
    incident_id: int
    kind: str
    occurred_at: float
    text: str
    photo: bytes | None
