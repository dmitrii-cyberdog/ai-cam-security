# Telegram Status and Audio Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make Telegram status useful for Pi health checks, expose actionable sound failures, verify recent audio defaults, and prove manual sirens work without RTSP.

**Status:** Software implemented after the user approved execution on 2026-11-08. Live Pi acceptance remains pending; no deployment or live sound tests were performed.

The later [Telegram volume plan](2026-11-08-telegram-volume.md) supersedes this
document's per-arm-volume requirements: AUDIO_VOLUME now initializes once at
startup; `/volume 0–100` changes the session volume; `/arm` leaves it unchanged.
Earlier investigation/results below are preserved as historical records.

**Architecture:** Keep the asyncio runtime as the owner of health and alarm state. Capture bounded ALSA diagnostics at the subprocess boundary, report them through the existing JSON logger, and expose a concise last-failure summary through runtime status. Separate audio readiness from automatic-detection readiness; collect lightweight host metrics without a new dependency or monitoring service.

**Tech Stack:** Python 3.11–3.14, asyncio, unittest, Poetry, python-telegram-bot, Linux sysfs/procfs, ALSA aplay/amixer, Raspberry Pi OS/systemd.

**Spec:** The proposed behavior below supplements `../specs/2026-09-27-camera-alarm-design.md`. The user approved execution; it does not replace existing alarm timing, suppression, authorization, or recovery rules.

## Investigation findings

- `app/services/telegram.py:TelegramService._status_reply()` currently renders protection, component health, notification queue statistics, and the last command. No host metrics or last audio exception are present.
- `app/services/audio.py:AudioPlayer.play()` sends aplay stderr to DEVNULL. Spawn failures become `audio_spawn_failed`; playback failures become `audio_exit_<code>`, losing the operating-system/ALSA explanation.
- `AudioPlayer.set_volume()` also discards stderr and collapses mixer errors into generic messages. `Runtime.execute()` returns the generic volume error; `Runtime._play()` catches playback exceptions without logging them.
- Commit `0a6b97d` correctly normalizes missing/empty AUDIO_DEVICE to `default`, with config tests. It also changed tracked volume settings to 25 in `.env` and 100 in `.env.prod`. Consequently `/arm` now attempts the default card's PCM mixer control unless overridden. Whether that card/control exists for the service account is unknown.
- Commit `ef4685b` bundles English/common WAVs and infers the main language. Effective local/process overrides still need checking on the Pi, especially old Czech-language settings. Do not assume the new tracked defaults describe the deployed configuration.
- `Runtime._readiness("alarm")` runs the complete readiness checker, then filters human-readable messages by `audio`/`aplay`. This drops `Volume backend unavailable: amixer`, but also leaves manual readiness coupled to unrelated checks before filtering. Manual `/alarm` intentionally does not apply volume, so missing amixer should not block it.
- `AlarmController.command("alarm")` already permits manual playback without frames. `vision_fault()` and stale-frame handling preserve manual playback; `/arm` requires fresh vision. Existing runtime tests initialize healthy camera/detector state and mock the entire readiness checker, so they do not prove end-to-end readiness during camera outage.
- Baseline: 144 focused tests ran; 141 passed and three config tests failed because macOS temporary paths compare `/var/...` with resolved `/private/var/...`. This is separate from the requested audio work. A rerun using canonical TMPDIR is recorded below.

The deployed audio failure's root cause is **not established**. Default routing, missing PCM controls, permissions, busy devices, and stale asset overrides remain hypotheses until actual backend diagnostics are available.

## Proposed behavior and scope

### Status

Add a compact host-health section with CPU temperature in °C, service uptime, 1/5/15-minute system load averages, and used/total host memory in MiB. Label load as load average, not CPU utilization. Add latest observation age, effective AUDIO_DEVICE, configured volume policy, and the last audio failure with UTC timestamp to the existing component-health section.

Use Linux's CPU thermal-zone reading, `/proc/meminfo` (MemTotal and MemAvailable), and standard-library monotonic time/load averages. On unsupported systems or missing/unreadable/invalid sensor files, display `Unavailable` for that metric independently. A fresh service with no observation displays `No frames received`. These are diagnostics; a sensor failure must never block commands or change protection state.

### Audio diagnostics

Capture aplay and amixer stderr and preserve backend operation, device, sound key/relative asset when applicable, exit code or OS exception, and a bounded reason. Keep the last 4096 bytes of stderr while draining the pipe continuously; do not accumulate unbounded output or wait for process exit before draining. Log failures at ERROR once per failed operation, with incident/generation context at runtime where available. Use DEBUG for per-file playback detail; avoid INFO logs for every frame, tick, or repeated siren WAV.

Keep the existing stable exception categories with a useful appended detail. Store the latest concise failure in runtime status, limited to 300 characters plus its UTC timestamp. Render it through Telegram's existing escaping/redaction path. Keep this as historical information until the next failure, clearly labeled “Last audio failure”; do not claim successful asset validation proves that a device works. Log effective device/volume/languages at startup, readiness rejection at WARNING, and actual playback start/stop at INFO once per logical playback operation.

Preserve command acknowledgment semantics: `/alarm` means requested; activation remains tied to the playback-start callback. Do not claim device-open success merely because a process was spawned. Do not replace the existing startup grace period with an unsupported guarantee about audible output.

### Readiness and camera independence

Use an explicit audio checker rather than filtering text. Manual `/alarm` needs readable configured WAV assets and aplay, and must work with absent RTSP settings, unreachable/stale camera, detector failure, or missing model. Preserve full-sequence WAV validation for now; selecting only the siren asset would be a separate behavior change.

`/arm` keeps full camera/detector/model/backend readiness, fresh observations, and configured volume application before arming. It needs amixer only when volume is configured. Manual `/alarm` continues to leave volume unchanged and must not require amixer. Camera faults must stop automatic playback but preserve manual playback. Both modes retain stop/disarm, audio-fault handling, and existing duration limits.

### Alternatives and exclusions

Recommended: targeted diagnostics and stdlib host metrics. A monitoring agent or new metrics dependency adds setup and runtime cost for this small status view. Automatic ALSA device selection or mixer-control fallback could route a siren to the wrong output; preserve explicit/default ALSA selection and diagnose the real failure before proposing a routing fix.

No automatic sound probes at startup/status, new bot commands, device switching, volume-policy changes, general logging rewrite, alarm-policy refactor, or unrelated macOS test cleanup. No deployment, restart, physical sound playback, or externally visible actions during implementation without the user's approval.

## Global constraints

- Preserve existing comments and blank lines; use the repository's two comment-only lines between major logic sections.
- Keep existing config layers and command authorization; add no secrets to logs or Telegram.
- Preserve automatic alarm timing, suppression/recovery, cancellation, and generation guards.
- No new runtime dependencies. Host metrics unavailable on other platforms must not break the service.
- Offline readiness must remain offline: no camera, Telegram, playback, or mixer execution.

## Review focus

- Verbose/invalid backend stderr must remain bounded, decodable, redacted, and unable to deadlock playback or stop.
- A slow or missing host metric must not block Telegram commands or suppress healthy metrics.
- Cold-start camera outage and loss during a manual siren must both leave manual playback functional.
- Default PCM and default mixer can resolve differently; status/logs must show the effective selection without inventing a hardware fix.
- Old failures and stale playback generations must not misrepresent the currently active operation or clear suppression.

## Task 1: Preserve and report actionable audio failures

**Files:** Modify `app/services/audio.py`, `app/runtime.py`, `app/services/telegram.py`; test `app_tests/test_audio.py`, `app_tests/test_runtime.py`, `app_tests/test_telegram.py`, `app_tests/test_logger.py`.

**Interfaces:** Preserve `AudioPlayer.play()`, `set_volume()`, and `stop()` signatures. Extend failure messages with bounded detail. Add `audioDevice`, `audioVolume`, and `lastAudioFailure` to `Runtime.status()`; the latter is `None` or a dictionary with `operation`, `reason`, and `atUtc` (UTC string). Preserve existing `audio` health meaning.

- [x] Add failing tests for aplay immediate exit with ALSA reason, missing executable, permission failure, delayed nonzero exit, and amixer missing PCM/control/timeout failures. Assert operation/device/exit context and that immediate failure never emits started.
- [x] Add failing tests for stderr over 4096 bytes, invalid UTF-8, stderr drain during long playback, cancellation during process spawn, terminate/kill cleanup, and `/stop` while stderr is flowing. Assert bounded retained output, no deadlock or orphaned drain task, and no cancellation reported as device failure.
- [x] Add runtime/logging tests asserting one failure record, incident/generation context, retained last failure timestamp/reason, and existing suppression. Test stale-generation failure does not mark a newer operation as failed. Cover readiness and volume exceptions separately from playback exceptions.
- [x] Add Telegram tests for escaped/bounded last failure, effective default/explicit device, configured/unconfigured volume, and historical labeling after a later successful playback. Feed configured secrets, encoded secrets, and HTML characters through stderr to verify logger and Telegram redaction.
- [x] Run the new tests and confirm they fail for the intended missing behavior.
- [x] Capture/drain bounded subprocess stderr, preserve OS/backend reasons, and cancel/join drain tasks during cleanup. Log expected operational failures without duplicate records; retain tracebacks for unexpected runtime exceptions. Add safe startup audio settings and command/playback lifecycle records using the existing logger.
- [x] Update runtime status and Telegram rendering as defined above. Keep diagnostics separate from readiness/suppression authority.
- [x] Run `poetry run python -m unittest app_tests.test_audio app_tests.test_runtime app_tests.test_telegram app_tests.test_logger -v`; require all tests to pass.

## Task 2: Make readiness dependencies explicit and prove camera-independent manual alarms

**Files:** Modify `app/runtime.py`; test `app_tests/test_runtime.py`, `app_tests/test_alarm.py`, `app_tests/test_config.py`. Change `app/services/alarm.py` only if the new fault tests expose a real defect.

**Interfaces:** Add `check_audio_readiness(config: Dict[str, Any], *, require_volume: bool) -> list[str]`. Keep `check_readiness(config) -> list[str]` as the complete offline checker by composing it with audio readiness. `Runtime._readiness("alarm")` calls only the audio checker with `require_volume=False`; arm retains complete readiness and freshness checks.

- [x] Add failing dependency tests using real readiness functions with filesystem/backend boundaries mocked, rather than mocking the whole checker. Verify manual readiness never reads/hashes the detection model or inspects FFmpeg and does not require RTSP credentials or amixer. Verify missing aplay or invalid WAV still rejects it.
- [x] Add runtime tests starting with no frames, unhealthy camera/detector, and missing camera/model configuration. Start a long-running fake siren; deliver camera and detector faults plus stale-frame ticks. Assert the same task keeps playing, no extra activation, no photo requirement, and duration limit, `/stop`, and `/disarm` still terminate it.
- [x] Add paired tests showing identical faults reject `/arm` or interrupt automatic playback, and audio failure still stops a manual siren. Verify amixer is required only for configured-volume arm; manual `/alarm` never applies volume.
- [x] Retain/add config regression tests for missing, empty, explicit `default`, and explicit `plughw:CARD=...` values across file/process overrides. Verify recent English/main-language inference remains intact. Do not replace ALSA default with a guessed card.
- [x] Run these tests before implementation; distinguish missing coverage from tests that already pass because controller behavior is correct.
- [x] Implement explicit readiness composition and remove human-message filtering. Preserve serial checks, volume-before-arm order, and the existing recheck after volume application.
- [x] Run `poetry run python -m unittest app_tests.test_runtime app_tests.test_alarm app_tests.test_config app_tests.test_startup -v`; require all tests to pass, with any baseline platform issue reported separately.

## Task 3: Add lightweight host health to Telegram status

**Files:** Create `app/services/system_health.py`, `app_tests/test_system_health.py`; modify `app/runtime.py`, `app/services/telegram.py`; test `app_tests/test_runtime.py`, `app_tests/test_telegram.py`.

**Interfaces:** Add `read_system_health() -> Dict[str, Any]` returning `cpuTemperatureCelsius` (float or None), `loadAverage` (three-float tuple or None), `memoryUsedMiB` and `memoryTotalMiB` (float or None). Runtime records service start with monotonic time and exposes `system` containing these metrics plus `uptimeSeconds` and `lastFrameAgeSeconds` (None before any fresh observation).

- [x] Add failing collector tests for millidegree conversion (e.g. 57321 → 57.321 °C), MemTotal minus MemAvailable, load-average ordering, missing/permission-denied/malformed files, invalid/nonfinite values, and independent metric failures. Identify CPU thermal zones by their type rather than assuming every zone is CPU.
- [x] Add tests for monotonic service uptime, no initial frame, fresh/stale frame age, and no alarm-state changes when metrics fail. Keep stale observation age visible as elapsed time rather than showing a healthy frame.
- [x] Add Telegram tests asserting clear units, named load averages, zero values displayed correctly, `Unavailable` per missing metric, and escaped output. Preserve keyboard, authorization, existing component/queue fields, and audio recovery hints.
- [x] Run the new tests and confirm the missing collector/status behavior fails.
- [x] Implement bounded local file reads and stdlib load averages. Sample outside the event loop using `asyncio.to_thread`, at most once every five seconds in the runtime loop; `status()` returns cached values immediately. On a failed sample overwrite failed metrics with None rather than retaining apparently current stale values. Cancel/join the sampler at shutdown. Add no subprocess metrics probes or new dependency.
- [x] Add sampler lifecycle tests proving slow sampling cannot block `/stop`, samples never overlap, and shutdown leaves no pending sampler. Report `Unavailable` before the first sample.
- [x] Run `poetry run python -m unittest app_tests.test_system_health app_tests.test_runtime app_tests.test_telegram -v`; require all tests to pass.

## Task 4: Document, verify, and prepare supervised Pi acceptance

**Files:** Modify `README.md`, `docs/raspberry-pi.md`; add a dated behavior amendment to the existing camera-alarm design and implementation plan so their status/readiness descriptions remain synchronized. Do not rewrite historical verification claims.

- [x] Document status fields, load versus utilization, unsupported metrics, effective default-device behavior, manual alarm's unchanged volume policy, last-failure history, and where JSON logs appear (`journalctl -u ai-cam-security`). State offline checks cannot establish that the speaker works.
- [x] Add a troubleshooting sequence: inspect safe effective audio settings and latest failure; inspect `aplay -l`, `aplay -L`, `amixer scontrols`, service account/group access, and service sandbox. Compare user versus service-account routing. Do not dump private env files or credentials. Test PCM playback and mixer selection separately.
- [x] Run `poetry run python -m unittest discover -s app_tests -v`, `poetry run mypy`, `poetry check --strict`, and `git diff --check`. Report focused and full-suite results separately. Use canonical temporary paths if necessary for the known macOS baseline issue; do not mask unrelated failures.
- [x] Review coverage against every requirement above and inspect the final diff for unrelated changes and secrets. Leave all live checks below explicitly pending until performed with approval.

### Live acceptance (requires Pi access and approval for sound/service changes)

1. Read-only first: collect effective safe audio settings, service identity/groups, ALSA inventory, PCM controls, and relevant journal errors. Determine whether failure is asset validation, mixer setup, device opening, or playback. Propose a device/config fix only from that evidence.
2. With permission for audible output, verify empty/default and the actual explicit device as the service account. Confirm permission/busy/missing-device errors show their real reasons in JSON and `/status`. Avoid unplugging or altering shared hardware just to produce a failure.
3. With RTSP unavailable via an approved test configuration, start the service disarmed, request `/alarm`, confirm audible sound and activation without a photo, then `/stop`. Confirm `/arm` refuses without fresh vision.
4. During an approved manual siren, simulate camera loss in a test setup and verify playback continues to its single-period limit. Compare automatic playback stopping on vision fault. Restore the test setup afterward.
5. Compare Telegram CPU temperature/load/memory with Pi readings; verify failure/recovery of optional metrics leaves commands responsive. Record actual observations separately from mocked test results.

## Verification record for this planning turn

- Inspected recent commits `0a6b97d` and `ef4685b`, audio/runtime/controller/Telegram/logger/config paths, nearby tests, and deployment documentation.
- Original focused command: `poetry run python -m unittest app_tests.test_audio app_tests.test_config app_tests.test_runtime app_tests.test_alarm app_tests.test_telegram app_tests.test_logger` — 144 run, 141 passed, three macOS temporary-path comparison failures.
- Rerun with `TMPDIR=/private/tmp` before the same Poetry command — all 144 tests passed. Poetry selected Python 3.13.4 because the active Python 3.15.2 is outside the project's supported range. A Telegram retry_after deprecation warning remains.
- No deployed journal, Raspberry Pi ALSA inventory, or live speaker/RTSP results were available. Hardware root cause remains open.


## Implementation and verification record (2026-11-08)

- Implemented in the existing `beta-deploy` checkout at the user's request; changes remain uncommitted.
- Audio captures a bounded stderr tail for aplay and amixer, logs actual backend/OS details and lifecycle events, and exposes escaped/redacted historical failures in status. Regression coverage includes busy-device output over the pipe limit, invalid UTF-8, delayed failure, stop/kill, mixer timeout, spawn cancellation, and stale operation failures.
- Manual readiness now explicitly checks audio only. Integration tests cover no initial camera observations, camera/detector faults during a manual siren, missing aplay, manual duration/stop/disarm/audio-fault endings, and automatic readiness protections. Existing default-device/language config regression tests passed unchanged.
- Added host metrics and background sampling; tests cover independent invalid/missing metric files, CPU-zone selection, units/zero values, stale frame age, sampling failure replacement, and responsive stop/shutdown during a slow sample. Cancelling the sampler joins its asyncio task; an already running bounded local-file read in `to_thread` can finish afterward.
- Independent review identified volume-spawn stderr cleanup and superseded mixer/validation health races. Each concrete regression was observed failing before its fix. Post-volume validation failure also now refuses arm with diagnostics instead of escaping. Final bounded review reported no outstanding findings.
- Focused verification: `poetry run python -m unittest app_tests.test_audio app_tests.test_runtime app_tests.test_telegram app_tests.test_logger app_tests.test_system_health` — **108 passed**.
- Full verification: **211 passed** with the canonical-temp bootstrap below. Standard discovery before the last validation regression ran 210 tests and reproduced only the three known macOS `/tmp` versus `/private/tmp` assertion failures (`test_defaults_use_project_root`, `test_detection_defaults_and_validation`, `test_layers_and_process_environment`). No config implementation/test cleanup was included.
- `poetry run mypy` — passed (16 source files); `poetry check --strict` — passed; `git diff --check` — passed. Python 3.13.4 was selected by Poetry; the pre-existing Telegram `retry_after` deprecation warning remains.
- README, Pi troubleshooting guide, and the original design/plan amendments are synchronized. All live acceptance steps above remain **pending**. No Pi root cause or physical sound result is claimed.

Canonical-temp full-suite command (resolves the macOS temporary-directory alias without changing repository tests):

```sh
poetry run python -c 'import tempfile, pathlib, unittest; tempfile.tempdir = str(pathlib.Path(tempfile.gettempdir()).resolve()); result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover("app_tests")); raise SystemExit(not result.wasSuccessful())'
```

## Follow-up: failure details in incident notifications (2026-11-08)

The user reported that the audio-fault deactivation notification still displayed
only “Audio playback failed.” The initial implementation exposed details in logs
and status but did not carry them through the incident event/notification path.

- Added a sanitized playback-error snapshot to the queued audio result and its
  deactivation event; later failures cannot replace that incident's explanation.
- The notification now includes an escaped “Audio error” line, bounded to 300
  UTF-16 units. Long error summaries retain initial device/exit context and the
  backend's final explanation. Suppression and recovery rules remain unchanged.
- Regression tests reproduced missing details and caption overflow before fixes;
  tests also cover stale generations, redaction, HTML escaping and queued error
  independence from later failures.
- Verification: 60 runtime/notification/delivery tests passed; all 214 tests passed
  with the canonical-temp bootstrap; mypy (16 files) and `git diff --check` passed.
  Independent review found no issues in the notification fix. No deployment,
  restart, live sound playback or hardware diagnosis was performed.
