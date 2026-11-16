# Updating the Raspberry Pi service

For an existing systemd installation at `/opt/ai-cam-security`.
For initial setup, see the [Pi guide](raspberry-pi.md).
Every restart starts **disarmed**. Resolve failed steps before continuing.

## Update and check

Run as the account managing the checkout and Poetry environment:

```sh
cd /opt/ai-cam-security
sudo systemctl stop ai-cam-security
git pull --ff-only
POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --only main
poetry env info --path
```

Stop before pulling to avoid running mixed versions. The virtual environment must be
`/opt/ai-cam-security/.venv`, matching the unit. Review changed settings in [`.env`](../.env);
keep private overrides in `.env.prod.local`.

Check file access and local readiness as the service account:

```sh
sudo -u ai-cam-security .venv/bin/python -m app --env prod --check
sudo -u ai-cam-security .venv/bin/python -m app --env prod --check-ready
```

Both must pass. These checks do not test live camera, speaker, Telegram or the systemd sandbox.

## If the systemd unit changed

Review and validate the unit, then install it:

```sh
systemd-analyze verify deploy/ai-cam-security.service
sudo cp deploy/ai-cam-security.service /etc/systemd/system/ai-cam-security.service
sudo systemctl daemon-reload
```

Skip this step for application, dependency, asset or environment-only changes.

## Start and verify

```sh
sudo systemctl start ai-cam-security
sudo systemctl status ai-cam-security --no-pager
sudo journalctl -u ai-cam-security --since "2 minutes ago" --no-pager
```

Confirm the Telegram startup notice and command menu. Use `/status` to verify disarmed mode
and fresh camera/detector observations. Under supervision, test `/alarm` and `/stop`,
then `/arm` when ready. Successful `/arm` clears audio-fault suppression.

For configuration-only changes, run both checks, then `sudo systemctl restart ai-cam-security`
and repeat verification. No `daemon-reload` is needed.
