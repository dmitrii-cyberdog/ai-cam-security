import asyncio
import re
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, Dict

from app.services.logger import logger

#
#


def resolve_sequence(config: Dict[str, Any]) -> dict[str, tuple[Path, ...]]:
    settings = config["audio"]
    root = Path(settings["soundsFolder"]).resolve()
    languages = list(dict.fromkeys([settings["mainLanguage"], *settings["languages"]]))
    keys = settings["sequence"]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in [*languages, *keys]):
        raise ValueError("Invalid audio asset name")
    result = {}
    for key in keys:
        common = root / "common" / f"{key}.wav"
        candidates = [common] if common.exists() else [root / language / f"{key}.wav" for language in languages]
        selected = []
        for index, candidate in enumerate(candidates):
            relative = candidate.relative_to(root).as_posix()
            if not candidate.resolve().is_relative_to(root):
                raise ValueError(f"Audio asset outside sound root: {relative}")
            if not candidate.exists() and index > 0:
                continue
            try:
                with wave.open(str(candidate), "rb") as sound:
                    if sound.getnframes() <= 0 or sound.getcomptype() != "NONE":
                        raise ValueError("Invalid WAV")
                    sound.readframes(1)
            except (OSError, EOFError, wave.Error, ValueError):
                raise ValueError(f"Missing or invalid audio asset: {relative}") from None
            selected.append(candidate.resolve())
        result[key] = tuple(selected)
    return result


#
#


async def _drain_stderr(stream: asyncio.StreamReader | None, tail: bytearray) -> None:
    if stream is None:
        return

    while chunk := await stream.read(4096):
        tail.extend(chunk)
        del tail[:-4096]


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return

    try:
        process.terminate()
    except ProcessLookupError:
        pass

    try:
        await asyncio.wait_for(process.wait(), 0.5)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass

        await asyncio.wait_for(process.wait(), 0.4)


async def _finish_stderr(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return

    try:
        await asyncio.wait_for(task, 0.5)
    except asyncio.TimeoutError:
        pass


#
#


class AudioPlayer:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._assets: dict[str, tuple[Path, ...]] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._playing = False
        self._stderr_task: asyncio.Task[None] | None = None
        self._cancel = asyncio.Event()
        self._stop_lock = asyncio.Lock()

    def validate(self) -> None:
        self._assets = resolve_sequence(self.config)

    async def set_volume(self) -> None:
        volume = self.config["audio"]["volume"]

        if volume is None:
            return

        device = self.config["audio"]["device"]
        mixer = ["amixer"]

        if device != "default":
            card = re.search(r"(?:^|[:,])CARD=([^,]+)", device)

            if card is None:
                raise ValueError("AUDIO_VOLUME requires AUDIO_DEVICE=default or a device with CARD=")

            mixer.extend(["-c", card.group(1)])

        tail = bytearray()
        process = None
        drain = None
        failure = ""
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *mixer, "-q", "sset", "PCM", "playback", f"{volume}%",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        ))

        try:
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                process = await spawn
                drain = asyncio.create_task(_drain_stderr(process.stderr, tail))
                raise

            drain = asyncio.create_task(_drain_stderr(process.stderr, tail))
            code = await asyncio.wait_for(process.wait(), 2)

            if code != 0:
                failure = f"Audio volume command failed (exit {code})"
        except FileNotFoundError as error:
            failure = f"Audio volume backend unavailable: amixer: {error}"
        except asyncio.TimeoutError:
            failure = "Audio volume command timed out"
        except OSError as error:
            failure = f"Audio volume command failed: {error}"
        finally:
            try:
                if process is not None:
                    await _terminate(process)
            finally:
                await _finish_stderr(drain)

        if failure:
            detail = tail.decode("utf-8", errors="replace").strip()

            raise ValueError(f"{failure}; device={device}; PCM volume={volume}%: {detail}")

    async def play(
        self,
        key: str,
        *,
        repeat: bool = False,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        if self._playing:
            raise ValueError("audio_busy")
        if self._assets is None:
            self.validate()

        assert self._assets is not None

        self._playing = True
        self._cancel = asyncio.Event()
        reported = False

        try:
            while not self._cancel.is_set():
                for path in self._assets[key]:
                    if self._cancel.is_set():
                        return

                    device = self.config["audio"]["device"]
                    asset = path.relative_to(Path(self.config["audio"]["soundsFolder"]).resolve())
                    context = f"device={device}; key={key}; asset={asset}"
                    tail = bytearray()

                    logger.debug("Audio asset playback requested", {
                        "device": device,
                        "key": key,
                        "asset": asset.as_posix(),
                    })

                    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                        "aplay", "-q", "-D", device, str(path),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    ))

                    try:
                        self._process = await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        self._process = await spawn
                        self._stderr_task = asyncio.create_task(_drain_stderr(self._process.stderr, tail))
                        raise
                    except OSError as error:
                        raise ValueError(f"audio_spawn_failed; {context}: {error}") from None

                    self._stderr_task = asyncio.create_task(_drain_stderr(self._process.stderr, tail))

                    if self._cancel.is_set():
                        return

                    waiter = asyncio.create_task(self._process.wait())

                    try:
                        done, _ = await asyncio.wait({waiter}, timeout=0.1)

                        if done and waiter.result() != 0:
                            if self._cancel.is_set():
                                return

                            await _finish_stderr(self._stderr_task)
                            detail = tail.decode("utf-8", errors="replace").strip()

                            raise ValueError(f"audio_exit_{waiter.result()}; {context}: {detail}")

                        if not reported and not self._cancel.is_set():
                            reported = True

                            if on_started is not None:
                                on_started()

                        code = await waiter

                        if self._cancel.is_set():
                            return

                        if code != 0:
                            await _finish_stderr(self._stderr_task)
                            detail = tail.decode("utf-8", errors="replace").strip()

                            raise ValueError(f"audio_exit_{code}; {context}: {detail}")
                    finally:
                        waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)

                    await _finish_stderr(self._stderr_task)
                    self._stderr_task = None
                    self._process = None

                if not repeat:
                    return

                await asyncio.sleep(0)
        finally:
            await self.stop()
            self._playing = False

    async def stop(self) -> None:
        self._cancel.set()
        async with self._stop_lock:
            process = self._process
            if process is None:
                return

            try:
                await _terminate(process)
            finally:
                await _finish_stderr(self._stderr_task)
                self._stderr_task = None
                self._process = None
