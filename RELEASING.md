# Releasing tlumi

Maintainer runbook. The release pipeline is: GitHub Release published -> `.github/workflows/release.yml` -> lint + tests + version check + build -> PyPI Trusted Publishing (OIDC, no API token).

## One-time setup (before the repo goes public)

These are repo/PyPI settings, not code. All of them must exist before the first release; the publish job fails without the first two.

1. **Create the `pypi` GitHub environment**: done (created 2026-07-01 via API). Once the repo is PUBLIC, add yourself as a required reviewer on the environment (Settings -> Environments -> pypi); reviewer rules are unavailable on private free-plan repos, and without one a publish runs with no human approval gate.
2. **Register the PyPI pending trusted publisher** at https://pypi.org/manage/account/publishing/ (before the project exists, use a *pending* publisher):
   - PyPI project name: `tlumi`
   - Owner: `jaydoubleu`, repository: `tlumi`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. **Enable branch protection (or a ruleset) on `main`**: require the CI checks to pass and require PRs. A GitHub Release is the only gate on PyPI publication, so `main` and release creation must not be writable by a drive-by credential.
4. **Enable private vulnerability reporting** once the repo is public: Settings -> Security -> "Private vulnerability reporting" (the API 404s while private; SECURITY.md points reporters there).
5. **Set the repo description and topics**: done (2026-07-01).
6. **Publish to PyPI promptly after the repo goes public.** The project name is only reserved once the first release uploads; until then anyone can squat `tlumi`.

## Cutting a release

1. **Bump the version** in `pyproject.toml` (`[project].version`) and run `uv lock` to refresh `uv.lock`.
2. **Date the CHANGELOG**: rename the `[Unreleased]` section to `[X.Y.Z] - YYYY-MM-DD` and start a fresh empty `[Unreleased]` above it.
3. **First release only, or when install instructions change**: flip the README install section from the "not yet on PyPI" note to `pip install tlumi` / `uv tool install tlumi`, and add the PyPI version badge:
   `[![PyPI](https://img.shields.io/pypi/v/tlumi)](https://pypi.org/project/tlumi/)`
4. **Sanity-check locally**:
   ```bash
   uv run python -m pytest tests/ -q
   uv run ruff check src/ tests/ examples/ && uv run mypy
   uv build && uvx twine check dist/*
   tar -tzf dist/*.tar.gz | grep -Ev '^tlumi-[0-9][^/]*/(src/|tests/|examples/|PKG-INFO|pyproject|README|CHANGELOG|CONTRIBUTING|SECURITY|DESIGN|LICENSE|\.gitignore)' # anything printed is unexpected
   ```
5. **Commit, push, and wait for CI to go green** on `main`.
6. **Tag and release**: create a GitHub Release with tag `vX.Y.Z` targeting the green commit (the workflow rejects a tag that does not match `pyproject.toml`). Paste the CHANGELOG section as the release notes.
7. **Approve the `pypi` environment deployment** when the workflow pauses for review.
8. **Verify**: `pip install tlumi==X.Y.Z` in a scratch venv, run `tlumi version`, and check https://pypi.org/project/tlumi/ renders the README correctly.

## After the release

- Confirm the CHANGELOG on `main` has an empty `[Unreleased]` section.
- Close the GitHub milestone (if any) and triage anything that slipped.
- If the release contained a security fix, publish the GitHub Security Advisory referencing the fixed version.
