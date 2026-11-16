# Raspberry Pi installation and supervised acceptance

Requires Raspberry Pi OS 64-bit, Python 3.11-3.14, Poetry 2.x and `alsa-utils`.
Hardware acceptance remains pending.
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

English warnings/countdowns and a common alarm are bundled with the repository.
Additional local WAV files and backups are ignored. Supply recordings that you
have permission to use when adding languages. The bundled layout is:

```text
system/sounds/common/alarm.wav
system/sounds/en/first_warning.wav
system/sounds/en/countdown_1.wav
system/sounds/en/countdown_2.wav
system/sounds/en/countdown_3.wav
```

Set `AUDIO_MAIN_LANGUAGE` and `AUDIO_LANGUAGES` to add languages using the same
filenames under `system/sounds/<language>/`. Common files override language versions;
main-language files are required, missing secondary files are skipped.

Leave `AUDIO_DEVICE` empty for ALSA default, or set a verified device such as
`plughw:CARD=UACDemoV10,DEV=0`. Use your actual card name.
`AUDIO_VOLUME` accepts 0-100 and sets system mixer volume once at startup;
empty leaves volume unchanged. Volume control needs `amixer` and a default or `CARD=` device.

Use `/volume 50` for an immediate change without restarting. Changes do not persist;
restart reapplies `AUDIO_VOLUME`. `/arm` and `/alarm` leave volume unchanged.
Restart after environment-file changes.

### Diagnosing audio failures

Use `/audio_status` to see the configured audio device, backend, playback hardware
inventory, mixer identity/control, volume values and last failure timestamp/reason.
`/status` retains general service/host information and an audio-health summary.
Inspect JSON startup configuration and failure records:

```sh
sudo journalctl -u ai-cam-security -n 100 --no-pager
id ai-cam-security
sudo -u ai-cam-security aplay -l
sudo -u ai-cam-security aplay -L
sudo -u ai-cam-security amixer scontrols
systemctl cat ai-cam-security
```

These commands inspect configuration/devices without playing sound. Do not
paste private environment files or credentials into troubleshooting output.
For an explicit `CARD=...` output, also inspect that card's mixer controls with
`amixer -c <actual-card> scontents` as the service account. The app selects a
playback-volume `Master`, then `PCM`, then a single other playback-volume control.
Common volume controls are supported; capture-only controls are excluded.
Multiple other controls are reported as ambiguous and no write is attempted.
Mute switches are preserved. The selected mixer change is system-wide.

Empty/unset `AUDIO_DEVICE` becomes ALSA `default`; it does not discover or switch
to a USB speaker. Default PCM playback and the default mixer may route
differently, especially between a desktop user and the service account. Compare
the reported backend reason, card inventory, account/group permissions and
systemd restrictions before choosing an explicit device. No suitable playback
volume control prevents startup volume initialization and `/volume` changes,
even if playback itself would work. The app never silently switches speakers.
The tracked production volume is 100; local files/process variables may override
it. Verify effective settings rather than assuming the tracked defaults apply.

With approval for audible output, test a known WAV using `aplay -D default`
as the service account, then the actual explicit device if needed. Test mixer
selection separately. A speaker/device/configuration change or service restart
needs approval; no automatic fallback device is chosen. Offline checks cannot
verify device opening, physical sound, routing, or volume.

Manual `/alarm` can start without camera credentials, fresh RTSP frames or a
model, provided the configured WAV sequence and `aplay` are available. Missing
`amixer` does not block it. With approval, test an unreachable camera in a test
configuration: `/alarm` must play, `/stop` must stop, and `/arm` must refuse until
fresh observations return. A camera failure during a manual siren must leave it
running to its single-period limit; automatic playback must still stop on vision
faults. No incident photo is required for manual sound.

`/status` also reports CPU temperature, service uptime, load averages, host memory
and last frame age. Compare temperature to the CPU thermal zone, memory to
`/proc/meminfo`, and load to `uptime` during supervised Pi acceptance. Metrics
refresh every five seconds; unavailable values do not block control commands.
The last audio error is historical and remains visible after a successful retry.

### Keeping application defaults portable across Linux hosts

Keep `AUDIO_DEVICE` empty to use the Linux default. Configure the intended
default once on each host; the app does not infer which physical speaker you
intend to use. Desktop audio-manager defaults and a headless system account's
ALSA defaults may differ.

If default playback fails with `audio open error: Unknown error 524`, inspect
ALSA routing. On one reported Pi, default playback/mixer targeted HDMI while the
speaker was USB `UACDemoV10`. Setting both defaults to USB resolved it; other
524 errors may have different causes.

Run the following commands as the account running the app, from the project
directory. A personal login and a dedicated service account may have different
ALSA defaults. First inspect the existing configuration without playing sound:

```sh
id
aplay -L
cat /etc/asound.conf "$HOME/.asoundrc"
amixer -c UACDemoV10 scontents
```

Missing-file errors from `cat` mean those override files do not exist. Substitute
your actual USB card name on another host. With approval for audible output,
test that card directly; this plays one second of the bundled alarm:

```sh
aplay -D plughw:CARD=UACDemoV10,DEV=0 -d 1 system/sounds/common/alarm.wav
```

If direct playback fails, investigate that error before changing the default.
If it works and this is the intended speaker, review any existing configuration
and approve the system-wide routing change before editing `/etc/asound.conf`:

```sh
sudoedit /etc/asound.conf
```

For this Pi, create the following configuration, or merge it with existing
settings rather than replacing unrelated configuration:

```text
pcm.!default {
    type plug
    slave.pcm "hw:CARD=UACDemoV10,DEV=0"
}
ctl.!default {
    type hw
    card "UACDemoV10"
}
```

This configuration is specific to that Pi; the application does not create or
replace host files. Other hosts use their own card names or audio-manager
defaults. ALSA documents
[system-wide and per-user defaults](https://www.alsa-project.org/wiki/Asoundrc).
Configuring both playback and control defaults keeps these aliases pointed at
the same card. Leave `AUDIO_DEVICE` empty, unset or explicitly `default`.

Verify as the account running the app. The second command plays another second
of alarm and requires approval for audible output:

```sh
amixer info
aplay -D default -d 1 system/sounds/common/alarm.wav
```

`amixer info` should now identify `UACDemoV10`. Retry `/alarm` and `/audio_status`;
the latter should report the USB mixer and `PCM` volume control. Its last-error
history can still contain the earlier failure after playback succeeds. The
operator reported working playback after this change; no agent-run live Pi
test was performed. Listing a USB card alone does not prove sound output.

See [alarm behavior](../README.md#alarm-behavior) for presence, countdown and siren rules.
Day/night confirmation uses `DETECTION_DAY_CONFIRM_SECONDS` and
`DETECTION_NIGHT_CONFIRM_SECONDS` from the effective configuration.

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

After reviewing the unit and approving installation/start on the target host:

```sh
sudo cp deploy/ai-cam-security.service /etc/systemd/system/ai-cam-security.service
sudo systemctl daemon-reload
sudo systemctl start ai-cam-security
sudo systemctl status ai-cam-security --no-pager
sudo journalctl -u ai-cam-security -n 100 --no-pager
```

Enable boot startup only after supervised acceptance with
`sudo systemctl enable ai-cam-security`, then verify a reboot.
Use one process manager per camera/bot.

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
latency for each row. Full supervised acceptance remains pending.

| Check | Expected result |
| --- | --- |
| Startup/status; unauthorized/private/group/edited commands | Disarmed; startup notice and status reply include all command buttons; Telegram command menu registered; only allowlisted private original fresh commands execute |
| Day/night entry and stationary presence | Configured day/night confirmation with >=3 distinct frames; frozen profile per candidate |
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
