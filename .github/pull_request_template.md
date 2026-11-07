## What and why

## How it was tested

## Checklist
- [ ] Behavior changes were exercised with `bash scripts/run_tests.sh`
- [ ] Coverage was checked with `bash scripts/run_tests.sh --coverage` and `coverage report --fail-under=90` when the change affects covered code
- [ ] Chart edits were linted with `helm lint k8s/helm --set-string secrets.JWT_SECRET=ci-only`
- [ ] User-facing notes are in the docs and under `[Unreleased]` in `CHANGELOG.md`
- [ ] The diff has no passwords, tokens, or `.env` values
- [ ] This pull request is not a vulnerability report (`SECURITY.md`)
