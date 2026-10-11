# Raspberry Pi installation and supervised acceptance

This is a reviewable deployment guide. No Pi installation, camera connection,
speaker test, Telegram message or systemd change was performed during development.
The service starts **disarmed after every full restart**, including reboot.

For an existing installation, follow the [update guide](update.md) after pulling
a new version or changing production configuration.

## 1. Prepare and verify the target

Use a Pi 5 with 4 GB RAM, Raspberry Pi OS 64-bit, adequate power and cooling,
Python >=3.11,<3.15 and Poetry 2.x. Record actual output in the
[benchmark report](benchmarks/pi5-detection.md):

```sh
uname -m
cat /etc/os-release
python3 --version
poetry --version
```

Expected architecture: `aarch64`. Resolve/install the locked dependencies on
the actual Pi; successful Windows installation does not prove ARM64 support.
Do not silently change the supported Python range to make installation succeed.
Only `opencv-python-headless` is installed; avoid another OpenCV distribution.

After authorization, prepare `/opt/ai-cam-security` and a dedicated unprivileged
`ai-cam-security` account with access to the `audio` group. Install `alsa-utils`
and OS timezone data (`tzdata`). Copy the reviewed source and lockfile to this
installation root.

Create the service account before starting the unit. First check whether it
already exists:

```sh
getent passwd ai-cam-security
getent group ai-cam-security
getent group audio
```

On a fresh installation with no `ai-cam-security` user or group, create them:

```sh
sudo adduser --system --group --no-create-home --home /opt/ai-cam-security ai-cam-security
sudo usermod -aG audio ai-cam-security
id ai-cam-security
```

If the account already exists, verify its group and audio membership instead of
recreating it. The names must match `User=` and `Group=` in the unit.

Install a project-local virtual environment:

```sh
cd /opt/ai-cam-security
POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --only main
poetry env info --path
poetry run python -c 'import platform, cv2, numpy; print(platform.python_version(), platform.machine(), cv2.__version__, numpy.__version__); print(cv2.getBuildInformation())'
poetry run python -c 'from zoneinfo import ZoneInfo; print(ZoneInfo("Europe/Prague"))'
```

Expected environment path: `/opt/ai-cam-security/.venv`, matching the systemd unit.
An existing cached Poetry environment can prevent the setting from taking effect;
if the path differs, resolve the environment location before installing the unit.

Expected: FFmpeg enabled in OpenCV's build information, imports successful,
timezone available. Linux uses the OS timezone database; the lockfile installs
the Python `tzdata` package on Windows. Record package/interpreter versions.

## 2. Provide private configuration and local assets

Use the checked-in `.env` as the settings reference and create
`/opt/ai-cam-security/.env.prod.local` for private values and overrides. Supply raw RTSP
credentials, host, bot token, numeric allowlisted user IDs and the fixed notification
chat ID there. Do not paste secrets into chat or commit them. Set owner to the
service account and permissions to `0600`; source/model/WAV files need only read
access. Give the account write access to `/opt/ai-cam-security/volume`.

The application reads the env file itself. The unit intentionally does not use
systemd EnvironmentFile syntax, which differs from dotenv quoting rules.
Paths resolve from the project root, independently of `STORAGE_FOLDER`.

Use the bundled pinned FP32 and INT8 artifacts and checksums in the benchmark
report for comparison. Verify with `sha256sum` and place the eventual
selected runtime artifact at `system/models/person.onnx`. Runtime never downloads
models. Offline readiness accepts either pinned candidate hash; this does not
mean its Pi performance/accuracy gate has passed.

Supply PCM WAV files that you have permission to use. Audio recordings are not
distributed with the public repository; local WAV files and backups are ignored.
The following layout illustrates the default Czech and English choices:

```text
system/sounds/common/alarm.wav
system/sounds/cz/first_warning.wav
system/sounds/cz/countdown_1.wav
system/sounds/cz/countdown_2.wav
system/sounds/cz/countdown_3.wav
system/sounds/en/first_warning.wav
system/sounds/en/countdown_1.wav
system/sounds/en/countdown_2.wav
system/sounds/en/countdown_3.wav
```

`cz` and `en` are defaults, not a fixed list of supported languages. To use any
other language, create `system/sounds/<language>/` with the same WAV filenames
and set `AUDIO_MAIN_LANGUAGE` and `AUDIO_LANGUAGES` in `.env.prod.local` to your
directory names. Use letters, numbers, underscores and hyphens in those names.
See the [French configuration example](../README.md#configuration).

Default playback: Czech then English for each key, then common alarm. A common
file takes precedence over language versions. Warning/countdown files play once;
the alarm WAV repeats throughout each siren period. Without a common file, the
main-language file is required; missing secondary-language files are skipped. Files must resolve inside
the sound root and have readable WAV headers. Set `AUDIO_DEVICE` to the verified
ALSA device (for example `plughw:CARD=<actual-card>,DEV=0`); `default` is only a
starting value. Use `aplay -l`/`aplay -L` to inspect actual devices. Verify access as
the service account without relying on an interactive desktop audio session.

To set device volume when arming, put an integer percentage in `.env.prod.local`:

```dotenv
AUDIO_DEVICE=plughw:CARD=UACDemoV10,DEV=0
AUDIO_VOLUME=75
```

Use the verified card name for your speaker. `AUDIO_VOLUME` accepts `0` to `100`;
empty or unset leaves volume unchanged. Each successful `/arm` sets the card's
`PCM` playback volume through `amixer` before arming. With volume configured,
`AUDIO_DEVICE` must be `default` or contain `CARD=`. Verify that the service account
can read the card's `PCM` mixer control:

```sh
sudo -u ai-cam-security amixer -c UACDemoV10 sget PCM
```

Restart after configuration changes. A missing mixer control, permission error or
volume-command failure rejects the arm attempt and preserves suppression.
Manual `/alarm` does not apply `AUDIO_VOLUME`.

Automatic alarms work whenever armed, day and night. The first warning starts
after 2 seconds of confirmed presence by day or 3 seconds at night (22:00–06:00,
Europe/Prague). Finish all warning/countdown files even after departure, then
start the siren only if a fresh detection still shows a person; uncertain absence
waits for detection or the 2-second confirmed-absence threshold.

Set `AUDIO_MAX_ALARM_SECONDS=60` and `AUDIO_MAX_ALARM_CYCLES=3` for the agreed
behavior: each started siren period finishes its 60 seconds even after departure.
At its boundary, stop if absence is confirmed; otherwise continue directly with
the siren, up to three total periods. The alarm file loops as needed, with no
repeat of warnings or countdowns between periods. Stop/disarm and faults still
interrupt playback. See [the behavior and settings](../README.md#commands-and-behavior).

## 3. Offline checks and measurement gate

```sh
poetry run ai-cam-security --env prod --check
poetry run ai-cam-security --env prod --check-ready
```

`--check`: exit 0 for valid configuration/storage, even if credentials/assets are
missing; component configuration flags are reported. No camera, audio or Telegram I/O.

`--check-ready`: exit 0 only with complete credentials, allowlist/destination,
pinned model, resolved valid WAVs, FFmpeg and `aplay` (plus `amixer` when
`AUDIO_VOLUME` is configured). Missing required WAV/model/
credential exits 1 naming the field or relative file, without secret values.
A missing secondary WAV is permitted. Malformed configuration exits 2. Readiness
does not test camera freshness, model throughput, speaker audibility or Telegram.

The commands above run as the current shell user. Also run them as the service
account from `/opt/ai-cam-security` to verify its configuration and file access:

```sh
sudo -u ai-cam-security /opt/ai-cam-security/.venv/bin/python -m app --env prod --check
sudo -u ai-cam-security /opt/ai-cam-security/.venv/bin/python -m app --env prod --check-ready
```

These checks do not exercise the unit's systemd sandbox.

Benchmark at least 60 seconds of labeled day and night footage for **both**
variants, following [the report](benchmarks/pi5-detection.md). Keep private footage
and generated JSON under ignored `volume/`. Agree acceptable false-alarm and
missed-person criteria with the user after initial results; record them and verify
both speed (>=2 fresh results/second) and accuracy before choosing the artifact.

With authorized live camera access, compare a visible clock/movement in the scene
with received frames. FFmpeg open/read timeouts are supplied at open; actual
timeout support and latency must be demonstrated on the installed backend.
Use the camera's low-latency substream if needed; change `RTSP_PATH` to its actual
path. Receipt timestamps are not camera exposure timestamps. Reject automatic
activation if delivered frames already exceed the 2-second freshness budget.

## 4. Review and install the unit only after approval

Review [`deploy/ai-cam-security.service`](../deploy/ai-cam-security.service).
It runs `/opt/ai-cam-security/.venv/bin/python -m app --env prod`, as the dedicated
account, with group audio access. It leaves audio devices visible, restarts after
failures in 5 seconds, and allows 20 seconds for process-group shutdown.

On the prepared Linux/Pi host:

```sh
systemd-analyze verify deploy/ai-cam-security.service
```

Expected: no unit syntax errors. Executable/path/account-related checks require
the prepared installation and account. This command was not available in the
Windows development environment and remains pending.

Concrete changes requiring authorization:

1. Install the reviewed source/venv/assets in `/opt/ai-cam-security`; create the
   dedicated account, audio membership and private env file permissions.
2. Copy the reviewed unit to `/etc/systemd/system/ai-cam-security.service` and run
   `systemctl daemon-reload`.
3. Start disarmed with `systemctl start ai-cam-security`; inspect
   `systemctl status ai-cam-security` and `journalctl -u ai-cam-security`.
4. Perform the explicit camera, speaker and Telegram checks below under supervision.
5. Only after acceptance, authorize `systemctl enable ai-cam-security` and a reboot
   test. Do not run PM2 and systemd for the same camera/bot.

If the journal reports `Failed to determine user credentials` or
`Failed at step USER`, systemd failed before launching Python. Inspect the
effective unit identity and account lookup:

```sh
systemctl show ai-cam-security -p User -p Group -p SupplementaryGroups
getent passwd ai-cam-security
getent group ai-cam-security
```

A missing account must be created as described in section 1. Successful manual
checks under your login user do not verify the service account. After correcting
the account and permissions, rerun the checks as that account before the
authorized service start. Inspect fresh journal entries for any subsequent errors.

## 5. Supervised acceptance record

Record date, configuration excluding secrets, artifact hash, pass/fail and measured
latency for each row. These tests **have not been performed**.

| Check | Expected result |
| --- | --- |
| Startup/status; unauthorized/private/group/edited commands | Disarmed; startup notice and status reply include all six keyboard commands; Telegram command menu registered; only allowlisted private original fresh commands execute |
| Day/night entry and stationary presence | Confirmation at 2/3 seconds with >=3 distinct frames; frozen profile per candidate |
| Complete incident | Recognized photo, still-there text, activated photo, deactivated photo in order |
| Leave during warning or each countdown key | Finish every warning/countdown file; confirmed absence at the end skips the siren and activated event |
| Leave during siren | Finish the current 60-second period, then stop if absence remains confirmed |
| Stay through three siren periods | Short WAV repeats; siren continues across period boundaries without warnings or duplicate activation; stops after three total periods |
| Leave and return before a siren period ends | Continue with the original repeat count; no extra periods granted |
| Return after confirmed departure ends the incident | Fresh confirmation starts a new warning and resets the period allowance |
| Two people become one | Incident remains active |
| Stop/disarm during playback | Decision-to-silence <=1 second; no later key starts |
| Manual siren while disarmed | Activated/deactivated only; stop or <=60-second maximum |
| Override active automatic siren | Uninterrupted playback; original deadline and single activated notification |
| Repeated manual alarm | No restart or deadline extension |
| Audio spawn/immediate exit/device failure | No false activation; audio fault remains through absence/recovery until successful `/arm` |
| Camera disconnect, frozen frames, reconnect | Automatic audio stops with fault; no stale-frame evidence; fresh confirmation after recovery |
| Vision child crash/hang | Child reaped/replaced; armed mode and suppression retained; stop/disarm effective during recovery |
| Internet outage and recovery | Already armed local protection continues while the camera remains reachable; bounded memory/retries; drop summary; no replay of old arm/alarm commands |
| Delayed photo delivery | Image and UTC timestamp belong to the event; stale/unavailable images labelled |
| Full restart/reboot, including without Internet | Disarmed; no restored playback; receiving `/arm` requires Telegram connectivity |
| Shutdown from partial startup or active incident | Audio stops first; no leaked player/vision processes; bounded cleanup |

Observe CPU, peak RSS, temperature and throttling during sustained operation.
Uncertain Telegram outcomes can produce duplicates; incident IDs/timestamps identify
them. Never interpret mocked tests as hardware acceptance. Keep unattended alarms
disabled until failures are resolved and the measurement and supervised gates pass.
