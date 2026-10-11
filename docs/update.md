# Updating the Raspberry Pi service

Use this procedure for an existing systemd installation at
`/opt/ai-cam-security`. Initial setup is covered by the
[Raspberry Pi guide](raspberry-pi.md).

Application code and environment changes require a service restart. Every full
restart starts **disarmed**; arm again explicitly after verification.

## 1. Stop the service and pull the new version

Run on the Pi as the account used to manage the checkout and Poetry environment:

```sh
cd /opt/ai-cam-security
sudo systemctl stop ai-cam-security
git pull --ff-only
```

Stop before pulling so the running service cannot load a mixture of versions.
If you already pulled the new version, stop the service and skip the pull command.
If any update command fails, resolve the failure before continuing.

## 2. Install dependencies and check the installation

```sh
POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --only main
poetry env info --path
```

The environment path must be `/opt/ai-cam-security/.venv`, matching the unit's
`ExecStart`. If it differs, resolve the environment location before starting.
Use `poetry install` to install the versions in the pulled `poetry.lock`.

Keep private configuration and local overrides in `.env.prod.local`. For the
USB speaker verified with `aplay` during setup, the working override is:

```dotenv
AUDIO_DEVICE=plughw:CARD=UACDemoV10,DEV=0
AUDIO_VOLUME=75
```

Use your verified device name if the speaker hardware differs. Review new settings
in the version's `.env` reference and release instructions before running checks.
`AUDIO_VOLUME` is an optional integer percentage from `0` to `100`, applied to the
card's `PCM` playback volume before arming. Empty or unset keeps the current volume.
It requires `amixer` from `alsa-utils` and a working `PCM` mixer control.

Run both checks as the service account, from `/opt/ai-cam-security`:

```sh
sudo -u ai-cam-security .venv/bin/python -m app --env prod --check
sudo -u ai-cam-security .venv/bin/python -m app --env prod --check-ready
```

Continue only if both checks pass. They validate configuration, local assets and
installed backends; they do not test live camera, speaker or Telegram operation,
or the unit's systemd sandbox.

## 3. Install the unit only if it changed

If `deploy/ai-cam-security.service` changed, review its changes and verify it:

```sh
systemd-analyze verify deploy/ai-cam-security.service
```

After resolving any errors, install the reviewed unit and reload systemd:

```sh
sudo cp deploy/ai-cam-security.service /etc/systemd/system/ai-cam-security.service
sudo systemctl daemon-reload
```

Skip this step for changes only to application code, dependencies, assets or
environment files. `daemon-reload` rereads unit definitions and does not restart
the application.

## 4. Start and verify

```sh
sudo systemctl start ai-cam-security
sudo systemctl status ai-cam-security --no-pager
sudo journalctl -u ai-cam-security --since "2 minutes ago" --no-pager
```

In Telegram, confirm the startup notice arrives in `TELEGRAM_CHAT_ID` with the start
time, disarmed reminder and a keyboard containing `/status`, `/arm`, `/disarm`,
`/alarm`, `/stop` and `/help`. Check that Telegram's command menu lists the same six
commands. Startup setup and notification delivery retry if Telegram is offline.

Run `/status` and confirm its formatted reply groups protection, system health and
notifications, includes the keyboard, shows the service is disarmed,
and the camera and detector have fresh observations. Under supervision, run `/alarm`, confirm sound,
then `/stop`. Run `/arm` when ready for automatic detection. A successful `/arm`
clears audio-fault suppression.

## Configuration-only changes

After editing `.env.prod.local`, run the two checks in section 2. If both pass,
restart to load the new values:

```sh
sudo systemctl restart ai-cam-security
```

Then repeat the verification in section 4. The restart starts disarmed.
No `daemon-reload` is needed for environment-file changes.
