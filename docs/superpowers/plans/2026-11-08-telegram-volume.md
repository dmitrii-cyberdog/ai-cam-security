# Telegram Volume Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans and superpowers:test-driven-development. Preserve the current checkout as requested earlier; no commits or deployment are requested.

**Goal:** Apply AUDIO_VOLUME once at service startup and allow authorized `/volume 50` changes without resetting them on `/arm`.

**Architecture:** Reuse the existing amixer subprocess and error reporting. Runtime owns the last successfully applied percentage and serializes mixer writes independently of alarm commands. Telegram forwards the volume argument through the existing command callback.

**Tech Stack:** Python, asyncio, unittest, python-telegram-bot 22, ALSA amixer.

**Spec:** The behavior below is the scoped design for the user's requested initial-volume and Telegram-volume change, supplementing `../specs/2026-09-27-camera-alarm-design.md`.

## Behavior and constraints

- Apply configured AUDIO_VOLUME once before Telegram starts accepting commands. Empty/unset leaves the mixer unchanged. Failure logs/stores the real error and leaves diagnostics running disarmed.
- `/volume <integer>` accepts ASCII integers from 0 through 100, including mute at 0. Missing, extra, noninteger or out-of-range arguments produce usage guidance without touching the mixer.
- Apply the change immediately, including during playback or camera outage. Reply with the percentage only after amixer succeeds. Failure preserves the previous reported percentage and alarm state, returns a redacted reason, and records the failure.
- `/arm` no longer applies volume or requires amixer. Complete offline startup readiness still checks amixer when the initial volume is configured.
- Changes last for this process lifetime; restart reapplies AUDIO_VOLUME. Do not persist to environment files or introduce mixer/device discovery.
- Status labels the initial configuration and last successfully applied percentage separately. Unknown current volume is explicit; no hardware readback is claimed.
- Preserve allowlist/private-chat/freshness/duplicate checks, playback cancellation, suppression and limits. Serialize volume writes so successful status matches the last mixer operation; `/stop` and `/disarm` remain responsive.

## Review focus

- Invalid `/volume` must not alter the mixer or supersede pending alarm commands.
- Mixer errors must preserve prior successful volume and current protection/playback state.
- Concurrent writes must finish in order; stop/disarm cannot wait on the mixer lock.
- Startup initial volume must not be reapplied on arm/reconnect; unset initial volume skips amixer.
- Status and documentation must not claim measured hardware volume or persistence across restarts.

## Task 1: Runtime volume lifecycle

**Files:** `app/services/audio.py`, `app/runtime.py`, `app_tests/test_audio.py`, `app_tests/test_runtime.py`.

- [x] Add failing tests for explicit mixer percentages independent of configured initial value; successful/failed/invalid runtime volume, no camera prerequisite, concurrent writes, and responsive stop/disarm during slow mixer operations.
- [x] Replace tests for the retired per-arm-volume behavior with startup-once/error/unset tests and arm-without-mixer/reset tests. Preserve validation and suppression regression coverage on their current paths.
- [x] Add `AudioPlayer.set_volume(volume: int | None = None) -> None`, defaulting omitted value to the initial config for compatibility. Runtime validates user input before calling it.
- [x] Add runtime `_apply_volume(volume: int) -> str`, using a dedicated mixer lock and committing the reported value only on success. Dispatch `execute("volume 50")` separately from alarm command serial/state transitions.
- [x] Apply initial volume before starting Telegram in `Runtime.run`; remove per-arm mixer execution/recheck. Extend offline `check_readiness(..., require_volume=True)` so runtime arm can disable mixer readiness without weakening CLI startup checks.
- [x] Run audio/runtime tests and verify the old alarm safety and failure-history tests remain green.

## Task 2: Telegram controls and documentation

**Files:** `app/services/telegram.py`, `app_tests/test_telegram.py`, `README.md`, `docs/raspberry-pi.md`, existing design/plan amendments.

- [x] Add failing tests for `/volume 50`, 0, 100, missing/invalid values, authorization/duplicate rejection, help/menu/keyboard discovery, and escaped mixer-failure replies.
- [x] Register volume with the existing commands and keyboard. Forward its arguments to runtime; a no-argument keyboard press shows usage. Leave other command semantics unchanged.
- [x] Update `/status`, help and replies to describe initial and last applied volume. Update docs/specs to supersede per-arm-volume semantics and document reset on restart.
- [x] Run focused tests, full suite with the established macOS canonical-temp bootstrap, mypy, strict Poetry validation, and whitespace checks. Obtain a bounded independent review and fix substantive findings.

Live mixer/speaker verification remains pending and requires Pi access and approval for audible output or deployment.


## Implementation record

Implemented in the current checkout without commits, deployment or live mixer
commands. Current status reports initialAudioVolume separately from the last
successfully applied audioVolume; a fresh runtime does not claim a known current
volume before successful initialization/change. Startup errors leave diagnostics
available. Alarm readiness checks freshness after asynchronous asset validation.

- Focused audio/runtime/Telegram suite: **111 tests passed**.
- Full suite: **225 tests passed** with the canonical temporary-directory
  bootstrap documented in the diagnostics plan (the known macOS symlink-path
  comparison issue remains outside this change).
- `poetry run mypy`: passed, 16 source files.
- `poetry check --strict`: passed.
- `git diff --check`: passed.
- Independent code review: no actionable findings. Documentation was checked
  against the implemented lifecycle and updated to supersede per-arm volume.
- The existing Telegram retry_after deprecation warning remains. Physical ALSA
  volume behavior and deployed Telegram controls have not been tested on the Pi.
