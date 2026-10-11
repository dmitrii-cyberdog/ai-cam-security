# Raspberry Pi Camera Alarm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect people on one RTSP camera using a Raspberry Pi 5, play interruptible multilingual warnings and an alarm, and provide authorized Telegram control and incident photos.

**Architecture:** An asyncio parent owns alarm state, audio, and Telegram. One isolated vision process owns OpenCV capture and inference, using a capture thread and a single latest-frame slot so decoding continues during inference. Bounded observations reach the parent; only the parent decides when to play or stop sounds.

**Tech Stack:** Python, Poetry, OpenCV DNN/headless, NumPy, python-telegram-bot, asyncio, unittest, Raspberry Pi OS 64-bit, ALSA `aplay`, systemd.

**Spec:** [Approved camera alarm design](../specs/2026-09-27-camera-alarm-design.md). Read both documents before implementation.

## Implementation status (2026-09-28)

Software implementation and the independent review fix pass are complete in the
existing `beta-deploy` checkout. Hardware acceptance remains pending.

| Tasks | Completed | Remaining |
| --- | --- | --- |
| 1 | Detector, benchmark tool, pinned artifact contracts; both models pass local synthetic inference | Pi day/night measurements, agreed accuracy criteria, final FP32/INT8 selection |
| 2 | Bounded capture, child supervision, restart and shutdown tests | Pi FFmpeg timeout support and measured RTSP latency |
| 3–7 | Audio, alarm policy, event photos, Telegram controls, bounded delivery | Linux timezone/symlink checks and supervised live acceptance |
| 8 | Runtime, readiness, secret redaction and automated integration tests | Hardware acceptance after Tasks 1–2 gates pass |
| 9 | Deployment guide and systemd template committed | Linux unit verification, authorized deployment and supervised acceptance |

Unchecked steps that combine software and hardware work remain open until both
parts are verified. No Pi, recordings, WAV assets or live-service access were
available. See [measurement gates](../../benchmarks/pi5-detection.md) and the
[deployment checklist](../../raspberry-pi.md).

Final local verification: 96 tests run, 95 passed and one skipped because Windows
symlink creation requires permission. Mypy passes for `app` and the benchmark
tool (16 source files); strict Poetry validation, `--check` and `git diff --check`
pass. `--check-ready` correctly exits 1 for the missing camera/Telegram settings,
model, WAV files and `aplay` on this checkout. No live I/O was used.

Implementation adjustments:

- Task 2 uses one fixed 4 MiB shared-memory message slot with a nonblocking lock.
  `multiprocessing.Queue.get_nowait()` can block on an interrupted partial packet;
  the slot preserves latest-only delivery and keeps parent polling responsive.
  Every replacement child receives a new slot.
- The actual pinned NanoDet artifacts expose six outputs at strides 8/16/32;
  decoder fixtures and documentation match both verified artifacts.
- Windows `tzdata` was added in Task 1 because configuration validation already
  needs `ZoneInfo`.
- One daemon encoder performs JPEG work outside the event loop. Its retained
  input/output remains accounted for through cancellation until completion.

## Behavior amendment (2026-09-28)

The user approved these changes before night testing. They supersede the original
departure and single-period requirements in the task checklists below:

- Automatic alarms are allowed whenever armed, day and night. Keep the 2-second
  day / 3-second night confirmation defaults and the 22:00–06:00 night profile.
- Once triggered, finish `first_warning` and all countdown keys even after
  departure. At their end, require a fresh positive detection to start the siren;
  uncertain absence waits for detection or the existing 2-second absence rule.
- A started siren plays until its current 60-second limit, even if the person
  leaves. Confirmed absence ends playback at that boundary.
- Otherwise continue immediately with the siren, up to three total periods
  (`AUDIO_MAX_ALARM_CYCLES=3`). Keep the playback task and single activation
  notification; do not replay warning/countdown keys. Short alarm WAVs repeat
  throughout each period (`AUDIO_MAX_ALARM_SECONDS=60`).
- Stop/disarm and faults still interrupt automatic playback. Manual `/alarm`
  has one period; overriding an automatic siren keeps the current period deadline.
- Returning before a period ends and vision-only recovery preserve periods used.
  Confirmed departure ending the incident or explicit rearming while idle resets
  the allowance. Full restart remains disarmed, including during Internet outages.
- The tracked `.env` now provides shared settings; use ignored local files for
  private overrides. The original `.env.example` instructions are superseded.

See the updated [design](../specs/2026-09-27-camera-alarm-design.md),
[behavior guide](../../../README.md#commands-and-behavior), and
[supervised checklist](../../raspberry-pi.md) for current behavior.

Local verification of this amendment: 114 tests run, 113 passed and one skipped
because Windows symlink creation requires permission. Mypy passes for `app` and
`tools` (16 source files), strict Poetry validation and `git diff --check` pass.
These checks use fake camera/player/Telegram boundaries. Pi night footage and
live speaker acceptance remain pending.

## Global Constraints

- “Run a local person-detection and audio alarm service on a Raspberry Pi 5 with 4 GB RAM and no AI accelerator. Use OpenCV and python-telegram-bot.”
- “Preserve the nested `Dict[str, Any]` configuration.”
- “The initial version supports one camera, one monitored scene, prerecorded WAV audio, and a fixed list of authorized Telegram users.”
- Finish warning/countdown after departure; finish a started siren's current period, then stop on confirmed absence.
- “Keep `cz` as the user's directory label.”
- Day confirmation: 2 seconds. Night confirmation: 3 seconds. Confirmed absence: 2 seconds.
- Maximum fresh-frame age: 2 seconds. Maximum positive-observation gap during confirmation: 1 second. Minimum distinct positive frames: 3.
- Night: 22:00 to 06:00; timezone: Europe/Prague. Freeze the chosen duration for each candidate.
- Alarm period: 60 seconds, up to three total automatic periods. Start disarmed after every full service restart. The parent automatically restarts a crashed or hung vision child while preserving the current armed mode, suppression state and used periods; automatic playback requires fresh confirmation after recovery.
- Audio failure suppresses automatic playback until an explicit `/arm` passes readiness checks. Confirmed absence does not clear this fault suppression.
- Target: at least two fresh inference results per second on the Pi; audio stops within one second after a local stop/disarm decision. These are verification targets, not measured results.
- Retain the existing Python range `>=3.11,<3.15` unless target-platform dependency resolution demonstrates a change is necessary. Verify the Pi's actual interpreter instead of assuming a Windows installation proves ARM64 support.
- Preserve existing comments/blank lines; use exactly two consecutive Python `#` separator lines when separating new logic sections. No unrelated cleanup.
- Real credentials stay in ignored environment overrides; never log credentials or native errors containing them.
- Planning does not install dependencies, play sounds, send Telegram messages, or modify the Pi. Hardware/deployment actions need the authorization required by AGENTS.md.

## Review Focus

1. A queued “warning finished” callback arrives after disarm: it must not start countdown or a siren (Tasks 4, 8).
2. A camera stops delivering frames while showing a person: repeated/stale observations must not count as confirmation or absence, and automatic audio must stop (Tasks 2, 4, 8).
3. Internet recovery delivers old commands: old `/arm` and `/alarm` must not execute; local protection must continue during the outage (Tasks 6, 8).
4. Audio exits with an error immediately after spawning: never emit a successful activated event, and leave no overlapping player (Tasks 3, 8).
5. A photograph waits behind failed Telegram sends: retain its event-time image/timestamp, bound memory, and never block stop/disarm (Tasks 5, 7, 8).

## Execution boundaries and dependencies

This is one integrated service, so keep one plan with independently testable tasks. Work in order for contracts, but Task 1's **hardware measurement** may remain pending while developing mocked Tasks 2–7. Task 8's automatic-audio acceptance and Task 9's deployment require that measurement to pass. If the Pi or footage is unavailable, record that fact; do not invent benchmark results or silently lower the target.

The dependency graph is `1 -> 2`, `1 -> 3`, `1 -> 4`, `2+4 -> 5`, `4+5 -> 6`, `5+6 -> 7`, `2+3+4+5+6+7 -> 8 -> 9`.

For every task: write the named tests, observe the intended failure, implement, then verify. Tests use fakes and temporary files by default; no live camera/audio/Telegram interaction. At a commit checkpoint, stage **only that task's named files** and inspect the staged diff. The workspace already contains earlier refactoring and user work: do not commit or discard it wholesale. No pushes are part of this plan.

## File map

| File | Responsibility |
| --- | --- |
| `app/config/_.py` | Existing environment layering; new nested settings and boundary validation |
| `app/services/models.py` | Small immutable frame, detection, and incident-event records shared by components |
| `app/services/detection.py` | One selected OpenCV DNN detector and its preprocessing/postprocessing |
| `app/services/camera.py` | Sole owner of RTSP capture, latest frame, reconnect |
| `app/services/vision.py` | Isolated capture/inference process and bounded observation bridge |
| `app/services/audio.py` | Asset resolution and one cancellable ALSA playback process |
| `app/services/alarm.py` | Deterministic alarm state machine and effects |
| `app/services/notifications.py` | Event photos, bounded delivery queue, retries |
| `app/services/telegram.py` | PTB lifecycle, authorized commands, transport |
| `app/services/logger.py` | Existing JSON logger; final-output secret redaction |
| `app/runtime.py`, `app/cli.py` | Service composition, effect execution, readiness, shutdown |
| `tools/benchmark_detection.py` | Repeatable recorded-video benchmark, no audio or Telegram |
| `docs/benchmarks/pi5-detection.md` | Actual measured results and selected model contract |
| `deploy/ai-cam-security.service`, `docs/raspberry-pi.md` | Reviewable deployment artifacts and supervised acceptance instructions |

New tests follow existing `app_tests/test_*.py`. Dependencies are added with the task that first uses them. Do not add a database, message broker, framework, plugin system, or face identification.

## Configuration contract

Implement these fields in `load_config(env_name=None, *, root=PROJECT_ROOT) -> Dict[str, Any]`, adding each section in its owning task. Preserve existing `debug`, `server.storageFolder`, `logs.level`, paths, and precedence.

`server.storageFolder` defaults to `volume/` for uploaded/generated content. System assets belong in `system/models/` and `system/sounds/`; resolve their paths independently from the project root. Sample recordings and generated benchmark output remain under `volume/`.

| Environment variable | Dictionary key | Default |
| --- | --- | --- |
| `RTSP_LOGIN` | `camera.login` | admin |
| `RTSP_PASSWORD` | `camera.password` | empty; camera verification code |
| `RTSP_HOST` | `camera.host` | empty; IP address or hostname |
| `RTSP_PORT` | `camera.port` | 554 |
| `RTSP_PATH` | `camera.path` | `/ch1/main` |
| `RTSP_OPEN_TIMEOUT_MS` | `camera.openTimeoutMs` | 5000 |
| `RTSP_READ_TIMEOUT_MS` | `camera.readTimeoutMs` | 2000 |
| `TELEGRAM_BOT_TOKEN` | `telegram.token` | empty; Telegram unavailable until supplied |
| `TELEGRAM_ALLOWED_USER_IDS` | `telegram.allowedUserIds` | empty list; deny all |
| `TELEGRAM_CHAT_ID` | `telegram.notificationChatId` | null until supplied |
| `DETECTION_MODEL_PATH` | `detection.modelPath` | `system/models/person.onnx` |
| `DETECTION_CONFIDENCE` | `detection.confidence` | 0.5, tune during benchmark |
| `DETECTION_DAY_CONFIRM_SECONDS` | `detection.dayConfirmSeconds` | 2 |
| `DETECTION_NIGHT_CONFIRM_SECONDS` | `detection.nightConfirmSeconds` | 3 |
| `DETECTION_ABSENCE_SECONDS` | `detection.absenceSeconds` | 2 |
| `DETECTION_MAX_FRAME_AGE_SECONDS` | `detection.maxFrameAgeSeconds` | 2 |
| `DETECTION_TIMEZONE` | `detection.timezone` | Europe/Prague |
| `DETECTION_NIGHT_START` | `detection.nightStart` | 22:00 |
| `DETECTION_NIGHT_END` | `detection.nightEnd` | 06:00 |
| `AUDIO_SOUNDS_FOLDER` | `audio.soundsFolder` | `system/sounds` |
| `AUDIO_MAIN_LANGUAGE` | `audio.mainLanguage` | cz; required language, played first |
| `AUDIO_LANGUAGES` | `audio.languages` | `en,cz` |
| `AUDIO_SEQUENCE` | `audio.sequence` | `first_warning,countdown_1,countdown_2,countdown_3,alarm` |
| `AUDIO_DEVICE` | `audio.device` | default; Raspberry Pi speaker output |
| `AUDIO_MAX_ALARM_SECONDS` | `audio.maxAlarmSeconds` | 60 |

Keep three minimum frames and the one-second positive gap as named detection constants initially. Avoid extra knobs for every internal queue/retry value.

In Task 2, build `camera.rtspUrl` from the separate RTSP fields in `app/config/_.py`; do not accept a second `RTSP_URL` override. For `admin`, `verification_code`, `192.168.1.100`, `554`, `/ch1/main`, the result is `rtsp://admin:verification_code@192.168.1.100:554/ch1/main`. Percent-encode raw login/password components once, including reserved characters such as `@`, `:`, `/`, `#`, and `%`. Require port 1–65535, a host without scheme/userinfo/path/port, and a path starting with `/`; bracket an IPv6 address when assembling the URL. Leave `rtspUrl` empty when host, login, or password is missing; readiness names missing fields without values. Keep secrets out of logs/status, including raw and encoded credentials. `AUDIO_DEVICE` remains independent of camera connection settings.

The checked-in `.env.example` already contains all planned settings, with blank credentials/host. Update it as each owning task implements the corresponding config section. Its header distinguishes currently supported foundation settings from planned settings. Copy it to ignored `.env.local` or `.env.<environment>.local` for real values.

Use JSON-free comma-separated lists for IDs/languages/sequence. Validate nonempty IDs as integers, language/key names against traversal, numeric settings as finite/in range, timezone with `ZoneInfo`, and HH:MM times. Require first key `first_warning`, last key `alarm`, and unique intermediate keys, so the configurable countdown length has unambiguous phase semantics. Do not require files or network connectivity inside `load_config`.

Missing optional credentials permit a disarmed diagnostic process; malformed supplied settings fail with field names and never secret values. Automatic arming requires a healthy camera/detector and valid audio. Telegram readiness is shown separately and does not prevent existing local protection from operating offline.

### Task 1: Establish the detector contract and measure it on the Pi

**Files:** Create `app/services/models.py`, `app/services/detection.py`, `tools/benchmark_detection.py`, `docs/benchmarks/pi5-detection.md`, `app_tests/test_detection.py`. Modify `pyproject.toml`, `poetry.lock`, `app/config/_.py`, `app_tests/test_config.py`, `.env.example`.

**Interfaces:**

- `Frame(number: int, stream_id: int, captured_at: float, captured_utc: datetime, image: NDArray[np.uint8])`: BGR image; immutable ownership, never mutate after publishing. `stream_id` identifies a successful camera connection; recorded-video benchmarks use 1.
- `Person(confidence: float, box: tuple[int, int, int, int])`: original-frame pixel coordinates, xyxy.
- `Observation(frame: Frame, people: tuple[Person, ...])`.
- `PersonDetector(config: Dict[str, Any])`; `infer(frame: Frame) -> Observation`. Constructor owns one model instance; all calls occur on its owning worker.
- `benchmark(video: Path, model: Path, confidence: float) -> dict[str, object]` in the tools script; CLI takes `--video`, `--model`, `--confidence`, `--output`.

- [x] **1. Write boundary and inference tests.** With a mocked DNN and known output tensors, assert person-only filtering, original-coordinate boxes after letterboxing, confidence threshold boundaries, NMS, and empty detections. Example assertions in `test_infer_preserves_frame_identity`: `self.assertIs(result.frame, frame)`, `self.assertEqual(len(result.people), 1)`, `self.assertEqual(result.people[0].box, (10, 20, 40, 80))`. A model load failure must contain a safe reason, not a configured URL/token.
- [x] **2. Run** `poetry run python -m unittest discover -s app_tests -p test_detection.py -v`. Expected: missing module/API failure before implementation.
- [ ] **3. Implement the detector and benchmark.** Resolve compatible releases of `opencv-python-headless` and `numpy`, install only one OpenCV distribution, and regenerate the lockfile. Benchmark OpenCV Zoo NanoDet FP32 and INT8 variants with the same 416x416 model contract first; copy verified preprocessing/postprocessing from the pinned official example with its required notices. Do not substitute generic YOLO decoding. Use OpenCV CPU backend, no PyTorch runtime, no automatic runtime model downloads. Record artifact source, revision, SHA256, license, person label, normalization, NMS and input shape. Runtime loads only the selected artifact; no multi-model framework.
- [ ] **4. Verify locally, then measure hardware.** Run the detection tests, existing config tests, `poetry run mypy`, and `poetry check --strict`. On the Pi, after access/recordings are available, run `poetry run python -m tools.benchmark_detection --video system/samples/day.mp4 --model system/models/person.onnx --confidence 0.5 --output volume/day-benchmark.json`, then the night equivalent. Use at least 60 seconds per clip, exclude model warmup, record inference/preprocess/postprocess p50/p95 latency, effective rate, CPU, RSS, temperature and throttling. Include a labeled person/empty-scene evaluation and false-positive detection/missed-person counts. Review the initial measurements with the user, then agree and record acceptable false-alarm and missed-person criteria for day and night. Select the variant that meets at least 2 fresh results/second and those agreed accuracy criteria; if neither does, stop this hardware gate and revise the model/resolution with evidence. The hardware gate remains pending until the criteria are agreed and met. Synthetic tests may pass while the hardware gate remains incomplete.
- [x] **5. Record and commit.** Populate the benchmark report only with actual measurements, including Python/OpenCV/OS versions. Keep uploaded/private footage and generated benchmark output under ignored `volume`; store model assets under `system/models`. Commit the reviewed Task 1 changes as `feat: add OpenCV person detector and benchmark`; do not claim the hardware gate passed without results.

### Task 2: Deliver fresh RTSP observations with bounded shutdown

**Files:** Create `app/services/camera.py`, `app/services/vision.py`, `app_tests/test_camera.py`, `app_tests/test_vision.py`. Modify `app/services/models.py`, `app/config/_.py`, `app_tests/test_config.py`, `.env.example`.

**Interfaces:**

- Consumes `Frame`, `Observation`, `PersonDetector.infer` from Task 1.
- `CameraCapture(config: Dict[str, Any])`: `run(stop: threading.Event) -> None`, `latest(after_number: int) -> Frame | None`, `health() -> str`. Only `run` opens, reads, or releases capture.
- `VisionFault(reason: str, at: float)`; `VisionMessage = Observation | VisionFault` in models.
- `VisionWorker(config: Dict[str, Any])`: `start() -> None`, `poll() -> VisionMessage | None`, `async stop() -> None`. Spawn a process with an import-safe module-level target; parent polling is nonblocking. Parent-side supervision detects child exit or stalled progress and automatically replaces the child. Report a `VisionFault` before forwarding observations from a replacement; `stop` also cancels pending restarts.

Task 2 configuration tests: `test_rtsp_url_from_parts` asserts the exact example URL above; `test_rtsp_credentials_are_encoded` uses password `a@b:c/d#e%` and expects `a%40b%3Ac%2Fd%23e%25` in the URL; `test_rtsp_missing_credentials` expects an empty URL; `test_rtsp_invalid_fields` rejects invalid ports/hosts/paths without echoing values. Test IPv6 bracket assembly and the existing env precedence with separate credential fields. Parse `.env.example` and verify its defaults agree with the implemented config sections. Run `test_config.py` alongside the camera/vision tests.

- [x] **1. Write camera/worker failure tests.** `test_latest_replaces_backlog`: after frames 1–100, `self.assertEqual(capture.latest(0).number, 100)` and `self.assertIsNone(capture.latest(100))`. `test_reconnect_does_not_reuse_last_positive`: a read failure gives a fault and no new observation until a newly opened stream produces a frame. `test_hung_capture_is_terminated`: fake a native read that never returns; `await worker.stop()` finishes within its 5-second budget and the child is no longer alive. Add `test_crashed_worker_is_restarted`, `test_hung_worker_is_restarted`, and `test_stop_cancels_pending_restart`; verify fault delivery before replacement observations, bounded restart backoff, old-child cleanup, and no restart after shutdown. Assert old/out-of-order frame numbers never become fresh again.
- [x] **2. Run** each test file with `poetry run python -m unittest discover -s app_tests -p test_camera.py -v` and the equivalent for `test_vision.py`. Expected: missing components before implementation.
- [x] **3. Implement bounded capture.** Use FFmpeg `VideoCapture` with open/read timeouts supplied at open; verify support rather than treating a property assignment as proof. Continuously drain capture on its owning thread, publish only the latest frame, and run inference serially on the child main thread. Resize published frames to at most 1280x720 preserving aspect ratio to bound IPC/photo memory. Use process output queue capacity 1, replacing old observations. Read failures release/reopen with backoff 1, 2, 4, 8, then 10 seconds; reset after success. Frame IDs remain increasing across reconnects within the worker lifetime, and stream_id increments on each successful open; a restarted worker causes the parent to clear evidence. Set receipt timestamps once; never retimestamp a cached image.
- [ ] **4. Verify behavior and real backend latency.** Pass tests; request cooperative stop first, then terminate/kill the whole child within 5 seconds if OpenCV remains blocked. Close IPC handles. Make faults persistent in health and have the parent stale-frame watchdog operate independently of queued faults. Supervise child exit and bounded capture/inference progress independently of observation freshness, allowing for startup, configured I/O timeouts, and reconnect backoff. Replace crashed or hung children with backoff 1, 2, 4, 8, then 10 seconds; reset the backoff after a fresh successful observation. Reap the previous child and replace its IPC queue before starting another, so old results cannot enter a new worker lifetime. Keep Telegram and controller ticks responsive throughout recovery; preserve armed mode and suppression while clearing frame-ID tracking and confirmation evidence. On the Pi, measure visible clock/movement versus delivered frames to expose RTSP backend buffering: receipt time is not the camera's exposure time. Use the camera's low-latency substream and reject the setup for automatic activation if delivered frames already exceed the freshness budget.
- [x] **5. Commit** the reviewed Task 2 files as `feat: add bounded RTSP vision worker`.

### Task 3: Resolve multilingual sounds and stop playback promptly

**Files:** Create `app/services/audio.py`, `app_tests/test_audio.py`; modify `app/config/_.py`, `app_tests/test_config.py`, `.env.example`.

**Interfaces:**

- `resolve_sequence(config: Dict[str, Any]) -> dict[str, tuple[Path, ...]]`: preserves key/language order; fully validates the configured WAV sequence.
- Effective language order is `mainLanguage` first, then unique entries from `languages` excluding the main language, preserving their listed order. Include the main language even when it is absent from `languages`. Require a nonempty directory-safe `mainLanguage`. For each key, a common file takes precedence and plays once; otherwise the main-language file is required and missing secondary files are skipped per key. Validate every selected WAV file; unreadable/invalid selected files reject readiness.
- `AudioPlayer(config: Dict[str, Any])`: `validate() -> None`; `async play(key: str, *, repeat: bool = False, on_started: Callable[[], None] | None = None) -> None`; `async stop() -> None`.
- `play` permits one active call; raises a safe audio error on spawn/nonzero exit. `stop` is idempotent and prevents subsequent files/repeats. The caller owns the playback task.

- [x] **1. Write asset and subprocess tests.** With default main language `cz`, `test_common_once_language_order` asserts the flattened result equals `["cz/first_warning.wav", "en/first_warning.wav", "cz/countdown_1.wav", "en/countdown_1.wav", "cz/countdown_2.wav", "en/countdown_2.wav", "cz/countdown_3.wav", "en/countdown_3.wav", "common/alarm.wav"]`. Test that a common warning overrides both language warnings even if the Czech file is absent. `test_missing_secondary_language_is_skipped` removes `en/countdown_1.wav` and asserts that key resolves to only `cz/countdown_1.wav`; readiness still succeeds. `test_missing_main_language_rejects_readiness` removes `cz/countdown_1.wav` and expects a readiness error naming that file even when the English file exists. `test_main_language_first` verifies Czech-then-English order with default main language `cz` and languages `["en", "cz"]`, then explicitly selects main language `en` and verifies English-then-Czech order. Test main language absent from the list, duplicate languages played once, invalid selected WAVs, and traversal/symlink escapes. Config tests assert default `mainLanguage == "cz"` and reject empty/unsafe main-language values. With a fake player, assert `self.assertEqual(spawn_count_after_stop, spawn_count_before_stop)` and that terminate, then kill if necessary, completes within one second. An immediate nonzero exit must leave `started_calls == 0`.
- [x] **2. Run** `poetry run python -m unittest discover -s app_tests -p test_audio.py -v`; expect missing resolver/player.
- [x] **3. Implement playback.** Add `AUDIO_MAIN_LANGUAGE` to `load_config` as `audio.mainLanguage`, defaulting to `cz`. Resolve common/main/secondary files using the interface rules above before arming; validate readable WAV headers for all selected files with the standard library. Use `asyncio.create_subprocess_exec("aplay", "-q", "-D", device, path)`; no shell strings. Suppress raw device stderr in favor of a safe exit-code error. For siren startup, allow a 100 ms startup observation: an immediate nonzero exit fails before `on_started`; successful process launch cannot prove speaker audibility, which the supervised hardware check must verify. Repeat the alarm key's resolved files only while requested; controller owns the 60-second duration. Terminate the active process, wait at most 0.5 seconds, then kill and reap with the remaining stop budget.
- [x] **4. Verify** audio and config tests without speakers. On Windows use injected/mock subprocess behavior; no ALSA requirement for unit tests. Document user-supplied WAV assets and explicit hardware playback testing in the later deployment task.
- [x] **5. Commit** as `feat: add cancellable multilingual audio sequence`.

### Task 4: Implement deterministic confirmation and incident state

**Files:** Create `app/services/alarm.py`, `app_tests/test_alarm.py`. Modify `app/services/models.py`, `app/config/_.py`, `app_tests/test_config.py`, `.env.example`, `pyproject.toml`, `poetry.lock`.

**Interfaces:**

- `IncidentEvent(incident_id: int, kind: str, phase: str, reason: str, at: float, at_utc: datetime, frame: Frame | None)` in models. Kind is one of `recognized`, `still_there`, `activated`, `deactivated`; frame is frozen at the transition.
- Effects in alarm: `PlayKey(incident_id: int, generation: int, key: str, repeat: bool)`, `StopPlayback(generation: int)`; `AlarmEffect = PlayKey | StopPlayback | IncidentEvent`.
- `AlarmController(config: Dict[str, Any])`: synchronous `observe(observation: Observation, now: float, utc_now: datetime) -> list[AlarmEffect]`; `tick(now: float, utc_now: datetime) -> list[AlarmEffect]`; `command(name: str, now: float, utc_now: datetime, readiness_error: str | None = None) -> list[AlarmEffect]`; `audio_event(generation: int, event: str, now: float, utc_now: datetime) -> list[AlarmEffect]`; `status() -> Dict[str, Any]`.
- `vision_fault(reason: str, now: float, utc_now: datetime) -> list[AlarmEffect]` resets evidence and stops an automatic incident with a camera/detector fault reason. A changed frame `stream_id` also resets evidence, even if a reconnect fault message was overwritten.
- Audio callback event names: `started`, `finished`, `failed`. Status includes armed, phase, suppression and its reason, last command result, and active day/night profile. Distinguish suppression until confirmed absence from audio-fault suppression requiring a successful `/arm`. No network, files, subprocesses, or sleeps in the controller.

- [x] **1. Write timed policy tests.** `test_day_confirmation`: fresh positives at 0, 0.5, 1, 1.5, 2 seconds emit exactly one recognized event at 2; `test_night_confirmation` emits none at 2 and one at 3. Assert repeated frame 1 for three seconds emits none. For day/night midnight and DST tests, local 22:00 is night and 06:00 day; a candidate crossing 22:00 retains its original duration. Require fresh negatives spanning 2 seconds for absence; one brief miss cannot stop playback.
- [x] **2. Add transition assertions, then run** `poetry run python -m unittest discover -s app_tests -p test_alarm.py -v`. Tests: `test_disarm_ignores_late_audio_finish` asserts no `PlayKey` after a previous-generation callback; `test_departure_in_every_phase` yields one stop and one deactivated event; `test_camera_stale_is_not_absence` resets evidence and cancels automatic playback with reason `camera_unavailable`; `test_manual_alarm_has_no_person_events` expects only activated/deactivated; `test_stop_suppresses_until_absence` leaves armed true but emits no new incident from continued positives. Expected initial failure: controller does not exist.
- [x] **3. Implement the state machine.** Consume only increasing fresh frame IDs. Confirmation requires the configured duration, at least 3 positive frames, and no gap greater than 1 second. A fresh negative breaks a positive run but tolerable brief misses may preserve a candidate until the positive gap exceeds 1 second. Confirm absence from distinct fresh negatives spanning 2 seconds with no positive and no gap greater than 1 second. Missing observations trigger the independent 2-second freshness timeout, not absence. Most recent fresh observation must be positive to start the next phase; otherwise wait for positive recovery, confirmed absence, or camera timeout. Freeze event frames by ownership, retain UTC captions, and use monotonic durations.
- [ ] **4. Complete and verify edge cases.** Warning completion emits still-there once and advances through remaining countdown keys; siren emits activated only on its `started` callback. A `failed` callback stops the incident; failure before startup emits no activated event, and later failure does not emit another activated event. Audio failure in either mode suppresses automatic playback until an explicit `/arm` passes readiness checks; confirmed absence, stop/disarm, or vision recovery must not clear this fault suppression. Add `test_audio_failure_requires_successful_arm` covering continued presence, absence and return, failed rearming, and successful rearming. Increment generation before cancellation so late callbacks are ignored. Siren expiry at 60 seconds and manual stop suppress retrigger until confirmed absence; a successful `/arm` explicitly clears suppression but does not restart an already active incident. Repeated `/alarm` during a manual siren is idempotent. During an automatic warning/countdown it switches that incident to a manual siren, cancelling the previous generation without inventing a recognition event. During an already playing automatic siren, switch only its mode to manual: preserve the incident, playback task, generation, and original 60-second deadline, and emit no second activated event. Add `test_manual_override_preserves_active_siren` to verify uninterrupted playback, one activated event, independence from subsequent person absence, and expiry at the original deadline. Disarm always stops both modes. At least one remaining person keeps the scene occupied. Add the tzdata dependency with a Windows platform marker when introducing ZoneInfo, and verify the OS timezone database on Linux. Pass all policy/config tests and mypy.
- [x] **5. Commit** as `feat: add day night alarm state machine`.

### Task 5: Prepare event-time photos and readable notifications

**Files:** Create `app/services/notifications.py`, `app_tests/test_notifications.py`; modify `app/services/models.py`.

**Interfaces:**

- Consumes Task 4 `IncidentEvent` and the event-owned Task 1 `Frame`.
- `Notification(incident_id: int, kind: str, occurred_at: float, text: str, photo: bytes | None)` in models.
- `prepare_notification(event: IncidentEvent, max_frame_age: float) -> Notification`: pure transformation; called off the event loop because encoding may block.
- Photo freshness compares `event.at` with `event.frame.captured_at`, not eventual delivery time.

- [x] **1. Write exact event/attachment tests.** For the four event kinds, assert attachment presence is `[True, False, True, True]`. `test_delayed_send_keeps_event_photo` changes the camera's latest image after preparing an event and asserts `self.assertEqual(notification.photo, original_event_jpeg)`. `test_stale_photo_is_labelled` asserts caption includes `"Latest available image"` and the capture UTC timestamp. With `frame=None`, assert `photo is None` and `"Photo unavailable"` appears. Deactivation during warning must include both `"warning"` and its reason.
- [x] **2. Run** `poetry run python -m unittest discover -s app_tests -p test_notifications.py -v`; expect absent notification preparation.
- [x] **3. Implement the caption and JPEG boundary.** Use the exact leading texts “Person recognized”, “Person is still there”, “Alarm activated”, “Alarm deactivated”. Add incident ID, UTC event time, and phase/reason where relevant. Keep captions below 1024 characters; JPEG-encode the already bounded event image with quality 80. Do not save incident photos to disk. A failed encoding produces a text notification explaining unavailable photo; never raises back into alarm control. No photo is attached to still-there.
- [x] **4. Verify** deterministic byte/caption assertions with synthetic images, plus the existing tests. Keep the event's frame reference immutable from publication until encoding completes. Captions contain no RTSP URL, bot token, or unnecessary user identifiers.
- [x] **5. Commit** as `feat: prepare incident photos and Telegram captions`.

### Task 6: Add authorized Telegram commands and managed polling

**Files:** Create `app/services/telegram.py`, `app_tests/test_telegram.py`; modify `pyproject.toml`, `poetry.lock`, `app/config/_.py`, `app_tests/test_config.py`, `.env.example`.

**Interfaces:**

- Consumes `Notification`; runtime later supplies `execute: Callable[[str], Awaitable[str]]` and `get_status: Callable[[], Dict[str, Any]]`.
- `TelegramService(config: Dict[str, Any], execute, get_status)`: `async run(stop: asyncio.Event) -> None`, `async send(notification: Notification) -> None`, `health() -> str`.
- Registered commands: `/status`, `/arm`, `/disarm`, `/alarm`, `/stop`, `/help`. Handler passes only the normalized command name to execute; status/help do not access frames.
- `run` owns Application/Updater lifecycle and reconnect; it must not own the application's event loop or signal handlers. `send` raises safe transport errors for Task 7 to classify.

- [x] **1. Write authorization and stale-command tests.** An unknown user, missing user, non-private chat, or edited message gives `execute.assert_not_awaited()`. Known private user: `execute.assert_awaited_once_with("disarm")`. Repeated update ID invokes execute once. A message older than 30 seconds or predating the current polling-session cutoff never executes, even if received immediately after reconnect. Assert tokens/RTSP URLs are absent from status/replies.
- [x] **2. Run** `poetry run python -m unittest discover -s app_tests -p test_telegram.py -v`; expect missing TelegramService. Use fake Application/Bot objects, no real token.
- [x] **3. Implement PTB integration.** Resolve a supported stable `python-telegram-bot` release compatible with the target Python and lock it. Use `Application.initialize()`, `Application.start()`, `Updater.start_polling(drop_pending_updates=True, allowed_updates=["message"])`. Shutdown in reverse operational order: updater stop, application stop, application shutdown. Use bounded concurrent update handling (8) so one slow reply does not serialize every command; dispatch control before awaiting its network reply. Keep each reply's network timeout at 5 seconds. All command state mutation still happens on the runtime's event loop.
- [x] **4. Implement and verify recovery.** On a polling transport error, signal a supervisor that stops/restarts the updater with pending updates dropped and refreshes the UTC command cutoff. Do not stop the updater recursively inside its own error callback. Apply backoff 1, 2, 4, 8, then 10 seconds. Reject messages before the cutoff, older than 30 seconds, or more than 5 seconds in the future; keep processed update IDs in a bounded 1024-entry set/deque. Invalid credentials are a Telegram fault, not an infinite fast retry. An offline initial start leaves the local runtime alive. Test partial-start cleanup, shutdown ordering, retry delay, and a blocked reply concurrent with disarm. `send` uses `send_photo(photo=bytes, caption=text)` or `send_message` as appropriate; it never changes alarm state.
- [x] **5. Commit** as `feat: add authorized Telegram alarm controls`.

### Task 7: Deliver notifications without blocking local protection

**Files:** Modify `app/services/notifications.py`, `app_tests/test_notifications.py`; create `app_tests/test_notification_delivery.py`.

**Interfaces:**

- Consumes Task 5 `prepare_notification` and Task 6 `TelegramService.send`.
- `NotificationWorker(send: Callable[[Notification], Awaitable[None]], max_frame_age: float)`: `enqueue(event: IncidentEvent) -> None`, `async run(stop: asyncio.Event) -> None`, `health() -> Dict[str, Any]`.
- `enqueue` is nonblocking, called on the event loop. One worker prepares and sends in FIFO order.

- [x] **1. Write queue and retry tests.** Enqueue recognized, still-there, activated, deactivated and assert send kind order equals that list. A retry does not allow later events to overtake it. With a never-returning sender, repeatedly enqueue and assert `queued_count <= 20` and `retained_image_bytes <= 32 * 1024 * 1024`, including the in-flight frame/encoded photo. Assert an event aged over 120 seconds is suppressed and increments the dropped count. Simulate recovery and assert one summary of dropped events is sent.
- [x] **2. Run** `poetry run python -m unittest discover -s app_tests -p test_notification_delivery.py -v`; expect missing worker. Use fake time/sender; no real waiting or Telegram calls.
- [x] **3. Implement bounded delivery.** Capacity is 20 events and 32 MiB retained image/encoded data, TTL 120 seconds, send deadline 10 seconds. Count in-flight retention too; evict oldest pending events until the new event fits, or drop the new event if the in-flight item prevents admission. A single encoding job runs off-loop at a time. After three send attempts, drop and count the event. Retry transient failures after 1 then 2 seconds; honor Telegram RetryAfter when it fits the remaining TTL. Classify BadRequest/Forbidden/invalid credentials before generic NetworkError and do not retry permanent failures.
- [x] **4. Verify overload and recovery behavior.** Do not block the controller or create an unbounded set of encoding tasks. Preserve FIFO for retained events. Once connectivity recovers, send one text summary of drops, then continue; reset the summary counter only on successful summary delivery. Keep the summary bounded and subject to the same transport deadlines. Uncertain send outcomes may create duplicates; include the incident ID/time and do not claim exactly-once delivery. Test cancellation during encoding/retry/send, recovery summary failure, and that an offline sender does not delay an independent local stop decision.
- [x] **5. Commit** as `feat: bound notification retries and backlog`.

### Task 8: Compose the service and verify complete incidents

**Files:** Create `app/runtime.py`, `app_tests/test_runtime.py`; modify `app/cli.py`, `app/services/logger.py`, `app_tests/test_startup.py`, `app_tests/test_logger.py`, `app/config/_.py`, `README.md`.

**Interfaces:**

- Consumes all previous component interfaces without changing their contracts.
- `Runtime(config: Dict[str, Any])`: `async run(stop: asyncio.Event) -> None`, `async execute(name: str) -> str`, `status() -> Dict[str, Any]`.
- `check_readiness(config: Dict[str, Any]) -> list[str]`: safe offline diagnostic errors for configured model/audio/secrets/backend availability. Never starts capture, plays a sound, or contacts Telegram.
- Extend `configure_logging(level="INFO", *, stream=None, secrets: Sequence[str] = ()) -> None`. Preserve the existing Logger API and existing stream tests.

- [x] **1. Write a complete fake incident test.** Feed fresh positives over the day window, finish both warning languages, finish countdown keys, report siren startup, then fresh negatives over the absence window. Assert event kinds equal `["recognized", "still_there", "activated", "deactivated"]`, attachment policy is correct, and playback order matches Task 3. Add an early-departure test expecting only recognized/deactivated and no siren.
- [x] **2. Write lifecycle/race tests, then run** `poetry run python -m unittest discover -s app_tests -p test_runtime.py -v`. Assert disarm followed by a stale finished callback leaves `armed == False`, no active audio, and no new play; camera loss cancels automatic playback; two people becoming one does not count as absence; manual alarm works while disarmed; Telegram unavailable at initial start does not kill camera/detection; after every simulated full service restart the mode is disarmed. Add `test_vision_restart_preserves_armed_mode`: child crash/hang cancels automatic audio, preserves armed mode and suppression, rejects old-child results, and requires a complete fresh confirmation after recovery. Verify disarm during recovery remains effective when the replacement becomes ready. Cover manual override of an active siren without respawn, timer extension, or duplicate activation, and audio-fault suppression persisting through confirmed absence and vision recovery until successful `/arm`. Shutdown must work from every partially started component state, including pending worker recovery.
- [x] **3. Implement effect execution and health.** Replace the idle body in `app.cli.main` with Runtime lifecycle while preserving signal restoration. Poll vision nonblocking, pass faults to controller.vision_fault, and tick the controller every 100 ms. Execute all StopPlayback effects before notifications or new playback; increment/invalidate generations synchronously before any awaited cleanup. Audio is an independently tracked task; completion callbacks carry their generation. Do not await Telegram delivery in execute/observe/tick. Re-check generation after asynchronous stop before starting a replacement key. Fill safe status with camera, detector, audio, Telegram, and notification queue health. Missing model or required audio leaves control available but arming refused; stale camera requires fresh confirmation after recovery.
- [x] **4. Complete operational checks and secret protection.** Existing `--check` remains offline and validates supplied configuration/storage; report unconfigured components clearly. Add `--check-ready` to require complete credentials/model/audio/backend assets and exit nonzero on missing readiness, still without live I/O. Update startup tests to use temporary model/audio fixtures or mocks for readiness; preserve the existing offline check behavior. Redact configured RTSP URL, decoded credentials, and bot token in the **final formatted output**, including nested context and exception strings. Capture third-party logs through an equivalent redacting handler without duplication; keep HTTP client logs at WARNING. Do not forward native OpenCV stderr verbatim: redirect it in the isolated vision process and emit safe fault codes. Add tests with URL/token strings in tracebacks and nested context asserting `self.assertNotIn(secret, output)`.
- [x] **5. Verify integration and commit.** Run `poetry run python -m unittest discover -s app_tests -v`, `poetry run mypy`, `poetry check --strict`, `poetry run ai-cam-security --check`, and `git diff --check`. Stop playback before worker/network teardown; budget 1 second for audio, 5 seconds for vision, and 5 seconds for Telegram cleanup, with notification drain limited to 2 seconds. Log any unsent count; do not hang shutdown. Hardware-gated acceptance requires Task 1 measurement plus Task 2 latency verification before any authorized real automatic-audio test. Commit as `feat: integrate camera alarm service lifecycle`.

### Task 9: Prepare Raspberry Pi deployment and supervised acceptance

**Files:** Create `deploy/ai-cam-security.service`, `docs/raspberry-pi.md`; update `README.md`, `.env.example`, `docs/benchmarks/pi5-detection.md` with verified operating instructions/results.

**Interfaces:**

- Uses installed `ai-cam-security` entry point and `--env prod`.
- Proposed installation root: `/opt/ai-cam-security`; dedicated account: `ai-cam-security`; local environment file: `/opt/ai-cam-security/.env.prod.local`.
- systemd template runs `/opt/ai-cam-security/.venv/bin/python -m app --env prod` from that root, with an unprivileged user and audio-device access. This is a file to review, not an instruction to modify infrastructure now.

- [x] **1. Write the deployment checklist with exact expected results.** Include OS architecture/interpreter, FFmpeg backend support, installed model hash, WAV resolution/order, ALSA device selection, timezone data, credentials file permissions, and benchmark report location. Include `poetry run ai-cam-security --check-ready`: complete setup exits 0; a missing required WAV/model/credential exits nonzero naming the missing field/file without values; a missing secondary-language WAV still permits readiness when every key has its common or main-language file.
- [x] **2. Create the systemd template and installation instructions.** Configure `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=20`, `KillMode=control-group`, and stdout/stderr to journald. Do not use `PrivateDevices=true`, which would hide audio hardware. Document the selected ALSA device and user/group permissions rather than relying on an interactive desktop session. Document OS-provided timezone data and the Windows tzdata dependency introduced in Task 4. Keep the existing PM2 config as an alternative; never run both managers for one camera/bot.
- [ ] **3. Validate artifacts without deployment.** On a Linux/Pi host use `systemd-analyze verify deploy/ai-cam-security.service`; explain any path/account checks that require the prepared installation. Re-run the local verification commands from Task 8 after documentation/config examples are finalized. Commit only the reviewed artifacts as `docs: add Raspberry Pi deployment and acceptance guide`.
- [ ] **4. Request authorization for concrete live checks/deployment.** Provide the reviewed unit/environment paths and the exact planned camera, speaker, Telegram and systemd actions. The user supplies recordings, audio assets, bot/RTSP secrets through local env files, and Pi access. Do not ask for credentials to be pasted into chat, do not fabricate success, and do not enable the service before authorization.
- [ ] **5. Perform supervised acceptance when authorized.** Install/start disarmed; verify status and authorized/unauthorized access. Arm and walk into view by day and at night; confirm timing, language order, all four event types and photos. Leave during warning, each countdown key, and siren: playback must stop after confirmed absence, without waiting for Telegram. Check stop/disarm while audio is active, manual siren timeout, manual override preserving an active siren's deadline and single activation notification, audio failure requiring successful `/arm`, camera disconnect/reconnect, vision-child crash/hang recovery preserving armed mode, Telegram outage/recovery, full service restart and reboot starting disarmed. Measure local decision-to-silence latency (target <=1 second), verify no stale command replay, bounded memory and no leaked processes. Record results and any failures before unattended operation.

## Self-review and completion criteria

- Spec coverage: detection/hardware -> Tasks 1–2; configuration -> owning Tasks 1–4/6; audio -> Task 3; state and day/night timing -> Task 4; photo events -> Task 5; authorization and polling -> Task 6; outage delivery -> Task 7; lifecycle, secrets and CLI -> Task 8; Pi operation -> Task 9.
- Contract consistency: Frame/Observation originate in Task 1; IncidentEvent/effects in Task 4; Notification in Task 5. Downstream tasks consume those exact names. No component changes controller state outside its event loop.
- Each Review Focus item has a named failure test in its owning task and integration coverage.
- A unit-test pass does not prove DNN recognition quality, RTSP latency, speaker audibility, or Telegram connectivity. Hardware results are reported separately.
- Completion means all automated checks pass, the measurement gate is documented, and authorized supervised acceptance succeeds. If hardware access is unavailable, distinguish code-complete from hardware-verified.
- Application code and dependencies remain unchanged during creation of this plan.

## Documentation used for planning

- [OpenCV Zoo benchmark procedure and model list](https://github.com/opencv/opencv_zoo/blob/main/benchmark/README.md): candidate selection and measured validation; published timings are not assumed to match this Pi.
- [OpenCV NanoDet model and examples](https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_nanodet): inspect and pin the artifact/example contract during Task 1.
- [OpenCV VideoCapture properties](https://docs.opencv.org/4.13.0/d4/d15/group__videoio__flags__base.html): open/read timeouts are backend-dependent.
- [PTB asyncio lifecycle](https://github.com/python-telegram-bot/python-telegram-bot/wiki/Frequently-requested-design-patterns): explicit lifecycle beside other asyncio services.
- [PTB Application and polling API](https://github.com/python-telegram-bot/python-telegram-bot/blob/master/telegram/ext/_application.py): verify signatures against the version locked during implementation.

## Execution handoff

Review this plan before implementation. Recommended: **subagent-driven**, with a fresh implementer/reviewer per task, because the timing, cancellation, and inter-component contracts deserve independent checks. Alternative: **native execution**, one implementing agent followed by a whole-change review. No agents are dispatched and no implementation begins merely by saving this plan.
