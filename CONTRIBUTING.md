# Contributing to tlumi

Thanks for your interest. This project is small and opinionated; the bar for contributions is "does it match the codebase's existing patterns and pull its weight." Drive-by PRs are welcome, but please open an issue first for anything beyond a small fix so we can agree on direction.

## Development setup

tlumi uses [uv](https://github.com/astral-sh/uv) for dependency management. The lockfile (`uv.lock`) is committed for reproducible builds.

```bash
git clone https://github.com/jaydoubleu/tlumi.git
cd tlumi
uv sync --dev
```

That installs tlumi in editable mode plus all dev tools (pytest, mypy, ruff, bandit) at the locked versions.

A plain venv + pip works too if you prefer:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest pytest-mock
```

## Run the tests

```bash
uv run python -m pytest tests/
```

The full suite runs in a few seconds and should pass on a clean checkout.

## Lint and format

```bash
uv run ruff check src/ tests/ examples/
uv run ruff format --check src/ tests/
uv run mypy
uv run bandit -r src/ -q
```

CI runs the same checks (plus the test matrix on Python 3.10 to 3.13) on every PR; please run them locally first.

## Code conventions

The full conventions guide lives in [`.claude/CLAUDE.md`](.claude/CLAUDE.md). Highlights:

- Python 3.10+ with modern type hints (`X | None`, not `Optional[X]`).
- Typer for the CLI surface; Rich for terminal output.
- Dataclasses for config models (frozen + slots where it matters); no Pydantic.
- Errors raise `TlumiError` subclasses; the CLI boundary in `cli.py` catches them.
- Engine errors flow through `catch_engine_errors(handler, message)`.
- User-controlled strings (resource names, types, paths) pass through `rich.markup.escape()` before f-string interpolation; raw data uses `markup=False, highlight=False`.
- JSON-mode output paths must emit structured JSON instead of Rich markup. This is the project-wide contract.
- Symlink checks (`is_symlink()`) precede every `mkdir` or file write on a user-controlled path.

### No em-dashes

Project style rule: no em-dash characters (U+2014) anywhere in the codebase, docs included. Rephrase the sentence; do not paste `--` as a substitute. Reviewers will ask for changes on PRs that introduce them.

### Comments

Default to writing no comments. Add one only when the WHY is non-obvious: a hidden constraint, a subtle invariant, a workaround for a specific upstream bug. Code that explains itself does not need a comment that paraphrases it.

## Architecture

See [`DESIGN.md`](DESIGN.md) for the architecture and the ADRs (Architecture Decision Records) that explain why specific choices were made. Before proposing a substantial change, please skim the relevant ADR; if your proposal contradicts one, the PR description should make the case for revisiting it.

## Pull request flow

1. Open an issue first if your change is non-trivial. We will agree on direction before code lands.
2. One PR per logical change. Bundled refactors are harder to review and revert.
3. Run tests, ruff check, and ruff format locally before pushing.
4. PR description: explain what changed and why. The "why" matters more than the "what" (the diff covers the what).
5. Keep commits small and atomic. Squash on merge.

## Reporting bugs

Open an issue with:

- tlumi version (`tlumi version`)
- Python version (`python --version`)
- OS
- Minimal reproduction (paste `tlumi.yaml` and a few lines of `infra.py`)
- Full output of `tlumi <command> --verbose`

For security-sensitive issues, see [SECURITY.md](SECURITY.md) instead.

## License

By contributing, you agree your contribution will be licensed under Apache-2.0, the project license.
