import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import cv2

from app.services.models import IncidentEvent
from app.services.notifications import prepare_notification
from app_tests.test_alarm import DAY, observation

#
#


class NotificationTests(unittest.TestCase):
    def event(self, kind, at=0, frame=True):
        return IncidentEvent(4, kind, "warning", "person_left", at, DAY + timedelta(seconds=at), observation(1, 0).frame if frame else None)

    def test_attachment_policy(self):
        notes = [prepare_notification(self.event(kind), 2) for kind in ("recognized", "still_there", "activated", "deactivated")]
        self.assertEqual([note.photo is not None for note in notes], [True, False, True, True])
        self.assertEqual([note.text.splitlines()[0] for note in notes], ["<b>👤 Person recognized</b>", "<b>👀 Person is still there</b>", "<b>🚨 Alarm activated</b>", "<b>⏹️ Alarm deactivated</b>"])

    def test_camera_availability_notices_are_timestamped_text_without_incident(self):
        for kind, heading in (
            ("camera_unavailable", "⚠️ RTSP camera stream unavailable"),
            ("camera_available", "✅ RTSP camera stream available again"),
        ):
            event = IncidentEvent(0, kind, "", kind, 100, DAY, None)
            note = prepare_notification(event, 2)

            self.assertEqual(note.kind, kind)
            self.assertEqual(note.occurred_at, 100)
            self.assertIsNone(note.photo)
            self.assertIn(f"<b>{heading}</b>", note.text)
            self.assertIn("Time (UTC): 2026-09-27T10:00:00Z", note.text)
            self.assertNotIn("Incident", note.text)
            self.assertNotIn("Photo unavailable", note.text)

    def test_delayed_send_keeps_event_photo(self):
        event = self.event("recognized")
        expected = cv2.imencode(".jpg", event.frame.image, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
        note = prepare_notification(event, 2)
        self.assertEqual(note.photo, expected)
        self.assertEqual(note.occurred_at, 0)

    def test_stale_photo_is_labelled(self):
        note = prepare_notification(self.event("deactivated", 10), 2)
        self.assertIn("Latest available image", note.text)
        self.assertIn("Latest available image (UTC): 2026-09-27T10:00:00Z", note.text)
        self.assertIn("Time (UTC): 2026-09-27T10:00:10Z", note.text)
        self.assertIn("Phase at event: Warning", note.text)
        self.assertIn("Reason: Person left the camera view", note.text)

    def test_periodic_alarm_photo_has_image_and_incident_caption(self):
        event = replace(self.event("alarm_photo"), phase="alarm", reason="alarm_active")
        note = prepare_notification(event, 2)

        self.assertIsNotNone(note.photo)
        self.assertEqual(note.kind, "alarm_photo")
        self.assertEqual(note.incident_id, 4)
        self.assertIn("<b>🚨 Alarm in progress</b>", note.text)
        self.assertIn("Phase at event: Alarm", note.text)
        self.assertIn("Reason: Siren is active", note.text)

    def test_audio_fault_explains_recovery(self):
        event = replace(self.event("deactivated"), reason="audio_fault")
        note = prepare_notification(event, 2)

        self.assertIn("Reason: Audio playback failed", note.text)
        self.assertIn("<i>Check the speaker, then use /arm.", note.text)

    def test_dynamic_labels_are_escaped_and_caption_stays_bounded(self):
        event = replace(self.event("deactivated", 10), phase="<alarm> & " * 1000, reason="<fault> & " * 1000)
        note = prepare_notification(event, 2)

        self.assertIn("&lt;alarm&gt; &amp;", note.text)
        self.assertIn("&lt;fault&gt; &amp;", note.text)
        self.assertNotIn("<alarm>", note.text)
        self.assertLess(len(note.text), 1024)
        self.assertTrue(note.text.endswith("</i>"))

    def test_missing_and_failed_photos_are_text(self):
        self.assertIn("Photo unavailable", prepare_notification(self.event("activated", frame=False), 2).text)
        with patch("app.services.notifications.cv2.imencode", side_effect=RuntimeError("secret")):
            note = prepare_notification(self.event("recognized"), 2)
        self.assertIsNone(note.photo)
        self.assertIn("Photo unavailable", note.text)
        self.assertNotIn("secret", note.text)
