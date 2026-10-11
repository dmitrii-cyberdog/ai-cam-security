# Raspberry Pi camera alarm design

Status: approved by the user on 2026-09-27 for implementation planning.
This approval does not authorize deployment. Values labelled proposed are
accepted initial defaults to verify and tune on the real camera.

## Goal and confirmed requirements

Run a local person-detection and audio alarm service on a Raspberry Pi 5 with
4 GB RAM and no AI accelerator. Use OpenCV and python-telegram-bot.

- Connect to an RTSP camera and recognize people.
- Load RTSP and Telegram credentials from environment files through
  `app/config/_.py`. Preserve the nested `Dict[str, Any]` configuration.
- Play a configurable sequence of sound keys across configured languages.
- Prefer a common sound over language versions when it exists in the common directory.
- Finish warning/countdown sounds after departure; skip the siren if absence is
  confirmed at their end. A started siren finishes its current duration limit.
- Support Telegram control: enable, disable, manually play, and stop alarms.
- Send four incident notifications: person recognized with photo, person still
  there, alarm activated with photo, and alarm deactivated with photo.
- Use different person-confirmation durations for day and night.

## Scope and implementation approach

The initial version supports one camera, one monitored scene, prerecorded WAV
audio, and a fixed list of authorized Telegram users. No face identification,
continuous video recording, web interface, multi-camera orchestration, or cloud
inference is included.

Recommended approach: CPU inference through OpenCV DNN, with blocking capture
and inference isolated from the asyncio controller and Telegram operations.
An accelerator adds hardware and a separate runtime; remote inference adds a
network dependency. Neither is required for the initial version.

Use a lightweight pretrained person-capable model. Model selection is a measured
engineering milestone: evaluate compatible OpenCV DNN models on the Pi using
day and night recordings, verify model licensing and preprocessing, then record
the chosen artifact, checksum, labels, input dimensions, and measured performance.
Do not promise a frame rate or detection accuracy before this measurement.
Proposed starting target: at least two fresh inference results per second without
delaying local stop/disarm handling. If the target is not met, review resolution
and model choice before implementing automatic audio activation. After the initial
measurements, review the results with the user and agree acceptable false-alarm
and missed-person criteria for day and night. Record the criteria and verify
them before passing the hardware gate.

## Existing project and component boundaries

The project currently contains explicit configuration loading, JSON logging,
an idle asyncio service, CLI startup checks, and unittest tests.

Keep that foundation and add focused components:

- `app/services/camera.py`: own the RTSP connection, reconnect, and provide the
  newest timestamped frame. Never build an unbounded backlog of old frames.
- `app/services/detection.py`: load the model once, infer person detections,
  and return frame identifiers, capture times, confidence, and bounding boxes.
- `app/services/alarm.py`: own incident state, timing, command decisions, and
  event generation. All automatic and manual activation uses this controller.
- `app/services/audio.py`: resolve sound keys, play one file at a time, and
  cancel playback. Use an audio subprocess with explicit arguments, not a shell.
- `app/services/telegram.py`: authorize commands, manage polling, and deliver
  notifications independently of audio and inference.
- `app/cli.py`: coordinate lifecycle and release all resources on shutdown.
- `app/config/_.py`: load credentials and typed values into the existing nested
  dictionary structure.

No worker may directly mutate the controller's state. Thread/process results
cross into the controller through bounded messages. Keep camera and inference
ownership explicit so reconnect and cancellation cannot race with frame reads.
Validate capture timeout and shutdown behavior against the installed backend;
use a separately terminable capture process if that backend cannot reliably
bound blocking reads. Do not assume cancelling an async wrapper stops native I/O.

## Configuration

Preserve existing environment precedence and root-relative paths. Checked-in
environment files contain examples/defaults only; real credentials use ignored
`.env.local` or `.env.<environment>.local` files, or process environment variables.

Use `volume/` for uploaded/generated content, such as sample recordings and
benchmark output. Use `system/` for system assets: models in `system/models/`
and prerecorded sounds in `system/sounds/`. Keep `server.storageFolder` pointed
at `volume`; resolve model/sound paths independently from the project root.

Proposed dictionary sections:

- `camera`: `login`, `password`, `host`, `port`, `path`, derived `rtspUrl`,
  connection/read timeouts, reconnect delays.
- `telegram`: `token`, `allowedUserIds`, `notificationChatId`.
- `detection`: model settings, `dayConfirmSeconds`, `nightConfirmSeconds`,
  `absenceSeconds`, `maxFrameAgeSeconds`, `timezone`, `nightStart`, `nightEnd`.
- `audio`: `soundsFolder`, `mainLanguage`, `languages`, `sequence`, playback device,
  `maxAlarmSeconds`, `maxAlarmCycles`.
- Existing `debug`, `server.storageFolder`, and `logs.level` remain.

Keep camera connection fields separate in environment files: `RTSP_LOGIN`
(default `admin`), `RTSP_PASSWORD` (camera verification code), `RTSP_HOST`
(IP address or hostname), `RTSP_PORT` (default `554`), and `RTSP_PATH`
(default `/ch1/main`). Assemble `camera.rtspUrl` in `app/config/_.py`, encoding
login/password as URL components. Enter credentials as raw values in env files.
`AUDIO_DEVICE` independently selects the Raspberry Pi speaker output.
Keep shared settings in the checked-in `.env`; put credentials, camera host and
private overrides in ignored `.env.local` / `.env.<environment>.local` files.

The assembled RTSP URL contains credentials. Neither it nor the bot token may appear in
logs, error replies, or status output. Redact external errors before logging them.
Authorize with numeric user IDs and private chats; notification destination is
configured separately and is never learned from an arbitrary incoming command.

## Recognition and day/night behavior

Proposed defaults:

| Setting | Initial value |
| --- | --- |
| Day confirmation | 2 seconds |
| Night confirmation | 3 seconds |
| Confirmed absence | 2 seconds |
| Maximum fresh-frame age | 2 seconds |
| Maximum gap between positive observations during confirmation | 1 second |
| Minimum distinct positive frames during confirmation | 3 |
| Night hours | 22:00 to 06:00, configurable |
| Timezone | Europe/Prague, configurable |

Measure sustained person presence, not continuous movement. A stationary person
still counts. A qualifying confirmation requires distinct fresh observations
spanning the confirmation duration; repeated processing of one frame never adds
evidence. Allow short detection misses within the positive-observation gap.
Freeze the day/night confirmation duration when each candidate starts.
Automatic alarms are allowed whenever armed, both day and night; the night
schedule only selects the confirmation duration.

Use monotonic time for durations, local wall-clock time only for day/night
selection. Detection absence requires fresh negative observations spanning the
absence duration. Camera failure or stale frames are unknown visibility, not
proof that the person left.

During unknown visibility, cancel automatic audio and reset confirmation;
report camera unavailability as the reason. Reconnection starts a new
confirmation window. Night settings improve temporal filtering but do not
guarantee recognition in darkness; test the camera's actual night/IR footage.

## Multilingual audio resolution

Example layout:

```text
system/sounds/
  common/alarm.wav
  en/first_warning.wav
  en/countdown_1.wav
  en/countdown_2.wav
  en/countdown_3.wav
  cz/first_warning.wav
  cz/countdown_1.wav
  cz/countdown_2.wav
  cz/countdown_3.wav
```

Keep `cz` as the user's directory label. `AUDIO_MAIN_LANGUAGE` maps to
`audio.mainLanguage` and defaults to `cz`. For main language `cz`, languages `["en", "cz"]`, and
sequence `["first_warning", "countdown_1", "countdown_2", "countdown_3", "alarm"]`:

1. Resolve each key in sequence order.
2. If `common/<key>.wav` exists, play it once and skip language versions.
3. Otherwise require and play `<mainLanguage>/<key>.wav` first (`cz` by default).
4. Play available files for the remaining configured languages in their listed
   order, skipping missing secondary files and never repeating a language.
5. Finish the resolved files for one key before moving to the next key.

Include the main language first even if it is absent from `AUDIO_LANGUAGES`.
Validate every resolved file before arming. For each key without a common file,
a missing main-language file rejects readiness and arming with a specific error.
Missing secondary-language files do not reject readiness; skip them per key.
A common file satisfies that key without requiring a main-language version.
Unreadable or invalid WAV files that are selected for playback still reject readiness.
Restrict keys/language labels to directory-safe names and resolve files within
the configured sound root.

Play warnings and countdown files once each, back-to-back with no extra delay.
Once the warning starts, finish all warning/countdown files even if the person
leaves. Repeat the alarm file as needed for each `AUDIO_MAX_ALARM_SECONDS` period
(default 60 seconds). A person leaving during the siren does not interrupt the
current period. At its boundary, confirmed absence ends playback; otherwise,
continue immediately with the siren, up to `AUDIO_MAX_ALARM_CYCLES` total periods
(default 3, including the initial period). Keep playback uninterrupted across
period boundaries without repeating warnings/countdowns or activation events.
After the final period, require confirmed absence, `/stop` or explicit rearming
before another automatic incident. Stop/disarm and faults cancel playback immediately.

## Incident lifecycle

Keep armed/disarmed mode separate from incident phase:

```text
idle -> confirming -> warning -> countdown -> alarm -> idle
                        |           |          |
                        +-----------+----------+--> stopped
```

- An armed service confirms presence before opening an incident.
- At incident start, enqueue the recognized notification and start the warning.
- Continue detection throughout every audio phase.
- After all first-warning language files finish, begin countdown. Send the
  still-there message only if the latest fresh observation detects a person.
- Continue warning/countdown phases through negative observations. Stale
  visibility still cancels automatic playback with a camera-fault reason.
- After countdown, start the siren only if the latest fresh observation detects
  a person. Otherwise wait for a detection or confirmed absence. Confirmed
  absence ends the incident after all warning/countdown sounds have finished.
- During the siren, confirmed absence takes effect at the current period's
  deadline. If the person returns before that deadline, retain the used period
  count. Reset the allowance when confirmed absence ends the incident, `/stop`
  ends playback, or an explicit `/arm` starts fresh confirmation while idle.
- One scene-level incident covers any detected people; no identity tracking is
  implied. One person leaving while another remains does not end the incident.
- Manual stop preserves armed mode, resets the siren allowance and starts a fresh
  confirmation window. Maximum-duration expiry suppresses automatic retriggering
  until confirmed absence, `/stop` or an explicit arm command.
- Audio failure stops the incident and suppresses automatic playback until an
  explicit `/arm` passes readiness checks. Confirmed absence, stop/disarm, and
  vision-worker recovery do not clear this fault suppression. Show its reason
  in status.
- While an automatic or manual siren is playing, emit periodic photo notifications
  every `DETECTION_ON_ALARM_SEND_PHOTO_EVERY_MS` milliseconds (`0` disables them).
  Start the timer on siren playback, require a newer fresh frame for each photo,
  and skip missed intervals without catch-up bursts. Keep the timer across siren
  periods and manual override; reset it when playback ends. Use the existing
  bounded notification worker for encoding and Telegram delivery.
- Start disarmed after every full service restart. Expose this clearly in status;
  do not silently restore siren playback. Restarting only the vision child
  preserves the current armed mode, suppression state and used siren periods,
  clears old evidence, and requires fresh confirmation before automatic playback
  resumes. Recovery cannot bypass the period limit.

## Telegram notifications

Send events once per relevant transition, in incident order:

| Event | Trigger | Attachment |
| --- | --- | --- |
| Person recognized | Initial presence confirmation succeeds | Recognition photo |
| Person is still there | Warning finishes and presence remains confirmed | Text only |
| Alarm activated | Siren playback successfully starts | Current photo |
| Alarm deactivated | An active warning/countdown/alarm incident ends | Current photo |

The deactivated caption includes the phase and reason, so cancellation during a
warning does not imply the siren previously sounded. Do not emit it for an idle
disarm command. If the siren never starts, do not emit alarm activated.

Capture each photo at its event, not when a delayed network send occurs. If a
fresh frame is unavailable, use the latest available frame with its capture time
and an explicit stale-image label. If no frame exists, send text stating that
the photo is unavailable. Never delay stopping audio to obtain or upload a photo.

Manual siren playback emits activated/deactivated notifications without inventing
person-recognition events. It is independent of person presence and is stopped
by stop/disarm, failure, or the same maximum-duration limit.
Repeated `/alarm` during a manual siren has no additional effect. During an
automatic warning/countdown, it switches the same incident to a manual siren.
During an already playing automatic siren, it switches to manual mode while
keeping playback running, preserving the current period's 60-second deadline, and
avoiding a second activated notification.

Use a bounded ordered notification queue and bounded retries for transient
Telegram failures. Internet failure must not stop local detection/audio. Keep
event timestamps on delayed messages; suppress expired notifications and report
any dropped events after connectivity returns. Do not promise exactly-once
network delivery: a send may succeed even if its response is lost.
Local protection requires an already armed service and camera connectivity on
the local network. A full restart during an Internet outage starts disarmed;
receiving `/arm` requires Telegram connectivity to return.

## Telegram commands

| Command | Behavior |
| --- | --- |
| `/status` | Armed mode, phase, camera/detection/audio health, day/night profile, siren periods used/allowed |
| `/arm` | Validate readiness, enable automation, start fresh confirmation |
| `/disarm` | Disable automation and cancel all playback immediately |
| `/alarm` | Play common/manual alarm now, with bounded duration |
| `/stop` | Stop the current sequence, preserve armed mode and require fresh confirmation |
| `/help` | Explain commands and the distinction between stop and disarm |

Reject unauthorized commands before accessing frames or changing state. Do not
replay pending control commands after restarting/reconnecting; reject stale
commands as well as duplicate update IDs within the running process. Serialize
control decisions so a late audio callback cannot restart an alarm after disarm.

## Failure handling and deployment

- Camera unavailable: reconnect with bounded backoff; reset evidence and cancel
  automatic playback. Keep Telegram controls available.
- Vision child crashes or hangs: the parent stops automatic playback, cleans up
  and restarts the child with bounded backoff, and preserves armed mode and
  suppression. Require fresh confirmation after recovery; keep controls
  responsive and honor stop/disarm commands received during recovery.
- Telegram unavailable: continue local operation and retry the connection without
  replaying stale actions. Keep notification memory bounded.
- Audio/model unavailable: refuse automatic arming, report a specific fault, and
  do not claim successful alarm activation.
- Shutdown: stop playback, stop workers, release capture/model resources, and
  stop Telegram polling/application with bounded cleanup.
- Deployment: Raspberry Pi OS 64-bit, a supported Python/Poetry environment, and
  a systemd service. Verify OpenCV package/backend support on the target Pi and
  the selected audio device. Keep Windows useful for mocked tests/development.
- Audio assets are user-provided. Deployment checks device access and playback;
  actual speaker tests and sending Telegram messages need explicit authorization.

## Acceptance and verification

- Preserve existing configuration precedence, import safety, logging, and CLI tests.
- Test recognition with synthetic timestamps: day/night boundaries, brief misses,
  stationary people, repeated frames, stale frames, and fresh confirmed absence.
- Test common-file precedence, language ordering, missing assets, and immediate
  stop/disarm cancellation in every playback phase. Verify short siren files repeat.
- Test departure during every warning/countdown key: remaining prelude completes,
  with no siren after confirmed absence. Test departure during a siren: playback
  stops at the current period limit. Test three periods, uninterrupted repeats,
  absence/rearming reset, return before deadline and recovery preserving the limit.
- Test exactly the four incident event transitions, early departure, no false
  activated event on audio failure, and fresh/stale/unavailable photo behavior.
- Test authorization, concurrent stop/disarm, stale commands, and fresh automatic
  confirmation after manual stop while the same presence continues.
- Test manual override of an active siren without interrupting playback, extending
  its deadline, or duplicating the activated notification. Test audio-fault
  suppression until successful explicit rearming, including absence and return.
- Test camera and Telegram outages, bounded queues, recovery, and shutdown;
  distinguish vision-child recovery preserving armed mode from full service
  restart starting disarmed.
- Benchmark on the actual Pi with day/night recordings and observe memory, CPU,
  temperature, detection rate, false alarms, and stop latency. Proposed target:
  audio stops within one second after a local stop/disarm decision.
- Run a supervised end-to-end test before enabling unattended automatic alarms.

## Planning handoff

After user review, create a task-by-task implementation plan under
`docs/superpowers/plans/`, with exact interfaces, affected files, tests, and
verification commands. Hardware/model measurement comes before decisions that
depend on its outcome. No product code or dependencies change during planning.

## Documentation consulted

- [python-telegram-bot asyncio lifecycle](https://github.com/python-telegram-bot/python-telegram-bot/wiki/Frequently-requested-design-patterns)
- [python-telegram-bot authorized handlers](https://github.com/python-telegram-bot/python-telegram-bot/wiki/Code-snippets)
- [OpenCV video capture backend behavior](https://github.com/opencv/opencv/wiki/Video-capture-and-write-benchmark)
