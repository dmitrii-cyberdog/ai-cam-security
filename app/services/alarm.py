from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

from app.services.detection import MAX_POSITIVE_GAP_SECONDS, MIN_POSITIVE_FRAMES
from app.services.models import Frame, IncidentEvent, Observation

#
#


@dataclass(frozen=True)
class PlayKey:
    incident_id: int
    generation: int
    key: str
    repeat: bool


@dataclass(frozen=True)
class StopPlayback:
    generation: int


AlarmEffect = PlayKey | StopPlayback | IncidentEvent


class AlarmController:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.detection = config["detection"]
        self.audio = config["audio"]
        self.zone = ZoneInfo(self.detection["timezone"])
        self.armed = False
        self.phase = "idle"
        self.manual = False
        self.generation = 0
        self.incident_id = 0
        self.suppression = ""
        self.last_command_result = "Started disarmed"
        self.profile = "day"
        self.frame: Frame | None = None
        self._stream: int | None = None
        self._number = -1
        self._last_capture = -1.0
        self._last_seen: float | None = None
        self._positive = False
        self._candidate: float | None = None
        self._duration = 0.0
        self._positive_count = 0
        self._last_positive: float | None = None
        self._negative_start: float | None = None
        self._last_negative: float | None = None
        self._key_index = 0
        self._waiting = False
        self._alarm_started: float | None = None
        self._alarm_cycles = 0
        self._last_alarm_photo_at: float | None = None

    def _profile(self, utc_now: datetime) -> None:
        local = utc_now.astimezone(self.zone).strftime("%H:%M")
        start, end = self.detection["nightStart"], self.detection["nightEnd"]
        night = start <= local < end if start < end else local >= start or local < end
        self.profile = "night" if night else "day"

    def _clear_evidence(self) -> None:
        self._candidate = None
        self._positive_count = 0
        self._last_positive = None
        self._negative_start = None
        self._last_negative = None
        self._positive = False
        if self.phase == "confirming":
            self.phase = "idle"

    def _event(
        self, kind: str, reason: str, now: float, utc_now: datetime
    ) -> IncidentEvent:
        return IncidentEvent(
            self.incident_id, kind, self.phase, reason, now, utc_now, self.frame
        )

    def _play(self) -> PlayKey:
        self.generation += 1
        self._waiting = False
        key = self.audio["sequence"][self._key_index]
        self.phase = (
            "warning"
            if self._key_index == 0
            else "alarm" if key == "alarm" else "countdown"
        )
        return PlayKey(self.incident_id, self.generation, key, key == "alarm")

    def _end(self, reason: str, now: float, utc_now: datetime) -> list[AlarmEffect]:
        self.generation += 1
        effects: list[AlarmEffect] = []
        if self.phase in {"warning", "countdown", "alarm"}:
            effects = [
                StopPlayback(self.generation),
                self._event("deactivated", reason, now, utc_now),
            ]
        self.phase = "idle"
        self.manual = False
        self._waiting = False
        self._alarm_started = None
        self._last_alarm_photo_at = None

        if reason == "person_left":
            self._alarm_cycles = 0

        self._clear_evidence()

        return effects

    def _advance(self, now: float, utc_now: datetime) -> list[AlarmEffect]:
        if (
            self._last_seen is None
            or now - self._last_seen > self.detection["maxFrameAgeSeconds"]
        ):
            self._waiting = True
            return []

        next_index = self._key_index + 1

        if self.audio["sequence"][next_index] == "alarm" and not self._positive:
            if self._negative_start is not None and self._last_negative is not None:
                if (
                    self._last_negative - self._negative_start
                    >= self.detection["absenceSeconds"]
                ):
                    return self._end("person_left", now, utc_now)

            self._waiting = True
            return []

        effects: list[AlarmEffect] = []

        if self.phase == "warning" and self._positive:
            effects.append(
                self._event("still_there", "presence_confirmed", now, utc_now)
            )

        self._key_index = next_index
        effects.append(self._play())

        return effects

    def observe(
        self, observation: Observation, now: float, utc_now: datetime
    ) -> list[AlarmEffect]:
        self._profile(utc_now)
        frame = observation.frame
        if not 0 <= now - frame.captured_at <= self.detection["maxFrameAgeSeconds"]:
            return self.tick(now, utc_now)
        effects: list[AlarmEffect] = []
        if self._stream is not None and frame.stream_id < self._stream:
            return effects
        if self._stream is not None and frame.stream_id != self._stream:
            effects += self.vision_fault("camera_unavailable", now, utc_now)
        if frame.number <= self._number or frame.captured_at <= self._last_capture:
            return effects
        self._stream, self._number = frame.stream_id, frame.number
        self._last_capture = frame.captured_at
        self.frame = frame
        self._last_seen = frame.captured_at
        self._positive = bool(observation.people)
        at = frame.captured_at
        if self._positive:
            self._negative_start = None
            self._last_negative = None
            if (
                self._last_positive is None
                or at - self._last_positive > MAX_POSITIVE_GAP_SECONDS
            ):
                self._candidate = at
                self._positive_count = 0
                self._duration = self.detection[f"{self.profile}ConfirmSeconds"]
            self._last_positive = at
            self._positive_count += 1
            if self._waiting:
                effects += self._advance(now, utc_now)
            elif (
                self.armed
                and not self.suppression
                and self.phase in {"idle", "confirming"}
            ):
                if self._alarm_cycles >= self.audio["maxAlarmCycles"]:
                    self.suppression = "until_absence"
                    return effects

                self.phase = "confirming"
                if (
                    self._candidate is not None
                    and at - self._candidate >= self._duration
                    and self._positive_count >= MIN_POSITIVE_FRAMES
                ):
                    self.incident_id += 1
                    self._key_index = 0
                    self.phase = "warning"
                    effects += [
                        self._event("recognized", "presence_confirmed", now, utc_now),
                        self._play(),
                    ]
        else:
            if (
                self._last_positive is not None
                and at - self._last_positive > MAX_POSITIVE_GAP_SECONDS
            ):
                self._candidate = None
                self._positive_count = 0
                if self.phase == "confirming":
                    self.phase = "idle"
            if (
                self._last_negative is None
                or at - self._last_negative > MAX_POSITIVE_GAP_SECONDS
            ):
                self._negative_start = at
            self._last_negative = at
            if (
                self._negative_start is not None
                and at - self._negative_start >= self.detection["absenceSeconds"]
            ):
                if self.suppression != "audio_fault":
                    self.suppression = ""

                if not self.manual and (
                    self.phase not in {"warning", "countdown", "alarm"} or self._waiting
                ):
                    effects += self._end("person_left", now, utc_now)

        return effects

    def tick(self, now: float, utc_now: datetime) -> list[AlarmEffect]:
        self._profile(utc_now)

        if (
            self._last_seen is not None
            and now - self._last_seen > self.detection["maxFrameAgeSeconds"]
        ):
            self._last_seen = None
            self._clear_evidence()

            if not self.manual:
                return self._end("camera_unavailable", now, utc_now)

        while (
            self._alarm_started is not None
            and now - self._alarm_started >= self.audio["maxAlarmSeconds"]
        ):
            if (
                not self.manual
                and self._negative_start is not None
                and self._last_negative is not None
            ):
                if (
                    self._last_negative - self._negative_start
                    >= self.detection["absenceSeconds"]
                ):
                    return self._end("person_left", now, utc_now)

            if not self.manual and self._alarm_cycles < self.audio["maxAlarmCycles"]:
                self._alarm_cycles += 1
                self._alarm_started += self.audio["maxAlarmSeconds"]
                continue

            if self.suppression != "audio_fault":
                self.suppression = "until_absence"

            return self._end("maximum_duration", now, utc_now)

        photo_interval = self.detection["onAlarmSendPhotoEveryMillisecs"]

        if (
            photo_interval > 0
            and self._last_alarm_photo_at is not None
            and (now - self._last_alarm_photo_at) * 1000 >= photo_interval
            and self._last_seen is not None
            and self.frame is not None
            and self.frame.captured_at > self._last_alarm_photo_at
            and 0
            <= now - self.frame.captured_at
            <= self.detection["maxFrameAgeSeconds"]
        ):
            self._last_alarm_photo_at = now

            return [self._event("alarm_photo", "alarm_active", now, utc_now)]

        return []

    def vision_fault(
        self, reason: str, now: float, utc_now: datetime
    ) -> list[AlarmEffect]:
        self._stream = None
        self._number = -1
        self._last_capture = -1
        self._last_seen = None
        self._clear_evidence()
        if not self.manual:
            return self._end(
                (
                    "camera_unavailable"
                    if reason.startswith("camera")
                    else "detector_unavailable"
                ),
                now,
                utc_now,
            )
        return []

    def command(
        self,
        name: str,
        now: float,
        utc_now: datetime,
        readiness_error: str | None = None,
    ) -> list[AlarmEffect]:
        self._profile(utc_now)
        if name in {"arm", "alarm"} and readiness_error:
            self.last_command_result = f"Cannot {name}: {readiness_error}"
            return []
        self.last_command_result = name
        if name == "arm":
            self.armed = True
            self.suppression = ""
            if self.phase in {"idle", "confirming"}:
                self._alarm_cycles = 0
                self._clear_evidence()
        elif name in {"stop", "disarm"}:
            if name == "disarm":
                self.armed = False

            if self.suppression != "audio_fault":
                self.suppression = "" if name == "stop" else "until_absence"

                if name == "stop":
                    self._alarm_cycles = 0

            return self._end(name, now, utc_now)
        elif name == "alarm":
            if self.phase == "alarm":
                self.manual = True
                return []
            effects: list[AlarmEffect] = []
            if self.phase in {"warning", "countdown"}:
                self.generation += 1
                effects.append(StopPlayback(self.generation))
            else:
                self.incident_id += 1
            self.manual = True
            self._key_index = len(self.audio["sequence"]) - 1
            effects.append(self._play())
            return effects
        return []

    def audio_event(
        self, generation: int, event: str, now: float, utc_now: datetime
    ) -> list[AlarmEffect]:
        if generation != self.generation or self.phase not in {
            "warning",
            "countdown",
            "alarm",
        }:
            return []
        if event == "failed":
            self.suppression = "audio_fault"
            return self._end("audio_fault", now, utc_now)
        if event == "started" and self.phase == "alarm" and self._alarm_started is None:
            self._alarm_started = now
            self._last_alarm_photo_at = now

            if not self.manual:
                self._alarm_cycles += 1

            return [
                self._event(
                    "activated",
                    "manual" if self.manual else "presence_confirmed",
                    now,
                    utc_now,
                )
            ]
        if event == "finished" and self.phase != "alarm":
            return self._advance(now, utc_now)
        return []

    def status(self) -> Dict[str, Any]:
        return {
            "armed": self.armed,
            "phase": self.phase,
            "manual": self.manual,
            "suppression": bool(self.suppression),
            "suppressionReason": self.suppression,
            "lastCommandResult": self.last_command_result,
            "profile": self.profile,
            "alarmCycles": self._alarm_cycles,
            "maxAlarmCycles": self.audio["maxAlarmCycles"],
        }
