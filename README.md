# ai-cam-security

Local RTSP person detection and multilingual audio alarms with authorized Telegram control.

Experimental Raspberry Pi camera alarm, licensed under [Apache-2.0](LICENSE).
Pinned NanoDet models are included with [third-party attribution](NOTICE).
Supply your own licensed PCM WAV recordings before enabling playback; sounds
and camera footage are kept local and are excluded from the public repository.

The service starts disarmed. One isolated worker captures the newest camera frame
and runs OpenCV NanoDet; the asyncio parent controls audio, incidents and Telegram.
Day/night confirmation, confirmed absence and stale-camera handling use distinct
fresh frames. Telegram outages do not stop local protection.

**Hardware acceptance is pending.** Preliminary Pi day/night benchmarks are recorded in
the measurement report; accuracy acceptance, live RTSP latency and speaker
audibility checks remain open. Complete the
[Pi guide](docs/raspberry-pi.md) and [measurement gate](docs/benchmarks/pi5-detection.md)
before unattended operation.

## Setup and run

Use Python 3.11–3.14 and Poetry 2.x. For local development, run from the project root:

Copy [`.env`](.env) to `.env.local` (or `.env.prod.local` for deployment) and set
your camera credentials, Telegram bot token, allowed user IDs and notification
chat ID in that ignored copy. The public defaults have no credentials or IDs.
See the [Pi guide](docs/raspberry-pi.md) for the required WAV files and backends.

```sh
poetry install
poetry run ai-cam-security --check
poetry run ai-cam-security
```

This installs runtime dependencies and development tools, including `mypy`.
For Raspberry Pi deployment, follow the [Pi guide](docs/raspberry-pi.md), which uses:

```sh
POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --only main
```

The Pi installation requires `.venv` in the project root for the systemd unit;
`--only main` installs runtime dependencies. The `poetry run` commands work with
either installation; use `--env prod` to load the production configuration.

The startup check validates configuration and creates the storage directory, then
exits. `poetry run ai-cam-security --check-ready` additionally checks local assets,
the pinned model hash, credentials and installed backends; it exits nonzero when
setup is incomplete. Both commands are offline. Neither proves live readiness.

Alternative entry point:

```sh
poetry run python -m app --env dev
```

Stop with Ctrl+C. SIGINT and SIGTERM request a clean shutdown.

The console command is named `ai-cam-security` to avoid the Windows shell's
built-in `start` command.

## Configuration

Configuration files are resolved relative to the project root. Values load in this
order, with later sources overriding earlier ones:

1. `.env`
2. `.env.local`
3. `.env.<environment>`
4. `.env.<environment>.local`
5. Process environment variables

Select the environment with `--env dev` / `-e dev` or the `ENV` environment
variable. The CLI selection takes precedence. With neither set, only the two base
files are loaded. Imports do not parse arguments or load configuration.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEBUG` | `0` | Enables DEBUG logs unless LOG_LEVEL is set. Accepts 0/1, true/false, yes/no, on/off. |
| `LOG_LEVEL` | `INFO`, or `DEBUG` with debug enabled | Standard Python logging level. |
| `STORAGE_FOLDER` | `volume` | Uploaded/generated content; relative paths resolve from the project root. |

The checked-in `.env` enables debugging locally; `.env.prod` disables it.
Keep credentials and real Telegram IDs in ignored `.env.local` /
`.env.<environment>.local` files or process environment variables.

Use [`.env`](.env) as the settings reference and put private values and overrides
in `.env.local` (or `.env.prod.local` for `--env prod`). All camera, Telegram,
detection and audio settings are implemented. Missing credentials allow disarmed
diagnostics; malformed supplied values fail with safe field names.

Camera connection settings are split into `RTSP_LOGIN`, `RTSP_PASSWORD` (the
camera verification code), `RTSP_HOST` (IP address or hostname), `RTSP_PORT`,
and `RTSP_PATH`. The config loader assembles the RTSP URL and percent-encodes raw credentials once.
`AUDIO_DEVICE` selects the Raspberry Pi speaker output.

Set `AUDIO_VOLUME` to an integer percentage from `0` to `100` to apply the device's
`PCM` playback volume before every successful `/arm`. Leave it empty or unset to
keep the current volume. Volume control requires `amixer` from `alsa-utils` and
`AUDIO_DEVICE=default` or a device containing `CARD=`, such as
`plughw:CARD=UACDemoV10,DEV=0`. A mixer error rejects the arm attempt without
clearing suppression. Restart the service after changing the setting.

Folder layout:

- `volume/`: uploaded/generated content, including sample recordings and benchmark output.
- `system/models/`: detection models.
- `system/sounds/`: prerecorded sounds, with `common/` and a subfolder for each configured language.

Model and sound paths resolve from the project root, independently of `STORAGE_FOLDER`.

`cz` (Czech) and `en` (English) are just the default language choices. You can
use any language: create `system/sounds/<language>/` and put your PCM WAV files
there, using the filenames in `AUDIO_SEQUENCE` (`first_warning.wav`,
`countdown_1.wav`, `countdown_2.wav`, `countdown_3.wav`, and `alarm.wav`). Directory
names may contain letters, numbers, underscores and hyphens.

For example, create `system/sounds/fr/` for French and set these values in your
ignored `.env.local` or `.env.prod.local`:

```dotenv
AUDIO_MAIN_LANGUAGE=fr
AUDIO_LANGUAGES=fr,en
```

`AUDIO_MAIN_LANGUAGE` selects the required language, played first for each
sound key. Other `AUDIO_LANGUAGES` follow in listed order,
without repeating the main language; missing secondary files are skipped.
A `common/<key>.wav` file replaces all language versions for that key. Otherwise,
a missing main-language file prevents readiness and arming.

The public `.env` uses confidence `0.35`, Czech followed by English, no forced
device volume and no periodic siren photos. The code fallback for confidence is
`0.5`. Keep installation-specific preferences in a local override and restart
after configuration changes.

## Logging

Logs are JSON lines on stdout, with a UTC timestamp, level, service, message, and
context. Process managers can collect and rotate stdout. No Graylog server is required.

```python
from app.services.logger import Logger

logger = Logger({"camera": "front-door"})
logger.info("Camera connected")
# Inside an exception handler:
# logger.exception("Camera worker failed")
```

The entry point configures logging once. Additional Logger instances do not add
handlers. Use `configure_logging()` when embedding the service in another process.
Configured credentials and their encoded forms are redacted in final JSON output,
including tracebacks and third-party logs. Native camera diagnostics are suppressed
inside the worker; health uses safe fault codes.

## Commands and behavior

Telegram commands are accepted only from configured numeric user IDs in private
chats. Edited, duplicate, old, future-dated and pre-reconnect commands are rejected.

On each full service start or restart, the bot registers all seven commands in
Telegram's command menu and sends a startup notice to `TELEGRAM_CHAT_ID` with the
start time and a reminder that it started disarmed. Every outgoing Telegram message,
including photos and notifications, includes a persistent keyboard with all
commands. Keyboard buttons use the same authorization and freshness checks as
typed commands. If Telegram is offline
at startup, setup and delivery retry when connectivity returns. Successful startup
notices are not repeated on a Telegram reconnect.

Status replies group protection, component health and notification delivery instead
of sending JSON. Commands and incident notifications use bold headings, readable
reasons and italic guidance; event times are labelled UTC. Audio-fault messages
explain how to resume automatic alarms with a successful `/arm`.

| Command | Result |
| --- | --- |
| `/status` | Mode, phase, day/night profile, automatic siren periods used/allowed, component and queue health |
| `/photo` | Wait up to 5 seconds for a frame captured after the request and send it to the requesting chat; report unavailable if no fresh frame arrives |
| `/arm` | Check camera/detector/audio readiness; apply configured volume, clear suppression and arm |
| `/disarm` | Disable automation and stop all playback |
| `/alarm` | Manual siren, including while disarmed; at most 60 seconds |
| `/stop` | Stop playback, keep armed mode, reset siren allowance and require fresh confirmation |
| `/help` | Command summary |

Defaults: confirm for 2 seconds by day and 3 seconds from 22:00 to 06:00 in
Europe/Prague; require at least 3 frames and positive gaps no greater than
1 second. Fresh negatives spanning 2 seconds confirm absence. Frames older than
2 seconds cancel automatic audio as unknown visibility. Worker recovery preserves
armed mode but requires fresh confirmation; full service restart starts disarmed.
Audio faults require a successful explicit `/arm`, even after absence or recovery.

Automatic alarms are allowed whenever armed, both day and night. Night hours
only select the confirmation time; they do not arm the service.

Once confirmed, play `first_warning` and every countdown key to completion,
even if the person leaves. The files play back-to-back, without an extra pause.
After countdown, start the siren if a fresh observation detects a person. If the
latest observation is negative, wait for a detection or confirmed absence;
confirmed absence ends the incident without a siren.

The siren WAV repeats for the full `AUDIO_MAX_ALARM_SECONDS` period (default 60),
even if the person leaves during playback. At the period boundary, confirmed
absence ends the incident. Otherwise, playback continues immediately into the
next siren period, up to `AUDIO_MAX_ALARM_CYCLES` total (default 3: initial plus
two repeats). Warnings/countdowns and activation notifications are not repeated.
Returning before the current period ends does not reset this count. Reaching the
limit suppresses automatic playback until confirmed absence, `/stop` or an explicit `/arm`.
Vision-worker recovery preserves periods already used. `/stop`, `/disarm`, audio
failure and loss of camera visibility interrupt automatic playback immediately.
Manual `/alarm` has one period; overriding an automatic siren keeps the current
period's deadline.

After `/stop`, an armed system can start another warning/countdown sequence once
person detection is freshly confirmed, even if the person remains in view.
Audio-fault suppression still requires a successful `/arm`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `DETECTION_DAY_CONFIRM_SECONDS` | `2` | Presence time before the first warning by day |
| `DETECTION_NIGHT_CONFIRM_SECONDS` | `3` | Presence time before the first warning at night |
| `DETECTION_NIGHT_START` / `DETECTION_NIGHT_END` | `22:00` / `06:00` | Night profile in `DETECTION_TIMEZONE` (`Europe/Prague`) |
| `DETECTION_ABSENCE_SECONDS` | `2` | Fresh negative observations required to confirm departure |
| `DETECTION_ON_ALARM_SEND_PHOTO_EVERY_MS` | `0` | Milliseconds between Telegram photos while the siren plays; `0` disables periodic photos |
| `AUDIO_MAX_ALARM_SECONDS` | `60` | Duration of each siren period; WAVs loop as needed (maximum 60 seconds) |
| `AUDIO_MAX_ALARM_CYCLES` | `3` | Total automatic siren periods allowed before absence, `/stop` or rearming resets the count |

With the previously tested private Czech and English WAVs, warning/countdown
playback totals about 18.76 seconds. Continuous detection therefore starts the siren about 20.76 seconds
after initial detection by day, or 21.76 seconds at night, plus playback/control
overhead. These WAVs are not distributed. Your WAVs and selected languages
determine the delay.

An Internet outage leaves an already armed service detecting and playing local
audio, provided the camera remains reachable on the local network. Telegram
commands and delivery need connectivity. A full restart during the outage starts
disarmed; Telegram must reconnect before `/arm` can be received.

The service sends a text notification to `TELEGRAM_CHAT_ID` when the RTSP camera
stream becomes unavailable and another when fresh frames return, even while
disarmed. Capture/vision faults and frames older than
`DETECTION_MAX_FRAME_AGE_SECONDS` count as unavailable. Repeated checks in the same
state do not repeat the notice. Startup allows 15 seconds plus the configured
camera open/read timeouts before reporting that no initial frames arrived; a
successful initial connection sends no recovery notice. These notices use the
same bounded delivery queue and retry policy as alarm notifications.

Notifications contain event-time images for recognition, activation and deactivation;
the still-there transition is text only. Set `DETECTION_ON_ALARM_SEND_PHOTO_EVERY_MS`
to send additional photos during automatic and manual sirens. The first periodic
photo is due one interval after playback starts; warnings/countdowns send no periodic
photos. Each photo requires a newer frame within `DETECTION_MAX_FRAME_AGE_SECONDS`.
Missed intervals do not produce catch-up bursts. The timer continues across siren
periods and manual override, and resets when playback ends. The interval accepts
milliseconds from `0` to `3600000`, including fractions. Restart after changing it.

Photos remain in memory. Delivery is bounded
to 20 events and 32 MiB, including the in-flight item, with a 120-second expiry and
at most 3 send attempts. Delivery can duplicate a message after an uncertain network
outcome; incident IDs and timestamps identify it. Drops are summarized on recovery.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution checks and
[SECURITY.md](SECURITY.md) for private vulnerability reporting.
The GitHub CI workflow is configured to run offline startup checks, unit tests
and mypy on Ubuntu and Windows with Python 3.11 and 3.14. It does not validate
Raspberry Pi hardware.

```sh
poetry run python -m unittest discover -s app_tests -v
poetry run mypy
poetry check --strict
```

VS Code includes a module-based debug configuration. Select the Poetry environment
as the Python interpreter. You can also use `poetry run python -m app.debugger`.

Runtime dependencies are python-dotenv, NumPy, opencv-python-headless and
python-telegram-bot, plus tzdata on Windows. Install only one OpenCV distribution.
Tests use synthetic images and fake capture/player/Telegram boundaries; they do
not contact a camera or bot or play audio. Mypy is a development dependency.

## PM2 (optional)

With PM2 already installed:

```sh
pm2 start server.json
pm2 start server.json --env dev
pm2 start server.json --env prod
```

Run these commands from the project root. The config watches `app` and environment
files, and excludes tests and storage.
Use one process manager per camera/bot. The recommended Pi systemd template and
supervised acceptance checklist are in [the deployment guide](docs/raspberry-pi.md).

## Documentation

- [Raspberry Pi service updates](docs/update.md)
- [Poetry](https://python-poetry.org/docs/)
- [python-dotenv configuration](https://github.com/theskumar/python-dotenv#load-configuration-without-altering-the-environment)
