import asyncio
import time
import unittest
import threading
import weakref
from unittest.mock import patch

import numpy as np

from app.services.models import Frame, IncidentEvent
from app.services.notifications import NotificationWorker
from app.services.telegram import TransportError
from app_tests.test_alarm import DAY, observation

#
#


def event(kind="recognized", number=1, at=None, large=False):
    at = time.monotonic() if at is None else at
    frame = Frame(number, 1, at, DAY, np.zeros((720, 1280, 3), np.uint8)) if large else observation(number, at).frame
    return IncidentEvent(number, kind, "warning", "person_left", at, DAY, frame)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_loss_and_recovery_use_fifo_text_delivery_with_retry(self):
        sent = []
        stop = asyncio.Event()
        attempts = 0

        async def send(note):
            nonlocal attempts
            attempts += 1

            if attempts == 1:
                raise TransportError("offline", retry_after=0)

            sent.append(note)

            if len(sent) == 2:
                stop.set()

        worker = NotificationWorker(send, 2)

        for kind in ("camera_unavailable", "camera_available"):
            worker.enqueue(IncidentEvent(0, kind, "", kind, time.monotonic(), DAY, None))

        await asyncio.wait_for(worker.run(stop), 2)

        self.assertEqual([note.kind for note in sent], ["camera_unavailable", "camera_available"])
        self.assertTrue(all(note.photo is None for note in sent))
        self.assertEqual(attempts, 3)
        self.assertEqual(worker.health()["dropped_count"], 0)

    async def test_fifo_retry_and_event_time_photo(self):
        sent = []
        stop = asyncio.Event()
        attempts = 0
        async def send(note):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TransportError("offline", retry_after=0)
            sent.append(note.kind)
            if len(sent) == 5:
                stop.set()
        worker = NotificationWorker(send, 2)
        for kind in ("recognized", "still_there", "activated", "alarm_photo", "deactivated"):
            worker.enqueue(event(kind))
        await asyncio.wait_for(worker.run(stop), 2)
        self.assertEqual(sent, ["recognized", "still_there", "activated", "alarm_photo", "deactivated"])

    async def test_bounded_memory_including_inflight(self):
        entered = asyncio.Event()
        async def send(note):
            entered.set()
            await asyncio.Event().wait()
        worker = NotificationWorker(send, 2)
        worker.enqueue(event(large=True))
        task = asyncio.create_task(worker.run(asyncio.Event()))
        await asyncio.wait_for(entered.wait(), 2)
        for number in range(100):
            worker.enqueue(event(number=number, large=True))
            self.assertLessEqual(worker.health()["queued_count"], 20)
            self.assertLessEqual(worker.health()["retained_image_bytes"], 32 * 1024 * 1024)
        self.assertGreater(worker.health()["dropped_count"], 0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_expiry_and_recovery_summary(self):
        sent = []
        stop = asyncio.Event()
        async def send(note):
            sent.append(note.kind)
            if note.kind == "recognized":
                stop.set()
        worker = NotificationWorker(send, 2)
        worker.enqueue(event(at=time.monotonic() - 121))
        worker.enqueue(event())
        await asyncio.wait_for(worker.run(stop), 2)
        self.assertEqual(sent, ["summary", "recognized"])
        self.assertEqual(worker.health()["dropped_count"], 1)

    async def test_permanent_failure_is_not_retried(self):
        calls = []
        stop = asyncio.Event()
        async def send(note):
            calls.append(note.kind)
            stop.set()
            raise TransportError("rejected", permanent=True)
        worker = NotificationWorker(send, 2)
        worker.enqueue(event())
        await asyncio.wait_for(worker.run(stop), 2)
        self.assertEqual(calls, ["recognized"])
        self.assertEqual(worker.health()["dropped_count"], 1)

    async def test_failed_summary_keeps_drop_count(self):
        stop = asyncio.Event()
        async def send(note):
            stop.set()
            raise TransportError("offline", permanent=True)
        worker = NotificationWorker(send, 2)
        worker.enqueue(event(at=time.monotonic() - 121))
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(task, 2)
        self.assertGreater(worker.health()["unreported_drops"], 0)

    async def test_cancelled_encoding_retention_is_accounted_until_finished(self):
        from app.services.notifications import prepare_notification
        entered, release = threading.Event(), threading.Event()
        def slow(event, age):
            entered.set()
            release.wait(2)
            return prepare_notification(event, age)
        async def send(note):
            pass
        worker = NotificationWorker(send, 2)
        worker.enqueue(event(large=True))
        with patch("app.services.notifications.prepare_notification", side_effect=slow):
            task = asyncio.create_task(worker.run(asyncio.Event()))
            try:
                await asyncio.to_thread(entered.wait, 1)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertGreater(worker.health()["retained_image_bytes"], 0)
            finally:
                release.set()
                await asyncio.sleep(0.05)
        self.assertEqual(worker.health()["retained_image_bytes"], 0)

    async def test_delivered_photo_is_released_while_idle(self):
        references = []
        delivered = asyncio.Event()

        async def send(note):
            references.append(weakref.ref(note))
            delivered.set()

        worker = NotificationWorker(send, 2)
        worker.enqueue(event(large=True))
        task = asyncio.create_task(worker.run(asyncio.Event()))

        try:
            await asyncio.wait_for(delivered.wait(), 2)
            await asyncio.sleep(0.01)

            self.assertEqual(worker.health()["retained_image_bytes"], 0)
            self.assertTrue(references[0]() is None, "Delivered photo is still retained")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_completed_encoding_releases_photo(self):
        from app.services.notifications import prepare_notification

        references = []

        def encode(item, age):
            note = prepare_notification(item, age)
            references.append(weakref.ref(note))

            return note

        async def cancel_after_completion(future):
            await future
            del future
            raise asyncio.CancelledError

        async def send(note):
            self.fail("A cancelled encoding must not send")

        worker = NotificationWorker(send, 2)
        worker.enqueue(event())

        with patch("app.services.notifications.prepare_notification", new=encode), patch("app.services.notifications.asyncio.shield", new=cancel_after_completion):
            task = asyncio.create_task(worker.run(asyncio.Event()))
            await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(references[0]() is None, "Completed encoding still retains its photo after cancellation")
