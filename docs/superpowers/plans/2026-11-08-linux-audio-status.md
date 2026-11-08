# Linux Audio Diagnostics Implementation Plan

> Use superpowers:executing-plans and superpowers:test-driven-development. Keep the current checkout and existing user changes. No deployment, host audio configuration changes, live sound tests or commits are requested.

**Goal:** Support Raspberry Pi and Linux system-wide ALSA volume across differing mixer controls and separate audio diagnostics into `/audio_status`.

**Architecture:** Preserve aplay/amixer and the OS-selected default. Read bounded ALSA inspection output asynchronously, choose an advertised playback-volume control, and render audio diagnostics separately from general service status.

**Tech Stack:** Python, asyncio, unittest, ALSA utilities, python-telegram-bot.

**Spec / confirmed design:** The user selected Raspberry Pi/Linux, system-wide volume, separate audio status, and explicitly “Honor Linux default.” No automatic fallback to another speaker or OS reconfiguration is allowed. Earlier macOS/shared-PortAudio ideas are out of scope.

## Behavior

- Empty AUDIO_DEVICE continues to mean ALSA `default`. Explicit CARD devices remain supported. A broken default reports its error; no card names or indices are hardcoded and no other speaker is tried.
- `/status` retains general host/protection/component/notification status and an audio-health summary. Move device, initial/last-applied volume and failure details to `/audio_status`.
- `/audio_status` performs read-only bounded queries as the running account: playback hardware inventory, mixer info, and simple-control contents. Show configured PCM separately from mixer card, because ALSA PCM and control defaults can differ. Label inspection errors independently and avoid claiming device-open or audible success.
- Select only controls advertising playback volume. Prefer `Master`, then `PCM`; otherwise use a single available playback-volume control. Multiple remaining controls are ambiguous and must return their names rather than guessing. Capture-only controls are excluded.
- System-wide `/volume` changes the selected control on the configured/default mixer. Keep initial-at-startup and successful-value reporting semantics. Inspection/read errors preserve alarm/protection state and previous successful volume.
- Show live mixer channel percentages/switch state separately from the last percentage the service requested. Do not imply software amplification or change mute switches implicitly.
- Preserve command authorization/freshness/duplicates, cancellation, suppression and manual-camera independence. No status query plays sound or changes settings.

## Task 1: Bounded ALSA inspection and portable mixer selection

**Files:** `app/services/audio.py`, `app_tests/test_audio.py`.

- [x] Add failing tests for Master, PCM, a sole differently named control, capture-only controls and ambiguity. Verify the selected-card prefix remains consistent with explicit CARD configuration and default remains default.
- [x] Implement bounded read-only command execution with a two-second deadline, continuous stdout/stderr draining, OS/exit/timeout reasons and process cleanup during cancellation. Reject oversized inspection output rather than selecting from a truncated control list.
- [x] Use read-only scontents to select a playback-volume control before the existing mixer write. Preserve explicit percentages, startup behavior, backend failure details and cancellation.
- [x] Add `AudioPlayer.inspect() -> Dict[str, Any]` reporting inventory, mixer info/control/capabilities/percentages/switches and independent inspection errors. Run queries concurrently; do not cache a claim of fresh hardware status.
- [x] Run audio tests, including existing playback subprocess lifecycle coverage.

## Task 2: Audio status command and documentation

**Files:** `app/runtime.py`, `app/services/telegram.py`, `app_tests/test_runtime.py`, `app_tests/test_telegram.py`, README, Pi guide and existing design/plan amendments.

- [x] Add failing tests for general/audio status separation, escaped/redacted bounded inspection output, timeout behavior, menu/help/keyboard and existing authorization/duplicate gates.
- [x] Add async `Runtime.audio_status()` and a Telegram callback for it; retrieve only for `/audio_status`, with a bounded timeout. Leave general status immediate and cached.
- [x] Render configuration, ALSA backend, current mixer data, last requested volume, history and independent errors. Keep configured PCM and default mixer identities distinct.
- [x] Update docs with Linux-only support, control selection, strict default policy, read-only diagnostics and a reviewed example for configuring the Pi's OS default. Do not apply host changes.
- [x] Run focused/full tests, Windows/Linux mypy targets for Python 3.11/3.14, strict Poetry and whitespace checks; request bounded independent review and address substantive findings.

## Review focus

- No automatic speaker switching or system-default changes, including on error 524.
- Playback and mixer defaults may differ; diagnostics must not conflate them.
- Capture-only/ambiguous controls must never be modified.
- Hung or verbose queries must not block stop/disarm, leak processes or leave partial discovery authoritative.
- Audio status must share normal Telegram authorization and redact before truncating external text.

Live Pi device/default/mixer validation remains pending.


## Implementation and verification record

Implemented in the current checkout. The prior Windows getloadavg typing fix
remains present. No commits, deployment, host audio settings changes or live
sound tests were performed.

- `/status` now retains general service/host/protection data and audio-health
  summary. `/audio_status` performs bounded concurrent read-only ALSA queries
  and reports configuration, inventory, mixer identity/control, measured
  playback percentages/switches, last requested volume and failure history.
- ALSA playback and mixer defaults remain distinct in the report. The user
  selected strict Linux default routing; no fallback or USB-specific choice is
  implemented. A Pi-specific OS configuration example is documented separately.
- Mixer selection handles Master, PCM, a sole differently named control and
  common-volume controls. Capture-only/ambiguous controls are rejected without
  mixer writes. Mixed playback/capture readback excludes capture values.
- RED regressions covered hardcoded PCM, absent audio-status callback, common
  volume capabilities, capture readback contamination and combined Unicode
  message overflow. Output is redacted before UTF-16 truncation. Cancellation,
  oversized output and inspection timeouts have process-cleanup tests.
- Independent review found the combined Unicode output bound issue; it was
  independently reproduced and fixed. Final review reported no outstanding
  findings and independently verified the focused suite.
- Focused audio/runtime/Telegram verification: **126 tests passed**.
- Full suite with the established canonical-temp bootstrap: **241 tests passed**.
- Mypy: all four targets passed, Windows/Linux × Python 3.11/3.14, 16 files each.
- Strict Poetry validation and whitespace checks passed. The existing Telegram
  retry_after deprecation warning remains.
- Live Pi mixer/default routing and audible output remain unverified. Inventory
  presence alone does not establish that the original error 524 is resolved.
