import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.config._ import load_config
from app.services.alarm import AlarmController, PlayKey, StopPlayback
from app.services.models import Frame, IncidentEvent, Observation, Person

#
#

DAY = datetime(2026, 9, 27, 10, tzinfo=timezone.utc)
NIGHT = datetime(2026, 9, 27, 21, tzinfo=timezone.utc)


def config():
    with patch.dict(os.environ, {}, clear=True):
        return load_config(root=Path(tempfile.gettempdir()) / "camera-alarm-no-env")


def observation(number, at, people=1, stream=1):
    return Observation(
        Frame(
            number,
            stream,
            at,
            DAY + timedelta(seconds=at),
            np.zeros((16, 16, 3), np.uint8),
        ),
        tuple(Person(0.9, (1, 1, 5, 5)) for _ in range(people)),
    )


def kinds(effects):
    return [effect.kind for effect in effects if isinstance(effect, IncidentEvent)]


class AlarmTests(unittest.TestCase):
    def setUp(self):
        self.controller = AlarmController(config())
        self.controller.command("arm", 0, DAY)
        self.number = 0

    def observe(self, at, people=1, utc=DAY, stream=1):
        self.number += 1
        return self.controller.observe(
            observation(self.number, at, people, stream),
            at,
            utc + timedelta(seconds=at),
        )

    def confirm(self, utc=DAY):
        effects = []
        for at in (0, 0.5, 1, 1.5, 2):
            effects += self.observe(at, utc=utc)
        return effects

    def finish(self, at=2):
        return self.controller.audio_event(
            self.controller.generation, "finished", at, DAY
        )

    def test_day_confirmation(self):
        self.assertEqual(kinds(self.confirm()), ["recognized"])

    def test_night_confirmation(self):
        self.assertEqual(kinds(self.confirm(NIGHT)), [])
        self.observe(2.5, utc=NIGHT)
        self.assertEqual(kinds(self.observe(3, utc=NIGHT)), ["recognized"])

    def test_repeated_and_out_of_order_frames_never_confirm(self):
        for at in (0, 0.5, 1, 2, 3):
            self.assertEqual(self.controller.observe(observation(1, 0), at, DAY), [])
        self.assertNotEqual(self.controller.status()["phase"], "warning")

    def test_profile_boundaries_and_frozen_candidate(self):
        for utc, profile in (
            (datetime(2026, 9, 27, 20, tzinfo=timezone.utc), "night"),
            (datetime(2026, 9, 27, 4, tzinfo=timezone.utc), "day"),
            (datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc), "night"),
        ):
            self.controller.tick(0, utc)
            self.assertEqual(self.controller.status()["profile"], profile)
        self.controller = AlarmController(config())
        self.controller.command("arm", 0, DAY)
        start = datetime(2026, 9, 27, 19, 59, 59, tzinfo=timezone.utc)
        self.assertEqual(kinds(self.confirm(start)), ["recognized"])

    def test_disarm_ignores_late_audio_finish(self):
        self.confirm()
        old = self.controller.generation
        self.controller.command("disarm", 2, DAY)
        self.assertEqual(self.controller.audio_event(old, "finished", 2, DAY), [])
        self.assertFalse(self.controller.status()["armed"])

    def test_departure_finishes_prelude_without_siren(self):
        for finishes in (0, 1, 2, 3):
            self.setUp()
            self.confirm()

            for _ in range(finishes):
                self.finish()

            effects = []

            for at in (2.5, 3, 3.5, 4, 4.5):
                effects += self.observe(at, 0)

            self.assertEqual(effects, [])
            played = []

            for _ in range(4 - finishes):
                next_effects = self.finish(4.5)
                played += [
                    effect.key for effect in next_effects if isinstance(effect, PlayKey)
                ]
                effects += next_effects

            self.assertNotIn("alarm", played)
            self.assertEqual(
                played, self.controller.audio["sequence"][finishes + 1 : -1]
            )

            self.assertEqual(kinds(effects), ["deactivated"])
            self.assertEqual(
                sum(isinstance(effect, StopPlayback) for effect in effects), 1
            )

    def test_departure_during_siren_waits_for_period_limit(self):
        self.start_siren()
        generation = self.controller.generation

        for half_second in range(5, 125):
            at = half_second / 2
            self.assertEqual(self.observe(at, 0), [])

            if at < 62:
                self.assertEqual(self.controller.tick(at, DAY), [])
                self.assertEqual(self.controller.generation, generation)

        effects = self.controller.tick(62, DAY)

        self.assertEqual(kinds(effects), ["deactivated"])
        self.assertEqual(
            next(
                effect.reason for effect in effects if isinstance(effect, IncidentEvent)
            ),
            "person_left",
        )
        self.assertEqual(self.controller.status().get("alarmCycles"), 0)

    def test_return_before_siren_limit_keeps_original_cycle_budget(self):
        self.start_siren()
        self.observe(62)
        self.controller.tick(62, DAY)

        for at in (63, 63.5, 64, 64.5, 65):
            self.assertEqual(self.observe(at, 0), [])

        self.observe(66)
        self.observe(122)
        self.assertEqual(self.controller.tick(122, DAY), [])
        self.assertEqual(self.controller.status().get("alarmCycles"), 3)
        self.observe(182)
        self.assertEqual(kinds(self.controller.tick(182, DAY)), ["deactivated"])

    def test_stop_disarm_and_faults_interrupt_siren_before_limit(self):
        for action in ("stop", "disarm", "camera", "audio", "stale"):
            with self.subTest(action=action):
                self.setUp()
                self.start_siren()

                if action in {"stop", "disarm"}:
                    effects = self.controller.command(action, 3, DAY)
                elif action == "camera":
                    effects = self.controller.vision_fault("camera_unavailable", 3, DAY)
                elif action == "audio":
                    effects = self.controller.audio_event(
                        self.controller.generation, "failed", 3, DAY
                    )
                else:
                    effects = self.controller.tick(4.01, DAY)

                self.assertEqual(kinds(effects), ["deactivated"])
                self.assertTrue(
                    any(isinstance(effect, StopPlayback) for effect in effects)
                )
                self.assertEqual(self.controller.phase, "idle")

    def test_brief_miss_at_period_boundary_does_not_end_siren(self):
        self.start_siren()
        self.observe(61)
        self.observe(61.5, 0)
        self.observe(62, 0)

        self.assertEqual(self.controller.tick(62, DAY), [])
        self.assertEqual(self.controller.status()["alarmCycles"], 2)

    def test_camera_stale_is_not_absence(self):
        self.confirm()
        effects = self.controller.tick(4.01, DAY)
        self.assertEqual(kinds(effects), ["deactivated"])
        self.assertEqual(
            next(
                effect.reason for effect in effects if isinstance(effect, IncidentEvent)
            ),
            "camera_unavailable",
        )
        self.assertTrue(self.controller.status()["armed"])

    def test_manual_alarm_has_no_person_events(self):
        self.controller.command("disarm", 0, DAY)
        effects = self.controller.command("alarm", 0, DAY)
        effects += self.controller.audio_event(
            self.controller.generation, "started", 0.1, DAY
        )
        effects += self.controller.command("stop", 1, DAY)
        self.assertEqual(kinds(effects), ["activated", "deactivated"])

    def test_stop_allows_fresh_confirmation_without_absence_or_arm(self):
        for utc in (DAY, NIGHT):
            with self.subTest(utc=utc):
                self.setUp()
                self.controller.audio["maxAlarmCycles"] = 1
                self.start_siren()
                generation = self.controller.generation

                effects = self.controller.command("stop", 2, utc)

                self.assertEqual(kinds(effects), ["deactivated"])
                self.assertTrue(
                    any(isinstance(effect, StopPlayback) for effect in effects)
                )
                self.assertTrue(self.controller.status()["armed"])
                self.assertFalse(self.controller.status()["suppression"])
                self.assertEqual(self.controller.status()["alarmCycles"], 0)
                self.assertEqual(
                    self.controller.audio_event(generation, "finished", 2.1, utc), []
                )
                self.assertEqual(
                    self.controller.audio_event(generation, "failed", 2.1, utc), []
                )

                duration = self.controller.detection[
                    f"{self.controller.profile}ConfirmSeconds"
                ]

                for half_second in range(int(duration * 2)):
                    self.assertEqual(self.observe(2.5 + half_second / 2, utc=utc), [])

                effects = self.observe(2.5 + duration, utc=utc)

                self.assertEqual(kinds(effects), ["recognized"])
                self.assertEqual(
                    [effect.key for effect in effects if isinstance(effect, PlayKey)],
                    ["first_warning"],
                )

    def test_stop_keeps_disarmed_mode(self):
        self.controller.command("disarm", 0, DAY)
        self.controller.command("alarm", 0, DAY)
        self.controller.command("stop", 1, DAY)

        self.assertFalse(self.controller.status()["armed"])
        self.assertEqual(self.confirm(), [])

    def test_stop_preserves_audio_fault_suppression(self):
        self.start_siren()
        self.controller.audio_event(self.controller.generation, "failed", 2, DAY)
        self.controller.command("stop", 2.1, DAY)

        self.assertTrue(self.controller.status()["armed"])
        self.assertEqual(self.controller.status()["suppressionReason"], "audio_fault")

        for at in (2.5, 3, 3.5, 4, 4.5):
            self.assertEqual(self.observe(at), [])

    def test_audio_failure_requires_successful_arm(self):
        self.confirm()
        effects = self.controller.audio_event(
            self.controller.generation, "failed", 2, DAY
        )
        self.assertEqual(kinds(effects), ["deactivated"])
        for at in (2.5, 3, 3.5, 4, 4.5):
            self.observe(at, 0)
        self.controller.command("disarm", 5, DAY)
        self.controller.vision_fault("vision_exited", 5, DAY)
        self.controller.command("arm", 5, DAY, "audio_unavailable")
        self.assertEqual(self.controller.status()["suppressionReason"], "audio_fault")
        self.controller.command("arm", 5, DAY)
        self.assertFalse(self.controller.status()["suppression"])

    def test_manual_override_preserves_active_siren(self):
        self.confirm()
        for _ in range(4):
            self.finish()
        effects = self.controller.audio_event(
            self.controller.generation, "started", 2, DAY
        )
        generation = self.controller.generation
        self.assertEqual(kinds(effects), ["activated"])
        self.assertEqual(self.controller.command("alarm", 20, DAY), [])
        self.assertEqual(self.controller.generation, generation)
        for at in (20, 20.5, 21, 21.5, 22):
            self.assertEqual(kinds(self.observe(at, 0)), [])
        self.assertEqual(kinds(self.controller.tick(62, DAY)), ["deactivated"])

    def test_uncertain_presence_waits_before_siren(self):
        self.confirm()

        for _ in range(3):
            self.finish()

        self.observe(2.5, 0)
        self.assertEqual(self.finish(2.5), [])
        effects = self.observe(3)

        self.assertEqual(
            [effect.key for effect in effects if isinstance(effect, PlayKey)], ["alarm"]
        )

    def test_person_returning_during_countdown_can_trigger_siren(self):
        self.confirm()

        for at in (2.5, 3, 3.5, 4, 4.5):
            self.assertEqual(self.observe(at, 0), [])

        for _ in range(3):
            self.finish(4.5)

        self.observe(5)
        effects = self.finish(5)

        self.assertEqual(
            [effect.key for effect in effects if isinstance(effect, PlayKey)], ["alarm"]
        )

    def test_one_negative_frame_does_not_confirm_departure_after_countdown(self):
        self.confirm()

        for _ in range(3):
            self.finish()

        self.observe(2.5, 0)

        self.assertEqual(self.finish(4.5), [])
        self.assertEqual(self.controller.phase, "countdown")

        effects = self.observe(4.6)

        self.assertEqual(
            [effect.key for effect in effects if isinstance(effect, PlayKey)], ["alarm"]
        )

    def test_missing_person_does_not_emit_still_there(self):
        self.confirm()
        self.observe(2.5, 0)
        effects = self.finish(2.5)

        self.assertEqual(kinds(effects), [])
        self.assertEqual(
            [effect.key for effect in effects if isinstance(effect, PlayKey)],
            ["countdown_1"],
        )

    def test_positive_gap_and_stream_change_reset_evidence(self):
        self.observe(0)
        self.observe(0.5)
        self.assertEqual(kinds(self.observe(2)), [])
        self.observe(2.5, stream=2)
        self.observe(3, stream=2)
        self.assertEqual(kinds(self.observe(3.5, stream=2)), [])

    def test_brief_miss_and_two_people_becoming_one(self):
        self.observe(0, 2)
        self.observe(0.5, 0)
        self.observe(1, 2)
        self.observe(1.5, 1)
        self.assertEqual(kinds(self.observe(2, 1)), ["recognized"])

    def start_siren(self, at=0):
        effects = []

        for offset in (0, 0.5, 1, 1.5, 2):
            effects += self.observe(at + offset)

        self.assertEqual(kinds(effects), ["recognized"])

        for _ in range(4):
            self.finish(at + 2)

        return self.controller.audio_event(
            self.controller.generation, "started", at + 2, DAY
        )

    def test_periodic_photos_start_only_after_siren_playback_starts(self):
        self.controller.detection["onAlarmSendPhotoEveryMillisecs"] = 2000
        self.confirm()

        self.assertEqual(self.controller.tick(2, DAY), [])

        for _ in range(4):
            self.finish()

        self.assertEqual(self.controller.tick(2, DAY), [])
        self.assertEqual(
            kinds(
                self.controller.audio_event(
                    self.controller.generation, "started", 2, DAY
                )
            ),
            ["activated"],
        )
        generation = self.controller.generation

        self.observe(3.9)

        self.assertEqual(self.controller.tick(3.9, DAY), [])

        for at in (4, 6):
            self.observe(at)
            effects = self.controller.tick(at, DAY)

            self.assertEqual(kinds(effects), ["alarm_photo"])
            self.assertEqual(effects[0].incident_id, self.controller.incident_id)
            self.assertIs(effects[0].frame, self.controller.frame)
            self.assertEqual(effects[0].frame.captured_at, at)
            self.assertEqual(self.controller.generation, generation)
            self.assertEqual(self.controller.tick(at, DAY), [])

    def test_periodic_photos_need_new_fresh_frames_and_do_not_catch_up(self):
        self.controller.detection["onAlarmSendPhotoEveryMillisecs"] = 1000
        self.controller.command("disarm", 0, DAY)
        self.controller.command("alarm", 0, DAY)
        self.controller.audio_event(self.controller.generation, "started", 0, DAY)

        self.assertEqual(self.controller.tick(1, DAY), [])

        self.observe(1, 0)

        self.assertEqual(kinds(self.controller.tick(1, DAY)), ["alarm_photo"])
        self.assertEqual(self.controller.tick(2, DAY), [])
        self.assertEqual(self.controller.tick(4, DAY), [])

        self.observe(10, 0)

        self.assertEqual(kinds(self.controller.tick(10, DAY)), ["alarm_photo"])
        self.assertEqual(self.controller.tick(10.1, DAY), [])

        self.controller.vision_fault("vision_exited", 10.2, DAY)

        self.assertEqual(self.controller.tick(11, DAY), [])
        self.assertTrue(self.controller.manual)

        self.observe(11.5, 0, stream=2)

        self.assertEqual(kinds(self.controller.tick(11.5, DAY)), ["alarm_photo"])

    def test_periodic_photo_schedule_survives_periods_and_manual_override(self):
        self.controller.detection["onAlarmSendPhotoEveryMillisecs"] = 10000
        self.start_siren()
        self.observe(61)

        self.assertEqual(kinds(self.controller.tick(61, DAY)), ["alarm_photo"])

        self.observe(62)

        self.assertEqual(self.controller.tick(62, DAY), [])
        self.assertEqual(self.controller.status()["alarmCycles"], 2)
        self.assertEqual(self.controller.command("alarm", 65, DAY), [])

        self.observe(71)

        self.assertEqual(kinds(self.controller.tick(71, DAY)), ["alarm_photo"])

    def test_periodic_photos_end_on_stop_disarm_fault_or_expiry(self):
        for action in ("stop", "disarm", "audio", "camera", "expiry"):
            with self.subTest(action=action):
                self.setUp()
                self.controller.detection["onAlarmSendPhotoEveryMillisecs"] = 1000
                self.controller.audio["maxAlarmCycles"] = 1
                self.start_siren()
                self.observe(3)

                if action in {"stop", "disarm"}:
                    effects = self.controller.command(action, 3, DAY)
                elif action == "audio":
                    effects = self.controller.audio_event(
                        self.controller.generation, "failed", 3, DAY
                    )
                elif action == "camera":
                    effects = self.controller.vision_fault("camera_unavailable", 3, DAY)
                else:
                    self.observe(62)
                    effects = self.controller.tick(62, DAY)

                self.assertEqual(kinds(effects), ["deactivated"])
                self.assertEqual(self.controller.tick(63, DAY), [])

    def test_periodic_photo_timer_restarts_for_next_alarm(self):
        self.controller.detection["onAlarmSendPhotoEveryMillisecs"] = 2000
        self.start_siren()
        self.observe(4)

        self.assertEqual(kinds(self.controller.tick(4, DAY)), ["alarm_photo"])

        self.controller.command("stop", 4, DAY)
        self.start_siren(5)
        self.observe(8)

        self.assertEqual(self.controller.tick(8, DAY), [])

        self.observe(9)

        self.assertEqual(kinds(self.controller.tick(9, DAY)), ["alarm_photo"])

    def test_three_siren_periods_keep_playback_and_single_activation(self):
        self.assertEqual(kinds(self.start_siren()), ["activated"])
        generation = self.controller.generation

        for at, cycle in ((61.5, 1), (62, 2), (121.5, 2), (122, 3), (181.5, 3)):
            self.observe(at)
            self.assertEqual(self.controller.tick(at, DAY), [])
            self.assertEqual(self.controller.generation, generation)
            self.assertEqual(self.controller.status().get("alarmCycles"), cycle)

        self.observe(182)
        effects = self.controller.tick(182, DAY)

        self.assertEqual(kinds(effects), ["deactivated"])
        self.assertTrue(any(isinstance(effect, StopPlayback) for effect in effects))
        self.assertEqual(self.controller.status()["suppressionReason"], "until_absence")

        for at in (182.5, 183, 183.5, 184, 184.5):
            self.assertEqual(self.observe(at), [])

    def test_single_siren_period_can_be_configured(self):
        self.controller.audio["maxAlarmCycles"] = 1
        self.start_siren()
        self.observe(62)

        self.assertEqual(kinds(self.controller.tick(62, DAY)), ["deactivated"])

    def test_late_tick_does_not_extend_total_siren_budget(self):
        self.start_siren()
        self.observe(190)

        self.assertEqual(kinds(self.controller.tick(190, DAY)), ["deactivated"])
        self.assertEqual(self.controller.status().get("alarmCycles"), 3)

    def test_alarm_budget_resets_after_absence_arm_or_stop(self):
        for reset in ("absence", "arm", "stop"):
            with self.subTest(reset=reset):
                self.setUp()
                self.controller.audio["maxAlarmCycles"] = 1
                self.start_siren()
                self.observe(62)
                self.controller.tick(62, DAY)

                if reset == "absence":
                    for at in (63, 63.5, 64, 64.5, 65):
                        self.observe(at, 0)
                else:
                    self.controller.command(reset, 65, DAY)

                self.assertEqual(kinds(self.start_siren(66)), ["activated"])
                self.assertEqual(self.controller.status().get("alarmCycles"), 1)

    def test_vision_recovery_preserves_used_siren_periods(self):
        self.start_siren()
        self.observe(62)
        self.controller.tick(62, DAY)
        self.controller.vision_fault("vision_exited", 63, DAY)

        self.assertEqual(kinds(self.start_siren(64)), ["activated"])
        self.assertEqual(self.controller.status().get("alarmCycles"), 3)
        self.observe(126)
        self.assertEqual(kinds(self.controller.tick(126, DAY)), ["deactivated"])

    def test_camera_fault_during_last_period_cannot_reset_budget(self):
        self.controller.audio["maxAlarmCycles"] = 1
        self.start_siren()
        self.controller.vision_fault("vision_exited", 3, DAY)
        effects = []

        for at in (4, 4.5, 5, 5.5, 6):
            effects += self.observe(at)

        self.assertEqual(kinds(effects), [])
        self.assertEqual(self.controller.status()["suppressionReason"], "until_absence")

    def test_manual_override_keeps_current_period_deadline(self):
        self.start_siren()
        self.observe(62)
        self.assertEqual(self.controller.tick(62, DAY), [])
        generation = self.controller.generation

        self.assertEqual(self.controller.command("alarm", 70, DAY), [])
        self.assertEqual(self.controller.generation, generation)

        for at in (71, 71.5, 72, 72.5, 73):
            self.observe(at, 0)

        self.assertEqual(self.controller.tick(121.9, DAY), [])
        self.assertEqual(kinds(self.controller.tick(122, DAY)), ["deactivated"])
