# ai-cam-security

RTSP person detection with local audio alarms and Telegram control, built for Raspberry Pi 5.
Detection runs locally using OpenCV NanoDet. English warnings, a siren and pinned models are included;
additional languages use your own WAV recordings.

**Experimental: hardware acceptance is pending.** Complete the [Pi checks](docs/raspberry-pi.md)
before unattended use. Every restart starts **disarmed**.

## Quick start

Requires Python 3.11–3.14 and Poetry 2.x. Audio playback targets Raspberry Pi/Linux with `alsa-utils`.
Run from the project root:

```sh
poetry install
```

Copy [`.env`](.env) to `.env.local`. Set `RTSP_LOGIN`, `RTSP_PASSWORD`, `RTSP_HOST`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` and `TELEGRAM_CHAT_ID` in that private copy.
Allowed user IDs are comma-separated numbers; commands work only in authorized private chats.

```sh
poetry run ai-cam-security --check
poetry run ai-cam-security --check-ready
poetry run ai-cam-security
```

`--check` validates configuration and storage. `--check-ready` also checks credentials,
models, WAVs and installed backends. Both are offline; neither tests live hardware or Telegram.
Stop with Ctrl+C. Use `/status`, verify camera and audio, then `/arm` to enable automatic alarms.

For deployment, follow the [Raspberry Pi guide](docs/raspberry-pi.md).

## Telegram commands

| Command | Action |
| --- | --- |
| `/status` | Protection, camera, audio and host health |
| `/audio_status` | Audio devices, mixer, volume and last failure |
| `/photo` | Request a fresh camera image |
| `/arm` | Check readiness, clear suppression and enable automatic alarms |
| `/disarm` | Disable automatic alarms and stop playback |
| `/alarm` | Play a manual siren for up to 60 seconds, even while disarmed |
| `/stop` | Stop playback; keep armed mode and require fresh detection confirmation |
| `/volume 50` | Set system playback volume, from 0 to 100 |
| `/help` | Show commands |

## Alarm behavior

Confirmed presence starts a warning and countdown. These finish even if the person leaves.
A fresh detection after the countdown starts the siren; confirmed absence skips it.
Each siren period finishes before checking absence, up to three 60-second periods by default.
`/stop`, `/disarm` and faults interrupt automatic playback.

Camera loss stops automatic audio; recovery requires fresh confirmation. Audio faults require
successful `/arm` to resume automatic alarms. Manual `/alarm` works without the camera.
An Internet outage leaves already armed local protection running while the camera stays reachable;
Telegram control and notifications need connectivity. A full restart still starts disarmed.

## Configuration

[`.env`](.env) lists settings. Load order, with later values overriding earlier ones:
`.env`, `.env.local`, `.env.<environment>`, `.env.<environment>.local`, process environment.
Select an environment with `--env prod` (or `ENV`); use `.env.prod.local` for private production values.
Restart after configuration changes. Paths resolve from the project root.

- **Detection:** confidence, day/night confirmation, absence timing and optional siren photos.
- **Audio:** `AUDIO_DEVICE` selects an ALSA output; empty uses the system default.
  `AUDIO_VOLUME` sets startup mixer volume; Telegram volume changes do not persist across restarts.
- **Languages:** add `system/sounds/<language>/` with the WAV names from `AUDIO_SEQUENCE`,
  then set `AUDIO_MAIN_LANGUAGE` and `AUDIO_LANGUAGES`. Common WAVs override language versions;
  main-language files are required, missing secondary files are skipped.

Keep credentials, camera footage and additional recordings out of commits.
Logs are JSON lines on stdout; incident photos are held in memory and sent to Telegram.

## Further reading

- [Pi installation, audio troubleshooting and acceptance](docs/raspberry-pi.md)
- [Updating an installed service](docs/update.md)
- [Detector benchmarks and pending hardware checks](docs/benchmarks/pi5-detection.md)
- [Development and contribution checks](CONTRIBUTING.md)
- [Security reporting](SECURITY.md)

Licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party attribution.
