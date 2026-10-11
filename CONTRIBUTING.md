# Contributing

Use Python 3.11–3.14 and Poetry 2.x. Run from the project root:

```sh
poetry install
poetry run ai-cam-security --check
poetry run python -m unittest discover -s app_tests -v
poetry run mypy
poetry check --strict
```

Tests use synthetic frames and fake camera, audio and Telegram boundaries.
Contributions must keep these checks offline. Passing tests does not replace
the Raspberry Pi measurements and supervised acceptance in the deployment guide.

Describe the behavior change and checks performed in your pull request. Follow
the surrounding code style and keep changes focused. Keep real credentials,
Telegram IDs, recordings, local sound assets and generated benchmark output out
of commits. Report security issues using [SECURITY.md](SECURITY.md).

Contributions are licensed under [Apache-2.0](LICENSE). Retain third-party
attribution and document the source and redistribution license of new assets.
