# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed (publishability audit sweep, 2026-07)
- **Critical:** secrets nested inside composite stack outputs (a dict/list output containing a `pulumi.Output.secret(...)` member) were printed in cleartext by default by `tlumi apply` and `tlumi output` (human, `--json`, and `--raw`). The Automation API marks only whole-output secrets, so default-masked display now sources values from exported state, where every secret keeps its wrapper and is masked as `(sensitive)`. `--show-secrets` is unchanged.
- Interactive `tlumi apply` no longer skips real work with "No changes": output-only edits (changing a `pulumi.export()` value) and pending imports now reach the confirmation prompt. Previously they were applied by `--auto-approve` but silently dropped in the default interactive flow.
- Imported resources (`ResourceOptions(import_=...)`) are now visible: plan/apply display `= ... will be imported` lines, summaries count "N to import"/"N imported", `--json` change envelopes include an `import` count, and `plan --json` gains an `outputs_changed` boolean.
- Rich emoji shortcode substitution is disabled on all consoles: output values containing `:name:` sequences (IPv6 addresses, MAC addresses, literal `:tada:`) are no longer silently rewritten at display time.
- `tlumi import` now records imported resources unprotected by default (Terraform parity), so a later `tlumi destroy` works without manual state surgery; pass `--protect` to opt into Pulumi's protection behavior. Import failures now surface engine diagnostics instead of a bare error.
- Module-cache eviction now covers symlinked entry layouts (`src -> ../shared`, symlinked `infra.py`): helper modules reached through symlinks are re-executed between preview and apply, closing a reopened path to the interactive-apply resource-loss bug.
- Stack config cleanup is now driven by a sidecar of keys tlumi actually wrote (`.tlumi/cache/managed_config_keys.json`); a project named like a provider namespace (e.g. `aws`) can no longer have real provider config (`aws:region`) deleted. On upgrade the first run skips cleanup and records the sidecar.
- `_mask_backend_url()` fails closed: an unparseable backend URL renders as a redacted placeholder instead of raising (non-numeric port) or leaking the raw URL.
- `state push` symlink refusal now covers symlinked parent directories for relative paths and opens the leaf with `O_NOFOLLOW` (TOCTOU-free); absolute paths still allow platform symlinks like macOS `/tmp`.
- `state mv` destination `type::name` parsing splits on the first `::` (consistent with resource resolution), so destination names containing `::` rename correctly; `resolve` also handles `type::name` identifiers whose name segment contains `::`.
- Property diffs filter Pulumi-internal dunder keys (`__defaults`) recursively, and nested sub-key comparison is type-aware so `True` vs `1` registers as a change.
- Variable coercion rejects lossy non-decimal YAML int forms (`0777` octal, `1:30` sexagesimal) with a quote-the-value hint, matching the existing float rejection.
- `redact_text()` also masks passwords in empty-username URLs (`scheme://:pass@host`) and hyphenated keys (`secret-key`).
- Re-running `tlumi init` on an initialized project re-enforces `0o700` on `.tlumi/`, takes the project name from `tlumi.yaml` instead of the directory name (a checkout dir like `my.repo` no longer fails), and non-interactive init prints the `TLUMI_SECRETS_PASSPHRASE` reminder.
- `tlumi clean` no longer prints alarming "Unlinking symlink" warnings for expected venv-internal symlinks, and its help text states that state and backups are preserved by default.
- CLI polish: unknown commands show a single "Did you mean" suggestion; an early-closed stdout pipe (`tlumi show | head`) exits 0 quietly instead of a silent exit 1; Ctrl+C in windowless commands (`state pull`, `output --raw`) writes to stderr, keeping stdout a clean data channel; the missing-module hint now suggests `tlumi deps add <package>` with the actual module name.
- Zero-change applies no longer print a dangling empty `Resources:` header ("No resources changed." instead).
- `tlumi show` and `state show` displays filter internal dunder keys; `state show` renders Inputs/Outputs JSON with correct indentation. (`state pull` and `state show --json` remain byte-faithful.)
- `~/.tlumi/cache/pulumi/` directories are created `0o700` at every level, matching the uv cache path.

### Changed (publishability audit sweep, 2026-07)
- Pinned uv auto-download bumped from 0.7.12 to 0.11.26 (SHA-256 verification unchanged).
- `pyyaml` floor raised to 6.0.1 (6.0 cannot build on Python 3.12+).
- sdist include patterns are anchored and `todo_continue/` is excluded, so internal files can never ride into the PyPI tarball via unanchored `README.md`-style patterns; CI and the release workflow verify the sdist contains no internal files.
- CI/release hardening: all GitHub Actions pinned to commit SHAs, the release workflow re-runs lint + tests on the tagged commit, verifies the tag matches `pyproject.toml`, builds without the shared Actions cache, and checkouts no longer persist the GITHUB_TOKEN. A Python 3.14 experimental lane was added.
- Community/launch scaffolding: `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), issue forms and a PR template, Dependabot config for actions and pip, `RELEASING.md` runbook, `Typing :: Typed` classifier, PyPI sidebar URLs (Issues, Changelog), and a trademark/affiliation note in the README.

### Fixed (pre-launch multi-agent review sweep)
- **Critical:** interactive `tlumi apply` could destroy resources the approved plan never showed. Helper modules imported by `infra.py` were cached across the in-process preview→up, so module-level resource registrations ran only once; the program is now fully re-executed (entry + all project-local imports) before each operation.
- Backend-URL credentials no longer leak in `Failed to initialize workspace` (and sibling) errors: Pulumi `CommandError` text is run through `redact_text()`, which now also masks URL-embedded credentials (`scheme://user:pass@host`) and Azure SAS params (`sig`/`sv`/`se`).
- Machine-output contract: workspace notices/warnings go to a stderr console, `output --raw` writes via `sys.stdout.write` (no width-wrap corruption) and runs windowless, `state pull` runs windowless, and user-program `print()` is redirected to stderr in `--json` mode -- so stdout stays a clean data channel. `output NAME` on an empty stack now errors instead of exiting 0.
- Property diffs no longer silently drop a changed value that differs only past the 56-char truncation point (raw values are compared, formatted only for display; colliding truncations re-truncate around the divergence point).
- Preview failures now report real error detail instead of a bare `Preview failed.`; provider/program **warnings** are surfaced (console + `warnings` JSON key) instead of dropped; failed resource ops stop animating; `tlumi refresh` shows per-resource progress.
- The hidden `pulumi:pulumi:Stack` resource is excluded from plan/apply/destroy/refresh counts, so summaries match the resource listing and an empty program's first apply short-circuits at "No changes".
- `state mv` parses URNs positionally (names containing `::` rename correctly) and now allows renaming a parent that has children; `state push` rejects a state file from a different project; `state unlock` distinguishes "released a stale lock" from "no lock present".
- `config`: the README's symlinked multi-environment layout is accepted (lexical entry-path check), YAML-coerced float variable values are rejected (prevents `1.10` -> `1.1` corruption), and unknown/typo'd `tlumi.yaml` keys warn with a "did you mean" suggestion. The `tlumi.yaml` template quotes the project name so YAML-keyword names (`no`, `off`, `true`...) work. Note: numeric-looking variable values that must keep an exact form (zero-padded IDs like `007`, underscored numbers like `1_000`) should be quoted; YAML coerces unquoted integers and tlumi cannot recover the original text.
- A second adversarial review round (verifying the fixes themselves) caught and fixed: a ReDoS in the new URL-credential redaction regex (bounded quantifiers), two un-redacted `pulumi config set/rm` error sites, duplicated provider/program warnings in interactive apply, `state unlock` mis-reporting on a custom `backend.url`, and human error text on the stdout data channel for `state pull`/`output --raw` (now stderr). The `#13` resource-count fix was also corrected to count displayed resources rather than `change_summary` (which mis-handled the hidden Stack vs. default provider).
- Platform/packaging: `typer>=0.16` floor (older typer ships a non-functional CLI), Windows is rejected with a clear "use WSL2" message, musl (Alpine) systems get the static uv build with an accurate error hint, and `deps add` rejects leading-whitespace pip-directive injection.
- `tlumi init` re-runs the idempotent dependency install on an already-scaffolded project (a prior failed install no longer reports false success), and its secret-encryption prompt no longer gets erased by the Live refresh thread.

### Added
- `runtime: bool = True` parameter on `get_stack()`. State and recovery commands (`state list/show/rm/mv/pull/push`, `show`, `state unlock`, and now `output`) run with `runtime=False`, skipping the venv check, `infra.py` load, and stack-config reconciliation. Recovery works even when the project is broken.
- `_mask_backend_url()` now masks bare-username-as-token netlocs and case-insensitive Azure SAS query parameters (`AccessKey`, `SharedAccessKey`, `SAS`, `Signature`, etc.), plus a broadened keyset and substring matching that catches `authToken`, `apiKey`, `Authorization`, `Bearer`, `awsSecretAccessKey`, `oauth_token`, etc. URL fragments containing `=` are now parsed as queries so `https://x#sig=...`-style tokens no longer leak.
- `tlumi.redact` module: free-form credential scrubber applied to Pulumi engine diagnostics and `EngineError.full_output`. Catches AWS access keys (`AKIA...`), JWT-shaped tokens, `Authorization: Bearer ...`, `Password=...;` connection-string fragments, GCP PEM private keys, and generic `password=`/`secret=`/`token=`/`apikey=` key-value pairs.
- `state push` deep validation: URNs must start with `urn:pulumi:`; `version` must be a positive integer; `parent`, `dependencies`, `provider`, and `propertyDependencies` references must resolve to URNs declared in the same envelope. Malformed or dangling references are rejected before `import_stack`.
- JSON-mode error envelopes now include `callback_errors` count when on_preview/on_update callbacks raise during a `--json` run, so diff-extraction or rendering crashes are no longer silent.
- `_handle_engine_error` accepts a defensive `json_output` parameter and re-routes to `_emit_json_error` if a future caller forgets the existing branch in `_run`.
- `--verbose` now lowers the `_WorkspaceNoticeHandler` from INFO to DEBUG so internal diagnostics ("config cleanup skipped", venv-lib unreadable, backup rotation failures) surface to the user.
- `_create_backup()` helper in `commands/state.py`: extracts the symlink guard + mkdir + secure write + rotation logic that was previously duplicated across `state rm`, `state mv`, and `state push`.
- Per-operation backup quotas: `_rotate_backups()` now takes an `op_prefix` argument so each of `state_rm_*`, `state_mv_*`, `state_push_*` has its own independent cap. A chatty `state_rm` rotation can no longer purge the `state_push` backup you wanted to keep.
- `tests/test_sdk_compat.py`: regression coverage for the source-position monkey-patch. Asserts that `_get_stack_trace`/`_get_source_position` still exist in the installed Pulumi SDK, that the patched return values are empty, and that the fail-open path logs a WARNING when the targets are missing.
- PEP 561 `py.typed` marker so downstream packages can use tlumi's type hints.
- `__version__` derived from `importlib.metadata` with a `PackageNotFoundError` fallback to `"0.0.0+source"` for raw-source imports.
- New `examples/random/` (no-credentials demo using `pulumi-random`) and `examples/aws-s3/` (minimal S3 bucket).
- Integration smoke tests for the published examples.
- Architectural Decision Records section in `DESIGN.md`.
- `CONTRIBUTING.md`, `SECURITY.md`, this `CHANGELOG.md`.

### Changed
- Minimum Pulumi SDK is now `>=3.242.0` (was `>=3.224.0`). 3.242 matches the lockfile and the version the test suite is pinned against. No upstream fix has shipped for source-position suppression, so `tlumi.sdk_compat` continues to patch `_get_stack_trace` and `_get_source_position` in `pulumi.runtime.resource`.
- `ProjectConfig` is now `@dataclass(frozen=True)`. `merge_variables()` returns a new `ProjectConfig` instead of mutating the input. Every merge re-runs `__post_init__` validation on the result.
- `_WorkspaceNoticeHandler` is now attached to the root `tlumi` logger. Previously the handler was wired to `tlumi.workspace`, `tlumi.engine`, and `tlumi.sdk_compat` explicitly, which silently dropped warnings from `tlumi.commands.deps` (backup rotation failures, package-name normalization fallbacks) and other command-module loggers.
- Pulumi engine diagnostics and `EngineError.full_output` now flow through `redact_text()` before they reach the user, both in the human Rich path and in the JSON envelope. Provider errors that echo credentials in stderr no longer surface them in console output or `--json` results.
- `tlumi state mv` now rejects same-name moves with a clear "source and destination are the same" error. Previously the collision loop matched the source resource against itself and produced a misleading "URN already exists, choose a different name" hint.
- `tlumi state rm/mv/push` recovery hints now embed the actual backup path instead of the literal placeholder `<file>`. Restoring is now copy-pasteable.
- `tlumi init` in non-interactive mode now scaffolds `allow_unencrypted: false` (was `true`). Automation pipelines no longer silently set up plaintext-secrets projects.
- `tlumi output` now uses `runtime=False`, joining the recovery command cohort. Reading already-applied outputs no longer requires a healthy `infra.py` or venv.
- `_rotate_backups()` deletion failures now log at `WARNING` (was `DEBUG`). A stuck rotation is visible at default verbosity rather than silently filling the backup directory.
- `state mv` on a parent with children rewrites each child's `parent` reference and warns, listing the children; a child whose code-side name is derived from the parent name will still be replaced on the next apply.
- `_ensure_pulumi_cli` shared-cache install failure now logs at `WARNING` (was `DEBUG`). A silent fallback was hiding stale `~/.tlumi/cache/` perms behind unrelated downstream errors.
- `SetupResult.__iter__` removed. Tuple-unpacking silently dropped `resolved_replace`; callers must use attribute access. Production callers already did.
- `PropertyChange.__post_init__` now rejects `kind="update"` with both `old_value` and `new_value` as `None`.
- Symlink guards on `.tlumi/backups/` in `state rm`, `state mv`, and `state push`.
- README restructured around the credential-free Quick Start; comparison table and state-locking caveat hoisted up.
- `DESIGN.md` trimmed to architecture plus ADRs; user-manual content lives in README.
- The shared `uv` binary download is now atomic: it extracts to a unique temp path, sets the executable bit, then `os.replace()`s it into the cache, removing the temp on any failure. A concurrent invocation can no longer pick up a half-written binary, and a failed extraction or chmod no longer poisons the cache. `ensure_uv()` also re-downloads a cached binary that is empty or non-executable, and the shared `~/.tlumi/cache/` hierarchy is chmod-tightened to `0o700` at every level (not just the leaf).
- `tlumi init` now tightens `.tlumi/` permissions via an `O_NOFOLLOW | O_DIRECTORY` fd (`safe_chmod_dir`) instead of `Path.chmod`, which follows symlinks; this closes the TOCTOU window between directory creation and the chmod.
- The source-position SDK patch now probes `source_pb2.StackTrace` at patch time. A future SDK that drops or renames it now fails open (the patch is skipped with a WARNING) instead of raising `AttributeError` on every resource registration.

### Fixed
- `_get_nested()` in `diffs.py` now correctly tokenizes Pulumi `detailed_diff` paths that contain quoted-string keys (`tags["k8s.io/role"]`, `nested["foo.bar"]["baz"]`). Previously the naive `replace("[", ".")` + split mangled such paths and the diff lookup silently fell back to "missing".
- README's documented Quick Start (`pip install tlumi`) was broken because the package is not on PyPI. README now installs from source via `uv tool install .` until the package is published to PyPI.
- README claimed `--replace` works on `destroy` and `refresh`. Pulumi's Automation API only accepts `replace=` on `up` and `preview`, so the flag was always rejected at the CLI level. README now states the actual surface.
- `engine.py` module docstring claimed `import` uses `on_event` callbacks. `import_resources` doesn't accept `on_event`; docstring corrected.
- `_WorkspaceNoticeHandler` class docstring listed two loggers; code attached to three. Docstring rewritten to describe the root-logger setup now used.
- `CONTRIBUTING.md` claimed "CI gates on both" ruff lint and ruff format. No CI exists. CONTRIBUTING now lists lint, format, bandit, and tests as locally-required gates without claiming CI enforcement.
- `examples/README.md`, `examples/random/README.md`, and `examples/aws-s3/README.md` no longer include the redundant `tlumi deps install` step after `tlumi init`. `init` already installs requirements for cloned projects.
- README incorrectly claimed secrets are stored in plaintext without `TLUMI_SECRETS_PASSPHRASE`. The actual default is to refuse and raise `ConfigError`; plaintext requires explicit opt-in via `secrets.allow_unencrypted: true`.
- `DESIGN.md` showed the per-project `pulumi_home` as the canonical CLI location; the shared cache `~/.tlumi/cache/pulumi/<sdk_version>/` is the primary path, with per-project as fallback.
- Backend URL masking previously triggered an unnecessary `parse_qsl` round-trip on the query string when only the password had been masked, with a latent risk of mangling percent-encoded values.
- `state mv`/`state rm`/`state push` now handle Pulumi's persisted `deletedWith` resource edge: `mv` rewrites it, `rm` strips it (and warns about `deletedWith` dependents before removal), and `push` validates it for referential integrity. Renaming or removing a resource wired with the `deleted_with` option (common on Azure) no longer fails the snapshot integrity check. (`replaceWith` is a registration-only option, not persisted in state, so it is intentionally not handled.)
- `resolve_resource()` now resolves a resource that has a pending-deletion duplicate (the live copy plus the condemned `delete: true` copy left by a create-before-delete replacement) by preferring the live entry, instead of raising an unsatisfiable "ambiguous" error whose hints could not disambiguate byte-identical entries.
- `extract_property_diffs()` no longer raises `TypeError` (which the `on_preview` wrapper swallowed, silently dropping every property diff and emitting a confusing callback-error warning) when one side's `inputs` is `None` rather than `{}`.
- Property diffs no longer emit a phantom `+ key` entry for a `meta.diffs` key absent from both old and new inputs, and no longer silently drop an explicitly empty nested container (`{}`/`[]`) or the index of an empty-dict list element.
- A YAML null variable value (`key:`, `key: null`, `key: ~`) in `tlumi.yaml` or a `--var-file` is now rejected with a clear error instead of being stored as the literal string `"None"`.
- The project-name regex is anchored with `\Z` instead of `$`, so a trailing newline (e.g. from a YAML block scalar) no longer slips past the `[a-zA-Z0-9_-]` invariant.
- `apply --plan` now also rejects `TLUMI_VAR_*` environment variables, closing an equal-priority channel that could silently override the saved plan's captured configuration.
- `deps add` now rejects every line boundary `str.splitlines()` honours (VT, FF, FS, GS, RS, NEL, U+2028, U+2029), not just `\n`/`\r`, plus an explicit NUL, so a crafted package argument cannot forge a second `requirements.txt` directive.
- `_mask_backend_url()` keeps IPv6 address brackets when masking credentials, so an IPv6 backend renders as `s3://user:***@[2001:db8::1]:9000/...` instead of an ambiguous host.
- The in-progress spinner only animates the in-progress marker; a literal `...` inside a provider diagnostic line ("Waiting for operation...") is no longer rewritten by the dots animation.
- DESIGN.md and `.claude/CLAUDE.md` drift corrected: the secret-wrapper sentinel description (no `__pulumi_secret` key exists), the attribution of boolean-field validation to `load_config()` (not `ProjectConfig.__post_init__`), the `_SENSITIVE_QUERY_SUBSTRINGS` list (6 entries including `passwd`), the default-backend lock-file path (`.tlumi/state/.pulumi/locks/`), and a stale "patched to handle camelCase" comment in `diffs.py`.

### Security
- `redact_text()` now scrubs credentials whose key is quoted, so JSON error bodies returned by cloud providers (`"password": "..."`, `"client_secret": "..."`, `"sasToken": "..."`, `"accessToken": "..."`) are redacted instead of passing through verbatim. Quoted multi-word values are consumed to the closing quote so a secret containing a space no longer leaks its tail, and colon-form `SecretAccessKey: ...` is now covered. This guard feeds the console output, the `--json` diagnostics envelope, and the live log simultaneously.
- `apply --json` now routes non-secret outputs through `_sanitize_value()`, masking secret sentinels nested inside complex (dict/list) outputs by default. Previously the default `apply --json` path serialized them in cleartext, even though the sibling `output --json` path and the human-readable display already masked them.

### Testing
- Behavioural regression coverage so the security guards cannot silently regress: dropping any of the three `redact_text()` calls in `engine.py`, or the `apply --json` sanitization, now fails the suite. Added coverage for JSON-key/quoted-value redaction, the `deletedWith` state edge across `mv`/`rm`/`push`, pending-deletion duplicate-URN resolution, Strategy-1/Strategy-2 diff edge cases, null-variable rejection, the project-name `\Z` anchor, uv cache atomicity/poison-recovery/permissions, the `safe_chmod_dir` symlink refusal, the sdk_compat `StackTrace` fail-open probe, the IPv6 backend-URL mask, and the `state push` provider `::id`-suffix strip. The test suite grew substantially across the pre-launch hardening passes.

## [0.1.0] - Unreleased

Initial public release. CLI surface: `init`, `plan`, `apply`, `destroy`, `refresh`, `output`, `show`, `validate`, `fmt`, `import`, `clean`, `version`, `state list/show/rm/mv/pull/push/unlock`, `deps add/install/list`.
