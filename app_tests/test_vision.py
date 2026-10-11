import asyncio
import queue
import time
import unittest
from unittest.mock import Mock, patch

from app.services.models import VisionFault
from app.services.vision import VisionWorker
from app.services.vision import _LatestMessageSlot
from app_tests.test_alarm import config, observation

#
#


def blocked_vision(*args):
    time.sleep(60)


def interrupted_publication(config, output, stop, main_progress, capture_progress):
    output._lock.acquire()
    main_progress.value = -1
    time.sleep(60)


class VisionTests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        worker = VisionWorker({"camera": {"openTimeoutMs": 1, "readTimeoutMs": 1}})
        worker._process = Mock()
        worker._process.is_alive.return_value = False
        worker._queue = Mock()
        worker._queue.get_nowait.side_effect = queue.Empty
        worker._stop = Mock()
        return worker

    async def test_crashed_worker_is_restarted(self):
        worker = self.worker()
        with patch.object(worker, "_launch") as launch, patch("app.services.vision.asyncio.sleep", new=asyncio.sleep):
            worker._restart_delay = 0
            fault = worker.poll()
            self.assertIsInstance(fault, VisionFault)
            await worker._recovery
            launch.assert_called_once()
        await worker.stop()

    async def test_hung_worker_is_restarted(self):
        worker = self.worker()
        worker._process.is_alive.return_value = True
        worker._main_progress = Mock(value=-1000)
        worker._capture_progress = Mock(value=-1000)
        worker._started = -1000
        self.assertEqual(worker.poll().reason, "vision_stalled")
        worker._process.is_alive.return_value = False
        await worker.stop()

    async def test_hung_capture_is_terminated(self):
        worker = self.worker()
        worker._process.is_alive.side_effect = [True, True, False]
        process = worker._process
        await asyncio.wait_for(worker.stop(), 5)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    async def test_stop_cancels_pending_restart(self):
        worker = self.worker()
        with patch.object(worker, "_launch") as launch:
            worker.poll()
            await worker.stop()
            await asyncio.sleep(0)
            launch.assert_not_called()
            self.assertIsNone(worker.poll())

    async def test_real_spawned_hung_process_is_reaped(self):
        worker = VisionWorker(config())
        with patch("app.services.vision._vision_main", blocked_vision):
            worker.start()
        process = worker._process
        started = time.monotonic()
        await worker.stop()
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(worker._process)
        self.assertTrue(process._closed)

    async def test_old_or_out_of_order_frames_are_not_forwarded(self):
        worker = self.worker()
        worker.config = config()
        worker._process.is_alive.return_value = True
        worker._started = time.monotonic()
        worker._queue = queue.Queue()
        now = time.monotonic()
        for item in (observation(3, now), observation(3, now), observation(2, now + 0.1), observation(4, now - 0.1)):
            worker._queue.put(item)
        self.assertEqual(worker.poll().frame.number, 3)
        for _ in range(3):
            self.assertIsNone(worker.poll())
        worker._queue = None
        worker._process.is_alive.return_value = False
        await worker.stop()

    async def test_interrupted_publication_never_blocks_parent(self):
        worker = VisionWorker(config())

        with patch("app.services.vision._vision_main", interrupted_publication):
            worker.start()

        old_slot = worker._queue

        try:
            for _ in range(100):
                if worker._main_progress.value == -1:
                    break

                await asyncio.sleep(0.02)

            self.assertEqual(worker._main_progress.value, -1)
            started = time.monotonic()

            for _ in range(100):
                self.assertIsNone(worker.poll())

            self.assertLess(time.monotonic() - started, 0.2)
            worker._process.terminate()
            await asyncio.to_thread(worker._process.join, 1)
            worker._restart_delay = 0
            self.assertIsInstance(worker.poll(), VisionFault)

            with patch("app.services.vision._vision_main", blocked_vision):
                await worker._recovery

            self.assertIsNot(worker._queue, old_slot)
        finally:
            await asyncio.wait_for(worker.stop(), 5)

    async def test_shared_slot_replaces_backlog_and_preserves_frame(self):
        worker = VisionWorker(config())
        slot = _LatestMessageSlot(worker._context)
        now = time.monotonic()

        for number in range(1, 101):
            slot.put_nowait(observation(number, now))

        received = slot.get_nowait()

        self.assertEqual(received.frame.number, 100)
        self.assertEqual(received.frame.captured_at, now)
        self.assertEqual(received.people[0].box, (1, 1, 5, 5))

        with self.assertRaises(queue.Empty):
            slot.get_nowait()

    async def test_corrupt_message_reports_fault_before_restart(self):
        worker = self.worker()
        worker._process.is_alive.return_value = True
        worker._started = time.monotonic()
        worker._queue = _LatestMessageSlot(worker._context)
        worker._queue._length.value = 1
        worker._queue._buffer[0] = 0

        self.assertEqual(worker.poll().reason, "vision_message_invalid")

        worker._process.is_alive.return_value = False
        await worker.stop()
