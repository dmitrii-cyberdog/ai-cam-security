# Reporting security issues

Use GitHub's private vulnerability reporting when enabled for this repository.
Otherwise, contact connect@cyberdog.top. Do not include credentials, camera footage
or other private information in public issues or pull requests.

Security fixes target the latest revision. This project is experimental;
hardware acceptance remains pending as described in the README and Pi guide.

Keep RTSP credentials, bot tokens and real Telegram IDs in ignored `.env.local`
or `.env.<environment>.local` files, or supply them through process environment
variables. The tracked `.env`, `.env.dev` and `.env.prod` are public defaults.
Keep camera footage in ignored `volume/` or `system/samples/`, and local WAVs
in ignored `system/sounds/`. Review Git history as well as current files before
publishing. Rotate any credentials that have been committed or publicly exposed.

Camera images are held in memory and incident photos are sent to the configured
Telegram destination. Treat that destination and any exported logs or benchmark
recordings as private data.
