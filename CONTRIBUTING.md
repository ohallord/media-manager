# Contributing to Media Manager

## Set up

You need Python 3.11, ffmpeg, and git. From a checkout:

```bash
./setup.sh
```

`setup.sh` creates a virtualenv, installs dependencies, and writes `.env` with a random `JWT_SECRET` and an admin password. The password is printed once. Save it. If `INITIAL_ADMIN_PASSWORD` is unset, no admin account is created. There is no default password.

Start the portal:

```bash
source .venv/bin/activate
set -a && source .env && set +a
python3 main.py serve
```

The portal listens on `http://localhost:8088`. Install, Docker, and Helm notes are in [docs/INSTALL.md](docs/INSTALL.md).

## Tests

Run tests through the wrapper. It points `DB_PATH` at a throwaway SQLite file, so the tests do not use a real library database.

```bash
pip install -r requirements.txt coverage
bash scripts/run_tests.sh
bash scripts/run_tests.sh --coverage
coverage report --fail-under=90
```

The published GitHub Actions workflow runs `bash scripts/run_tests.sh --coverage`, then `coverage report --fail-under=90`. The workflow also runs:

```bash
helm lint k8s/helm --set-string secrets.JWT_SECRET=ci-only
```

`ci-only` is the placeholder in that public workflow. It is not a credential you should reuse outside the lint.

## Repository layout

| Path | Contents |
| --- | --- |
| `main.py` | Portal entry point |
| `media_manager/` | FastAPI portal, integrations, and the web UI |
| `tests/` | Tests launched by `scripts/run_tests.sh` |
| `scripts/run_tests.sh` | Test wrapper |
| `k8s/helm/` | Helm chart |
| `docs/` | Install, feature, and design notes |
| `setup.sh` | Local virtualenv and `.env` setup |

## Before you open a pull request

- Run `bash scripts/run_tests.sh` when behavior changes.
- Lint `k8s/helm/` with the command above when the chart changes.
- Do not commit `.env`, passwords, API keys, or tokens.
- Put user-visible notes under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md). Leave older tags as they are.

## Pull requests

Open a pull request on the public repository. Say what changed and how you tested it.

For a change in behavior, open an issue first. Describe the problem, the approach, and what it means for an existing install.

## Code of conduct

People taking part in this project are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Do not open a public issue for a vulnerability. Follow the [security policy](https://github.com/ohallord/media-manager/security/policy).

## License

MIT. See [LICENSE](LICENSE). By contributing, you agree that your contributions are licensed under the MIT license.
