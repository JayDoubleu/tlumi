# Terralumi (tlumi)

Python CLI tool providing Terraform-like workflow powered by Pulumi Automation API.

## Architecture

- See `DESIGN.md` for full architecture and decisions
- Pure Python CLI: Typer + Rich, distributed via pipx/uv
- Pulumi Automation API as engine, completely hidden from user UX
- Local state by default in `.tlumi/state/`, no Pulumi Cloud, no `~/.pulumi` pollution

## Project Layout

```
src/tlumi/
├── __init__.py       # __version__ via importlib.metadata, fallback "0.0.0+source"
├── __main__.py       # python -m tlumi entry point
├── cli.py            # Typer app, command registration, --verbose/--var/--var-file/--target/--replace on commands
├── commands/         # One file per command
│   ├── _common.py    # Shared setup_command() helper for config/stack/targets/replace
│   ├── init.py       # tlumi init
│   ├── clean.py      # tlumi clean (remove .tlumi/ directory)
│   ├── plan.py       # tlumi plan
│   ├── apply.py      # tlumi apply (with Live rolling log)
│   ├── destroy.py    # tlumi destroy (reads state for preview)
│   ├── cancel.py     # tlumi state unlock (release stale state locks)
│   ├── output.py     # tlumi output
│   ├── deps.py       # tlumi deps add/install/list
│   ├── validate.py   # tlumi validate (config + syntax check, no Pulumi needed)
│   ├── fmt.py        # tlumi fmt (format Python files via ruff)
│   ├── refresh.py    # tlumi refresh (reconcile state with cloud)
│   ├── state.py      # tlumi state list/show/rm/mv/pull/push (unlock lives in cancel.py)
│   ├── show.py       # tlumi show (full state dump)
│   └── import_cmd.py # tlumi import (import existing cloud resources)
├── config.py         # tlumi.yaml parsing (ProjectConfig dataclass), merge_variables()
├── workspace.py      # Pulumi Automation API wrapper
├── engine.py         # Pulumi on_event handler with Rich Live display
├── diffs.py          # Property diff extraction and expansion
├── sanitize.py       # Pulumi sentinel value detection and sanitization
├── sdk_compat.py     # SDK monkey-patch: source position suppression
├── resolve.py        # Unified resource resolution (name_from_urn, resolve_resource, resolve_targets)
├── display.py        # Rich output formatting
├── errors.py         # TlumiError hierarchy (includes EngineError, Diagnostic NamedTuple)
├── redact.py         # Free-form credential redaction for stderr/diagnostic strings
├── _safeio.py        # Symlink-safe file/dir primitives (O_NOFOLLOW writes, lstat-checked mkdir)
├── uv.py             # uv binary management (ensure_uv, create_venv, run_install, run_list)
├── py.typed          # PEP 561 marker so downstream type-checkers pick up tlumi's types
└── templates/        # Jinja2 templates for tlumi init

examples/
├── random/                 # No-credentials hello world (pulumi-random)
├── aws-s3/                 # Minimal S3 bucket with versioning
├── azure-container-apps/   # Docker build + ACR + Container App (single infra.py)
└── azure-modular/          # Reusable ComponentResource components (Python modules)
    ├── components/         # Local Python package --replaces Terraform modules
    │   ├── monitoring.py   # MonitoringStack (Log Analytics + App Insights)
    │   └── storage.py      # StorageBucket (Storage Account + N containers)
    └── infra.py            # Uses components via Python imports
```

## Conventions

- Python 3.10+, use modern type hints (X | None, not Optional[X])
- Typer for CLI with type-annotated commands
- Rich for all terminal output (console = Console())
- Dataclasses for config models, not Pydantic
- Errors: raise TlumiError subclasses, caught at CLI boundary in cli.py
- Engine errors: use `catch_engine_errors(handler, message)` context manager (engine.py) instead of raw try/except CommandError
- Engine events: use Pulumi `on_event` callbacks (not stdout parsing) for resource display and error collection
- Templates: Jinja2 `.j2` files in templates/ directory
- Reusable components use Pulumi's `ComponentResource` class (Python's answer to Terraform modules) --local Python packages in project dir, imported in infra.py
- `sys.path.append(project_dir)` in workspace.py enables local imports from the project directory
- No stacks/workspaces exposed to users --single "default" stack internally
- Engine events use Rich Live display with animated progress (creating.../deleting...)
- Property-level diffs shown in plan/apply preview for CREATE, UPDATE, REPLACE, DELETE ops (CREATE shows all inputs as adds, DELETE shows all inputs as deletes --matches Terraform); dunder keys (`__internal` etc.) are filtered out
- `PropertyChange` dataclass in diffs.py: `path`, `kind: Literal["add", "update", "delete"]`, `old_value`, `new_value`, `forces_replacement`; factory classmethods `.add(path, value)`, `.delete(path, value)`, `.update(path, old, new)` enforce kind/value pairing; `__post_init__` rejects invalid combos (add+old_value, delete+new_value, update with both old_value and new_value as None)
- `extract_property_diffs(meta)` in diffs.py: five paths --early return for non-diff ops; CREATE expands all new inputs as add entries; DELETE expands all old inputs as delete entries; UPDATE/REPLACE tries `meta.detailed_diff` if SDK provides it, falls back to `meta.old.inputs` vs `meta.new.inputs` comparison using `meta.diffs` as the changed-key list with one level of dict sub-key diffing (only when both sides are dicts; mixed dict/non-dict types fall through to the else branch)
- `_sanitize_value()` in sanitize.py: recursively walks dicts/lists (depth-limited to `_MAX_DEPTH=50`), replaces only sentinel nodes --direct secret wrappers → `(sensitive)`, unknown sentinel strings → `(known after apply)`, leaf strings containing `_PULUMI_SECRET_SIG` → `(sensitive)` (defense-in-depth for debug logs / embedded JSON where the wrapper-dict form may be lost), `[secret]` ciphertext → `(sensitive)`; non-sentinel structure preserved; returns `(sensitive)` at max depth as safe default (never leaks deeply nested secrets)
- `_contains_secret_sentinel(value)` in sanitize.py: sibling walk of `_sanitize_value()` (same dict/list/str inspection, same `_MAX_DEPTH` cap) returning `True` if a Pulumi secret sentinel appears anywhere (wrapper dict, embedded-JSON sig string, or bare `[secret]` ciphertext); returns `True` at the depth limit as the conservative default. Used by diffs.py `extract_property_diffs`/`_expand_complex_diff` to detect the case where a rotated secret is scrubbed to a BYTE-IDENTICAL wrapper on both old and new sides: normal equality would read that as an unchanged no-op, so when a sentinel is present the diff logic skips the dunder-strip/deep-equal early return and surfaces the secret masked (the engine's diff signal is authoritative)
- `_format_json()` in diffs.py: serializes sanitized Python value to JSON; compact (`separators=(", ", ": ")`) if ≤60 chars, otherwise `indent=2`; post-processes to unquote `(sensitive)` and `(known after apply)` markers
- `_format_leaf()` in diffs.py: formats a leaf value for flattened output --quoted strings (truncated at 60 chars), lowercase bools, `null`, marker passthrough
- `_flatten_value(value, prefix="")` in diffs.py: flattens sanitized dicts/lists into `(relative_path, formatted_leaf)` tuples --`{"a": {"b": 1}}` → `[("a.b", "1")]`, lists use bracket notation `[0].name`
- `_format_diff_value()` in diffs.py: sanitizes via `_sanitize_value()` first, then formats (quoted strings, lowercase bools, truncate at 60 chars); dicts/lists → compact JSON (≤60 chars inline) → flatten to dotted paths if too long
- `_expand_value(path, value, kind, forces_replacement)` in diffs.py: expands add/delete of dict/list values into per-leaf `PropertyChange` entries; scalars fall back to single `PropertyChange` with `_format_diff_value()`
- `_expand_complex_diff(path, old_val, new_val, forces_replacement)` in diffs.py: when both sides are dict/list, sanitize → flatten both → diff flattened pairs → one `PropertyChange` per changed leaf with inline values; unchanged sub-fields skipped; falls back to single `PropertyChange` when one side is scalar or both compact (≤60 chars); returns [] when old/new differ only inside dunder keys (compared on RAW dunder-stripped values pre-sanitize, so genuinely changed secrets that both mask to `(sensitive)` still surface)
- `_get_nested()` in diffs.py: traverses dotted/bracketed paths into nested dicts/lists for detailed_diff value lookup
- `format_diff_tree(header, changes, guide_style)` in display.py: returns `Padding(Tree(...), (0,0,0,2))` --Rich Tree with resource header as root, property changes as children with tree connectors (`├──`, `└──`)
- `_build_diff_text(change, max_path)` in display.py: dispatches to inline or expanded rendering based on whether values contain newlines
- `_build_diff_text_inline(change, max_path)`: single-line format --`~ path  "old" → "new"` with strikethrough old, bold green new, dimmed paths
- `_build_diff_text_expanded(change)`: multi-line format --path on its own line, old/new values indented with `_DIFF_INDENT` (4 spaces), `→` arrow on its own line; no `max_path` alignment
- `_DIFF_INDENT = "    "` in display.py: 4-space indent for expanded multi-line diff values
- Tree guide colors match operation type via `_DIFF_GUIDE_STYLE` map (yellow for update, cyan for replace)
- SDK source position suppression in sdk_compat.py: patches `_get_stack_trace` and `_get_source_position` in `pulumi.runtime.resource` to return empty values, preventing local filesystem paths (sourcePosition, stackTrace) from being persisted in state; always applied (intentional behavior change, not a bug fix); guarded by `try/except (ImportError, AttributeError)` for SDK layout changes
- `handler.property_diffs: dict[str, list[PropertyChange]] ` stores per-URN diffs; used by plan.py for `"resource_diffs"` JSON key
- Property diffs are extracted only in `on_preview`, never in live `on_update` --too noisy during provisioning. They are still collected when `EventHandler(quiet=True)` is set for JSON mode (the dict is populated; only the Rich rendering is suppressed)
- Replacement dedup: `on_preview()` skips `CREATE_REPLACEMENT` and `DELETE_REPLACED` events --only `REPLACE` is shown (Pulumi emits 3 events per replacement; we show 1, matching Terraform)
- Preview spacing: `on_preview()` emits a blank line only after resources that had property diff trees (`_preview_last_had_diffs`). In practice CREATE/DELETE always carry diff trees because `extract_property_diffs()` expands their inputs as adds/deletes, so the "no gap" branch fires only for the edge case of resources with no inputs at all
- Diagnostic log lines attributed to a resource via `_resource_logs: dict[str, deque]` keyed by URN. Attribution is best-effort: lines with no URN fall back to `_last_started_urn`, so during parallel updates a log line can land under the wrong resource (whichever one started most recently). Inactive-resource lines may also be dropped from the live window once `_active_resources` no longer holds them; the structured `errors`/`callback_errors` collection still surfaces them in the final summary
- Ctrl+C handled gracefully (suppresses Pulumi thread tracebacks); JSON mode emits `{"error": "Interrupted"}` instead of Rich markup
- `_detect_hint()` in cli.py matches known error patterns to actionable hints (locked state, missing modules, auth errors, connection errors); uses plain-text quotes (`'cmd'`) not Rich markup --portable across Rich and JSON output; also called from `_emit_json_error()` for EngineError when no hint is already set (ensures JSON mode includes hints)
- `extract_changes()` in display.py extracts (create, update, replace, delete) 4-tuple from change-count mappings; the `import` count is read separately via `change_counts().get("import", 0)` (deliberately not widened to a 5-tuple). `print_plan_summary`/`print_apply_summary` take `import_count` ("N to import"/"N imported") and `print_plan_summary` takes `outputs_changed` (renders "Plan: outputs will change." when no resource parts); `print_apply_summary` prints "No resources changed." instead of a dangling empty "Resources:" header when all counts are zero; `format_engine_result` includes an `import` key in the changes dict
- `setup_command()` in `commands/_common.py`: consolidates find_project_dir + load_config + merge_variables + banner + animated_status + get_stack + resolve_targets + print_targets; returns `SetupResult`; used by plan.py, apply.py, refresh.py (destroy.py uses direct imports for state-based display)
- `ProjectConfig.__post_init__` validates `name` against regex, rejects `..` in `entry` path, rejects absolute `entry` paths, and requires `project_dir` to be absolute
- Variable keys in tlumi.yaml reject colons (reserved for Pulumi provider config namespaces); also enforced in `ProjectConfig.__post_init__` for programmatic construction
- `provider_config` block in tlumi.yaml (`ProjectConfig.provider_config: dict[str,str]`) sets namespaced Pulumi provider config verbatim (e.g. `azure-native:location: westeurope`), the complement to `variables` (which go under the project namespace). Keys MUST be `namespace:key` (validated by `_PROVIDER_KEY_RE` in config.py); a bare key, or a namespace equal to the project name, is rejected with a hint pointing at `variables:`. Values reuse `_coerce_variable_value` (same lossy-YAML rejection). Needed by nearly every real Azure accelerator (an `azure-native` ResourceGroup with no explicit `location=` reads `azure-native:location`); before this, the only workaround was hand-editing `.tlumi/Pulumi.default.yaml`. `merge_variables()` carries `provider_config` through unchanged (not overridable via `--var`). In `get_stack()`, provider keys are set via `stack.set_config(full_key, ...)` after the bare variables loop, and recorded in the `managed_config_keys.json` sidecar under a new `"provider_keys"` field (alongside `"keys"`); stale provider keys tlumi wrote are removed like stale variables, while a sidecar predating the feature (no `provider_keys` field) means "nothing to clean" (not a skip). Sidecar reads go through `_read_sidecar()` + `_sidecar_key_set()`; `_read_managed_keys()` / `_read_managed_provider_keys()` wrap them
- `ProjectConfig`, `BackendConfig`, and `SecretsConfig` are all `@dataclass(frozen=True, slots=True)` (never mutated after construction; `slots=True` blocks accidental attribute addition too). Variable merging produces a new ProjectConfig from `merge_variables()` rather than mutating in place
- `_OpDisplay` NamedTuple in engine.py: `symbol`, `in_progress`, `past_tense`, `future` fields for `_OP_DISPLAY` map entries
- `on_preview()` and `on_update()` wrapped in try/except with `_log.warning()` and `_callback_errors` counter increment; `handler.callback_errors` property exposes the count; commands check after operation and emit `print_warning()` if > 0
- Sentinel constants in sanitize.py use `typing.Final` annotations; `_DIFF_VALUE_MAX_LEN` in diffs.py also `Final`
- Entry path traversal validation in config.py prevents `entry: "../../../etc/passwd"`
- `--var KEY=VALUE` and `--var-file PATH` on plan/apply/destroy/validate/refresh/import commands
- `merge_variables(config, var, var_file) -> ProjectConfig` in config.py: --var > --var-file > TLUMI_VAR_* > tlumi.yaml priority; returns a NEW ProjectConfig (frozen dataclass) instead of mutating in place, so every merge re-runs `__post_init__` validation
- `TLUMI_VAR_*` environment variables: lowercased key after prefix, e.g. `TLUMI_VAR_REGION` → `region`
- Variable values (tlumi.yaml, var files, --var) must be scalars; lists/dicts are rejected. Accepted unquoted forms are str, plain decimal int, and canonical `true`/`false` bool. Lossy unquoted forms are rejected with a quote hint (the `_StrictIntLoader` retags each to a raw-text marker class in config.py): floats (YAML rewrites `1.10` to `1.1`), non-decimal ints (`0777`->511, `0x1A`->26, `1:30`->90, `+7`->7), YAML 1.1 boolean keywords (`yes`/`no`/`on`/`off`, the "Norway problem" that turns an ISO code `NO` into `false`), and timestamps/dates (`2026-07-02T10:00:00Z`, reformatted on load). A quoted value arrives as a string. Embedded NUL bytes in a key or value are rejected (they cannot be passed to the Pulumi CLI), and a variable key YAML coerced from a keyword/number must be quoted
- YAML booleans are preserved as lowercase ("true"/"false"), not Python repr --applies in both tlumi.yaml and var files
- Project variables (`--var`, `--var-file`, `TLUMI_VAR_*`, `tlumi.yaml`) are written as **plaintext** Pulumi config and are NOT encrypted by `TLUMI_SECRETS_PASSPHRASE`; they are also visible in the `pulumi config set` argv. Real secrets belong in `infra.py` as Pulumi secrets (`config.require_secret(...)` / `pulumi.Output.secret(...)`), which the passphrase encrypts in state. `--var` values additionally appear in shell history, so prefer `--var-file`/`TLUMI_VAR_*` among the (still-plaintext) variable channels; `merge_variables()` warns on sensitive key names (secret, password, token, etc.) unless `quiet=True`
- `--var-file` accepts paths anywhere on the filesystem (no project-dir restriction) --supports `/dev/stdin`, `/tmp/`, and cross-project var files
- Non-engine errors (missing output, bad syntax, state failures) use appropriate TlumiError subclass, not raw prints
- `validate` command catches `SyntaxError` from `compile()` and wraps in `ConfigError`
- Project names validated: must start with letter, contain only `[a-zA-Z0-9_-]`
- `state rm`, `state mv`, and `state push` create backups in `.tlumi/backups/` --backups use full restorable format `{"version": N, "deployment": {...}}`; the deployment is sourced from `export_stack_no_secrets(stack)` (a plain `stack export`, NOT the SDK's hardcoded `--show-secrets` export), so secrets are preserved as **ciphertext** (their on-disk encrypted form) and never decrypted to plaintext on disk, even when `TLUMI_SECRETS_PASSPHRASE` is set; ciphertext restores identically because the salt lives in `deployment.secrets_providers` and `state push` requires the same passphrase; backup write failures wrapped in `WorkspaceError` with "State was NOT modified" hint (backup happens before mutation); import failures reference the backup path in their hint; `_rotate_backups(backup_dir, op_prefix=...)` keeps last `_MAX_BACKUPS` (10) files **per op_prefix** (`state_rm_`, `state_mv_`, `state_push_` each get their own quota), removing oldest by mtime after each write (best-effort, deletion failures logged but not raised). Per-op quotas prevent a chatty operation (e.g. a scripted retry loop on `state rm`) from rotating out the `state_push` backup the user actually wanted to revert to
- `state rm` cleans all four reference types: `parent`, `dependencies`, `provider`, and `propertyDependencies` (removes URN from lists, deletes empty entries); provider refs matched with `_provider_matches(provider, urn)` which checks `provider == urn or provider.startswith(urn + "::")` to handle `URN::id` suffix without false positives on similar-prefix URNs
- `state mv` rewrites all four reference types (same as `state rm`) by replacing old URN with new URN (not deleting); constructs new URN positionally (split maxsplit=3, name field replaced, so names containing `::` survive); destination `type::name` splits on the FIRST `::` (consistent with resolve_resource), so destination names containing `::` parse correctly; validates destination type when `type::name` format is used; checks for URN collision; provider refs with `::id` suffix preserved by rewriting the URN prefix portion only
- `state mv` allows renaming a target that has children and prints a `print_warning` listing them. Child URNs embed only the parent TYPE chain (`...parent_type$child_type::child_name`), never the parent's NAME, so the rewrite loop repointing each child's `parent` reference to the new URN is sufficient; the children's own URNs are unaffected. The warning notes that a child whose code-side name is derived from the parent name (e.g. `f"{parent}-child"`) will still be replaced on the next apply. (Earlier versions refused outright on a false "child URNs embed the parent name" premise.)
- `tlumi init` prompts interactively (Y/N) for whether to enable secret encryption; **no passphrase is captured** at init time --the user is reminded to set `TLUMI_SECRETS_PASSPHRASE` before running plan/apply. Answering "N" sets `secrets.allow_unencrypted: true` in `tlumi.yaml`. Non-interactive sessions default to secure (`allow_unencrypted: false`); a piped or scripted init that wants unencrypted state must edit `tlumi.yaml` afterward
- `get_stack()` removes stale project config keys before setting current variables, driven by a sidecar `.tlumi/cache/managed_config_keys.json` recording the bare keys tlumi itself wrote (written via `_safeio.safe_write_text`, 0o600): only keys present in the sidecar and absent from `config.variables` are removed. Missing/corrupt/symlinked sidecar (including non-UTF-8 bytes and pathologically nested JSON) means cleanup is SKIPPED entirely (debug log): failing to clean stale vars is safe, deleting provider config is not (a project named `aws` would otherwise turn the real provider key `aws:region` into bare key `region` and delete it). Best-effort via `CommandError` catch
- `get_stack()` catches `auto.errors.CommandError` (not bare `Exception`) on `create_or_select_stack` and config cleanup; programming errors propagate with full tracebacks
- workspace.py uses `_log = logging.getLogger(__name__)` for all notices (info/warning) and debug logging; does NOT import from display.py; cli.py registers `_WorkspaceNoticeHandler` on the root `tlumi` logger so every submodule (workspace/engine/sdk_compat/commands.*) propagates through it without per-module wiring; handler checks `_json_active` module flag to suppress output in JSON mode; `--verbose` lowers the handler from INFO to DEBUG so internal diagnostics surface
- `--json` flag on plan/apply/destroy/refresh/validate/output/show/state list/state show/state push for machine-readable output
- `_json_option` type alias in cli.py, threaded through `_do_*` wrappers as `json_output: bool`
- `RunContext` `@dataclass(frozen=True, slots=True)` in cli.py holds `verbose` and `json_output` flags; stored in `typer.Context` via `ctx.ensure_object(RunContext)`, replaced via `dataclasses.replace` on update so the documented "read-only after setup" invariant is enforced by the type system. The `_json_active` flag in cli.py is still a module-level global (it's read by `_WorkspaceNoticeHandler` from a separate logging context that doesn't see `RunContext`); the RunContext-vs-globals split is deliberate but partial
- `_setup(ctx, verbose, json)` in cli.py: consolidates RunContext extraction and flag assignment; used inline as `_run(lambda: ..., _setup(ctx, verbose, json))`
- JSON mode suppresses banners, Rich formatting, confirmation prompts, Live display (`start_live(quiet=True)` returns `nullcontext()`), and animated status (`animated_status(..., quiet=json_output)` yields without output)
- `output NAME --raw` value formatting (output.py): dict/list, bool, and None are emitted as compact JSON (`json.dumps(..., separators=(",",":"))`) so bool/None render as lowercase `true`/`false`/`null` (matching every other tlumi channel) rather than Python's `True`/`False`/`None` repr; plain strings stay bare `str()` so a shell-captured string does not gain JSON quotes. Condition is `isinstance(val, (dict, list, bool)) or val is None`
- `print_json(data)` in display.py: prints pre-serialized JSON with `soft_wrap=True, highlight=False, markup=False` --prevents Rich from inserting newlines inside JSON strings at terminal width and prevents markup interpretation of JSON values; use instead of `console.print(json.dumps(...))`
- `EventHandler(quiet=True)` suppresses `on_preview()` console output and `_print()` calls --still collects `property_diffs` and `errors` for JSON output; pass `quiet=json_output` when constructing handlers
- JSON mode errors: `_emit_json_error()` in cli.py emits `{"error": "...", "hint": "...", "diagnostics": [...]}` instead of Rich-formatted output
- `get_stack()` accepts `quiet: bool = False` --pass `quiet=json_output` to suppress Rich warnings (e.g. passphrase warning) in JSON mode
- `deps add` writes requirements.txt first, then uv installs --on install failure entries are already recorded for retry
- `deps add` uses `packaging.requirements.Requirement` for proper package name normalization
- `_normalize_name()` in deps.py normalizes hyphens, dots, underscores and handles extras/markers; fallback uses regex `[~!=<>\[;@]` split for invalid Requirement specs; **rejects empty normalized names with `WorkspaceError`** rather than returning empty (a returned `""` would collide unrelated weird specs as the same key in `existing_packages` and silently drop legitimate adds)
- `print_banner()` outputs `\n text` (leading newline only) --animated status and content follow directly beneath
- `animated_status(message, quiet=False)` in display.py: context manager using Rich Live with animated dots (matches engine's `_AnimatedLog` pattern); message must end with `...`
- `install_live(header)` in display.py: context manager yielding `add_line` callback; shows animated header with rolling window of last 6 install output lines (dimmed); used by init, deps add, deps install for live dependency installation feedback
- `_InstallLog` in display.py: renderable combining animated dots header + rolling log lines; `_INSTALL_LOG_LINES = 6` controls window size
- uv binary management in uv.py: `ensure_uv()` → PATH first, then shared cache `~/.tlumi/cache/uv/<version>/uv`, then auto-download from GitHub releases (SHA-256 verified)
- `_verify_checksum()` in uv.py: downloads `.sha256` file from GitHub release (timeout=10s), compares against computed hash; on mismatch or checksum fetch failure, deletes archive and raises `WorkspaceError` --hard-fails because if the archive downloaded successfully, the network is available
- `_download_uv()` in uv.py: uses `urlopen(timeout=60)` + `shutil.copyfileobj` (not `urlretrieve`, which has no timeout support); cleans up partial archive on failure
- tarfile extraction in uv.py uses a manual `tar.extractfile()` + `shutil.copyfileobj()` write to a known-safe path (`dest_dir / "uv"`), not `tar.extract()`. The `filter="data"` argument exists only from Python 3.12+; on 3.10/3.11 an `extract()` fallback would follow symlinks and absolute paths in the archive. Checksum verification before extract is the primary control; the manual write is defense-in-depth that does not depend on filter availability
- `create_venv(uv, venv_path)` in uv.py: runs `uv venv <path>` (no pip seeded in venv)
- `run_install(uv, venv_path, args, add_line=None)` in uv.py: runs `uv pip install --python <venv>/bin/python ...`; streaming mode when `add_line` callable provided
- `run_list(uv, venv_path)` in uv.py: runs `uv pip list --python ... --format=columns`
- Install commands use `Popen` with `stdout=PIPE, stderr=STDOUT` for streaming (not `capture_output=True`)
- `console.print()` strings must never have trailing `\n` --use explicit `console.print()` (no args) for blank lines
- Use `print_success()`/`print_warning()`/`print_error()` helpers consistently, not inline Rich markup like `[success]...[/success]`
- `tlumi state unlock` (was `tlumi cancel`) --matches Terraform's `force-unlock` naming convention
- Engine diagnostic lines attributed to source resource via `_last_started_urn` fallback
- `--target` flag (`-t`) on plan/apply/destroy/refresh --targets specific resources by name, type::name, or full URN
- `_target_option` type alias in cli.py, threaded through `_do_*` wrappers as `target: list[str] | None`
- `resolve_targets(stack, identifiers, resources=)` in resolve.py: resolves identifiers to full URNs; accepts pre-loaded resources to avoid double state reads; raises `WorkspaceError` if resolved resource has empty URN (corrupted state guard)
- `resolve_resource()` in resolve.py: name-only → unique match or ambiguity error; type::name → matches against both URN-derived type and canonical `type` field from state (handles `$`-qualified child resource URNs), raises ambiguity error on multiple matches; full URNs pass through
- `print_targets()` in display.py: displays "Targeting N resource(s):" with display_from_urn formatting
- destroy.py reads state once for both display and target resolution (passes `resources=raw_resources`)
- Resolved targets passed to Pulumi API's `target` parameter on `preview()`, `up()`, `destroy()`, `refresh()`
- JSON output includes `"targets"` key when `--target` is active
- `--replace` flag (`-r`) on plan/apply --forces replacement of specific resources; resolution reuses `resolve_targets()`
- `_replace_option` type alias in cli.py, threaded through `_do_*` wrappers as `replace: list[str] | None`
- `print_replace_targets()` in display.py: displays "Replacing N resource(s):" with display_from_urn formatting
- Resolved replace targets passed to Pulumi API's `replace` parameter on `preview()` and `up()`
- JSON output includes `"replace"` key (list of URNs) when `--replace` is active
- `--replace` + `--target` is valid (both passed to SDK; Pulumi handles the intersection)
- `--destroy` flag on plan command --calls `stack.preview_destroy()` instead of `stack.preview()`; mutual exclusion with `--replace` and `--out`
- `--destroy` + `--target` is valid (SDK supports targeted destroy preview)
- `--out` (`-o`) flag on plan --saves plan file via Pulumi SDK's `plan=` parameter on `preview()`; JSON output includes `"plan_file"` key
- `--plan` flag on apply --applies a saved plan file via Pulumi SDK's `plan=` parameter on `up()`; skips interactive preview; mutual exclusion with `--var`, `--var-file`, `--target`, `--replace` (plan captures all configuration)
- `apply --plan` skips stack-config reconciliation: `get_stack()` takes `reconcile_config: bool = True` and apply.py passes `reconcile_config=not plan_file` (threaded through `setup_command()`). The saved plan carries the config it was generated against and `up(plan=...)` does not re-inject config into the in-process program, so reconciling would remove plan-time variables (present in the managed-keys sidecar but absent from the now-narrower merged variables) and break `config.require()` at apply time. All other commands reconcile as before
- `--plan` still requires `--auto-approve` in JSON mode (destructive operation guard)
- `setup_command()` returns `SetupResult` (`@dataclass(frozen=True, slots=True)`) with `.config`, `.stack`, `.resolved_targets`, `.resolved_replace`; callers must use attribute access. The previous `__iter__` shim was removed because tuple-unpacking `(c, s, t) = setup_command(...)` silently dropped `resolved_replace` --footgun
- `__version__` in `tlumi/__init__.py` reads via `importlib.metadata.version("tlumi")` with a `PackageNotFoundError` fallback to `"0.0.0+source"` so `import tlumi` works without an installed distribution (doc builds, raw `PYTHONPATH=src python`)
- `--show-secrets` flag on `apply`, `output`, `show`, `state show` --secrets masked as `(sensitive)` by default. Default-masked apply/output paths NEVER trust `stack.outputs()`/`OutputValue.secret`: the SDK marks secret only whole-output secrets, so composite outputs with nested secrets arrive as decrypted plaintext with `secret=False`. Masked values come from `masked_outputs_from_state(safe_export_stack(stack))` (display.py): the Stack resource outputs in exported state keep the secret sig wrappers, which `_sanitize_value()` masks. `--show-secrets` uses `stack.outputs()` plaintext. `print_outputs(outputs)` is a pure renderer of a pre-masked mapping
- `_unwrap_secrets(value)` in sanitize.py: sibling of `_sanitize_value()` (same walk, same `_MAX_DEPTH` cap) that decodes secret wrapper dicts to `json.loads(plaintext)` for the HUMAN `--show-secrets` render paths of `show` and `state show`; ciphertext-only wrappers fall back to `(sensitive)`, undecodable plaintext returns the raw string. JSON paths are untouched (`show --json --show-secrets` and `state show --json --show-secrets` still emit the raw wrapper envelope). show/state show human displays strip dunder keys RECURSIVELY via `_strip_dunder_keys` imported from diffs.py
- `tlumi show` (top-level command) displays all non-Stack resources with URN, ID, parent, deps, inputs, outputs
- `tlumi show --json` emits the same envelope shape as `state pull` (`{"version": N, "deployment": {...}}`) but masks secrets by default; pass `--show-secrets` to get the cleartext form. `tlumi state pull` is the backup primitive and always emits cleartext (deliberate asymmetry: `show` is for display, `pull` is for restore round-trips)
- `tlumi state pull` always outputs pure JSON to stdout; uses `quiet=True` on `get_stack()` --no spinners, no banners
- `tlumi state push <file>` validates: (1) the source path is not a symlink (preventing arbitrary host file reads through a redirected path); (2) the full envelope (`version` is `int >= 1`, `deployment` is `dict`, `deployment.resources` is `list`); (3) each `deployment.resources[*]` entry shape (must be a dict with non-empty string `urn` and `type` keys, and `urn` must start with `urn:pulumi:`); (4) referential integrity across `parent`, `dependencies`, `provider`, and `propertyDependencies` (every referenced URN must be declared in the same file). All checks run before `import_stack` so a malformed file fails fast with a clear error rather than leaving Pulumi mid-import in an undefined state; backs up current state to `.tlumi/backups/state_push_<timestamp>.json`, requires `--auto-approve` in JSON mode
- `tlumi state push --json --auto-approve` emits `{"pushed": true, "resources": N, "backup": "...", "diff": {"added": N, "removed": N, "unchanged": N}}`
- `state push` shows diff summary before confirmation: added/removed/unchanged resources with `display_from_urn()` formatting
- `tlumi version` (top-level command) outputs `tlumi <version>` --no `_do_*` wrapper needed
- `tlumi clean` removes `.tlumi/` directory (venv, state, pulumi_home, backups); uses `find_project_dir()` + `_safe_rmtree()` (symlink-aware, including root path); confirms unless `--auto-approve`; no `--json` flag (simple destructive op)
- `_safe_rmtree()` checks root path for symlink before descending --prevents `.tlumi -> /target` from causing recursive deletion of symlink target
- `run_clean()` detects `.tlumi` as symlink early, unlinks it, and returns without touching target
- `tlumi clean` without `--include-state`: cleans caches and non-state dirs while preserving `state/` and `backups/`; with `--include-state`: removes entire `.tlumi/`
- `safe_export_stack(stack)` in workspace.py wraps `stack.export_stack()` with `CommandError` → `WorkspaceError` conversion, including the redacted CommandError detail in the message; when the text contains "incorrect passphrase" (case-insensitive) the hint points at TLUMI_SECRETS_PASSPHRASE instead of `state unlock`. Use instead of bare `export_stack()`
- `.tlumi/` permissions enforced to `0o700` on every `tlumi init` (not just first creation); fixes insecure dirs from older versions or manual creation; symlink check before mkdir prevents malicious repos from redirecting `.tlumi` via pre-placed symlinks
- Symlink checks required before mkdir and file writes on user-controlled paths (config.py checks `tlumi.yaml` before read; init.py checks `.tlumi`, `.tlumi/state`, `.tlumi/cache`, `.tlumi/cache/pulumi_home`, `tlumi.yaml`, `infra.py`, `requirements.txt`, `.gitignore`; deps.py checks `requirements.txt` before read; workspace.py checks `.tlumi` and subdirs `state/`, `cache/`, `pulumi_home/` in `get_stack()`; state.py checks `backups/` before backup creation in `state rm`, `state mv`, `state push`; state.py also checks the **source file** in `state push` before reading it, so a symlink can't redirect the read into `/proc/self/environ` or arbitrary host paths)
- Actual file/dir writes on user-controlled paths use the `_safeio` helpers (`safe_write_text`, `safe_append_text`, `safe_mkdir`) so a TOCTOU race between the `is_symlink()` check and the write cannot redirect the operation. `safe_write_text`/`safe_append_text` use `os.open(..., O_NOFOLLOW)`; `safe_mkdir` calls `os.mkdir` (atomic, never follows symlinks for the leaf) and `lstat`-verifies the result on `FileExistsError`
- `get_stack()` guards against `.tlumi` being a symlink (malicious repository) before proceeding with directory creation
- Rich markup injection prevention: `escape()` from `rich.markup` applied to resource type, name, and error messages before Rich f-string interpolation in engine.py and cli.py
- `tlumi init` template file writes wrapped in `OSError` catch that raises `ConfigError` with disk/permissions hint; partial writes acceptable since init is idempotent
- `_write_secure(path, data)` in state.py: uses `os.open(O_CREAT|O_EXCL, 0o600)` + `os.fdopen()` for atomic, symlink-safe backup file creation (no permission window, fails if path exists)
- Entry file loaded via `importlib.util.spec_from_file_location` (exact path), not `importlib.import_module` (sys.path search); prevents import hijacking from malicious files on sys.path; module name namespaced as `_tlumi_entry.<stem>` to prevent stdlib collisions; always fresh-loads (no `importlib.reload()`) because dotted name has no real parent package
- `deps add` validates package specifiers: rejects args starting with `-` (argument injection) and containing `\n`/`\r` (control character injection)
- `apply` and `destroy` require `--auto-approve` in JSON mode for destructive operations (no interactive confirmation available)
- `tlumi fmt` ruff failures (returncode != 0/1) print the full ruff output to the console before raising; the user sees file paths, error carets, and ruff's hints rather than just the first line of stderr
- Engine subprocess env scrubs `TLUMI_SECRETS_PASSPHRASE` and all `TLUMI_VAR_*` from the inherited environment (set to `""` in `env_vars`). Pulumi's `LocalWorkspace` uses additional_env semantics so the parent env inherits to provider plugins (third-party code); the passphrase is only needed translated as `PULUMI_CONFIG_PASSPHRASE`, and `TLUMI_VAR_*` values are already promoted to Pulumi config via `set_config()`, so neither needs to remain in the subprocess env
- `extract_property_diffs()` in diffs.py skips `detailed_diff` update entries when the path is not present in either the old or new inputs --a SDK/inputs inconsistency that would otherwise construct `PropertyChange.update(None, None)` and raise in `__post_init__`, surfacing as a counted-but-invisible `callback_errors` increment. The skip is logged at debug only; the next preview reconciles
- Change counts shown in plan/apply/destroy/refresh summaries come from `EventHandler.change_counts()` (which returns `self.op_counts`, tallied in `_count_op` from the resources the engine actually displays: non-Stack, non-SAME, replacements counted once), NOT from Pulumi's `change_summary`/`resource_changes`. Pulumi's summary counts the hidden `pulumi:pulumi:Stack` resource (e.g. `create: 1` for an empty program) but not default providers, so its totals do not match tlumi's resource listing; counting displayed events keeps the summary and the listing consistent by construction (an empty program correctly shows "No changes")
- `EventHandler` collects provider/program `warning` diagnostics into `self.warnings` (alongside `error` -> `self.errors`) via the shared `_collect_diagnostic()`, used by both `on_preview` and `on_update`; commands call `handler.render_warnings()` (no-op when quiet) before the summary and include them under a `"warnings"` JSON key. `on_preview` also collects `error` diagnostics so a failing preview reports real detail instead of a bare "Preview failed."
- `EventHandler.start_live()` returns a `_session()` context that sets `self._closed = True` and `self._live = None` on exit; `on_preview`/`on_update` early-return when `_closed`, dropping late gRPC event callbacks (which can arrive after `up()/destroy()/refresh()` return) instead of re-injecting ghost animation or printing after the summary
- `res_op_failed_event` is handled in `_on_update` (pops the URN from active maps, resets `_last_started_urn`, prints a `x ... failed` line) so a failed resource stops animating and later log lines are not misattributed to it. `OpType.REFRESH` has an `_OP_DISPLAY` entry so `tlumi refresh` shows per-resource progress
- `redirect_program_stdout(active)` in engine.py wraps the engine operation in JSON mode so a `print()` in the user's `infra.py` (in-process inline program) goes to stderr, keeping stdout pure JSON. plan/apply/destroy/refresh wrap their `stack.preview/up/destroy/refresh` calls in it
- `display.err_console` (`Console(stderr=True)`): the `_WorkspaceNoticeHandler` and the `_run()` error path for windowless commands write here so stdout stays a clean data channel. `_run(..., windowless=True)` (used by `state pull` and `output --raw`) skips `create_window()` and routes `print_error`/`_handle_engine_error` to `err_console`
- `display.prompt(message, default="")` pauses the window Live during input (like `confirm()`); used by `tlumi init`'s secret-encryption prompt so the refresh thread does not erase the question. Returns `default` on EOF (optional prompts only, not destructive confirmations)
- `_load_inline_program()`'s `program()` calls `_evict_project_modules()` before each run, deleting every cached module whose `__file__` (checked in BOTH lexical and resolved forms) falls under any user-code root: `project_dir`, `entry.parent`, and `entry.resolve().parent` (each in lexical and resolved forms), excluding `.tlumi/` AND the running interpreter's own prefixes (`sys.prefix`/`sys.base_prefix`, both lexical and resolved forms), so provider SDKs, the pulumi SDK, and tlumi itself stay cached even when the tooling venv sits under a user-code root. The extra roots cover symlinked entry layouts (`src -> ../shared`, symlinked `infra.py`, individually symlinked helpers) whose helper modules resolve OUTSIDE the project dir and would otherwise stay cached across preview->up. Without this, a helper module imported by `infra.py` runs its body (and module-level resource registrations) only once per process, so an interactive apply (preview then up in one process) would omit those resources and Pulumi would destroy them. The resolve guard catches `(OSError, RuntimeError, ValueError, TypeError)` (RuntimeError: Python 3.10 raises it on symlink loops) so a pathological `__file__` skips that module instead of aborting the run
- `redact_text()` masks URL-netloc credentials (`scheme://user:pass@host`) and Azure SAS query params (`sig`/`sv`/`se`); all its regex quantifiers are length-bounded to avoid O(n^2) backtracking (ReDoS) on large provider stderr. It is applied to every `CommandError` text before it reaches a `WorkspaceError` (workspace.py init + config set/remove, cancel, output, state update/import)
- `tlumi state unlock` only consults the local lock dir (`.tlumi/state/.pulumi/locks/`) for the **default** backend (`config.backend.url is None`); a custom `backend.url` stores locks elsewhere, so it falls back to the optimistic "released" message rather than mis-reporting

- `_OP_DISPLAY`/`_OP_SUMMARY_KEY` include `OpType.IMPORT` and `IMPORT_REPLACEMENT` (both count as "import", symbol `=`). `OpType.REFRESH` is deliberately ABSENT from `_OP_SUMMARY_KEY`: during refresh, pre-events carry REFRESH (progress display) while res_outputs_events carry the outcome op (SAME/UPDATE/DELETE), which `_count_op` already counts; verified live
- `EventHandler.stack_outputs_changed` (set in `_on_preview` from Stack res_outputs_events where `new.outputs != old.outputs`) is the signal for output-only changes: Pulumi previews an edited `pulumi.export()` as SAME ops everywhere and `change_summary` reports only `same` (verified live). Interactive apply gates "No changes" on counts OR import count OR this flag; an empty program (no exports) still short-circuits. `plan --json` includes `outputs_changed`. BLIND SPOT: preview scrubs secret output values to the IDENTICAL secret-sig wrapper on both sides (the Automation API `preview()` cannot request unscrubbed secrets), so a changed secret export value with zero resource changes leaves the flag False. `EventHandler.stack_outputs_contain_secrets` (via `_contains_secret_wrapper()`, sentinels imported from sanitize.py) flags the blind comparison; interactive apply and human non-destroy plan then print the muted `SECRET_OUTPUTS_BLIND_HINT` next to "No changes" (changed secret export needs `apply --auto-approve`); `plan --destroy` and `plan --json` never show it (JSON contract unchanged)
- `tlumi import` records imported resources UNPROTECTED by default (Terraform parity, so destroy works); `--protect` opts into Pulumi's default. Import failures surface engine diagnostics
- Both display consoles are `Console(..., emoji=False)`: Rich emoji shortcodes would corrupt values containing `:name:` sequences (IPv6, MACs); tlumi's own strings never use emoji shortcodes. Renderables that build Text via `Text.from_markup` (`_AnimatedStatus`, `_InstallLog` in display.py; `_AnimatedLog` in engine.py) must pass `emoji=False` explicitly: `from_markup` substitutes shortcodes at Text-construction time, BEFORE the console emoji setting is consulted
- `_mask_backend_url()` fails closed: any parse/mask failure returns the `_UNPARSEABLE_URL_PLACEHOLDER` redacted placeholder, never the raw URL; non-numeric ports are dropped from the masked netloc
- `state push` source-path symlink policy: relative paths get a component-wise lstat walk (any symlinked component rejected: attacker-controlled repo surface); ALL paths open the leaf with `O_NOFOLLOW` (TOCTOU-free); absolute user-typed paths tolerate symlinked parents (macOS `/tmp`)
- BrokenPipeError from an early-closed stdout (`tlumi show | head`) exits 0 quietly. Rich >= 13.8 converts EPIPE into SystemExit(1) inside `Console._check_buffer`, so both display consoles are `_TlumiConsole` subclasses whose `on_broken_pipe()` mutes the console (quiet=True) and re-raises BrokenPipeError on the MAIN thread so `_run`'s handler sees it (`_pacify_broken_stdout()` then points the fd at /dev/null to survive interpreter-shutdown flushes); non-main threads keep Rich's SystemExit convention (threading swallows it). An EPIPE that interrupts a FAILURE report preserves the failure exit code (1, or 130 for interrupt) via `_report_failure()`. KeyboardInterrupt in windowless commands prints to `err_console` so stdout stays a data channel
- Typer apps are built via `_make_typer()` which passes `suggest_commands=False` only when typer >= 0.20 (kwarg exists) AND click >= 8.3 (`hasattr(click.exceptions, "NoSuchCommand")`): click 8.3+ appends its own "Did you mean" so typer's copy doubled it; on click < 8.3 typer's suggestion is the only layer and stays on. click is a declared direct dependency (cli.py imports it for this detection)
- Variable coercion rejects unquoted int forms whose text does not round-trip (`0777` -> 511, `1:30` -> 90, `+7` -> 7, `-0` -> 0) with a quote-the-value hint, mirroring the float rejection; plain decimal ints (incl. negatives) unchanged

## Formatting

- `tlumi fmt` formats project Python files using `ruff format` via `uv tool run` (no ruff install needed)
- `tlumi fmt --check` exits non-zero if files need formatting (CI-friendly)
- Passes project directory to ruff (not individual files); ruff handles its own file discovery and `.gitignore` respect
- `--exclude .tlumi` passed to ruff to skip the cache/state directory
- Does not require `load_config()`, only `find_project_dir()` (works even with invalid tlumi.yaml)

## Window Context (Live Display)

- `_run()` in cli.py wraps non-JSON commands in `create_window()` --a single outer `Live(transient=True)` display
- All `console.print()` calls route above the Live region automatically (Rich behavior)
- `animated_status()` composes into the window's Live via `set_inner()`/`clear_inner()` instead of creating a new `Live`
- Engine's `start_live()` returns a `WindowLiveProxy` that duck-types as `Live` --routes `update()`/`console.print()` through the window
- Only one `Live` display exists at any time --Rich doesn't support nested `Live` contexts
- `_run()` has a bare `except Exception` that restores cursor visibility (`console.show_cursor(True)`) before re-raising unexpected errors
- `confirm()` pauses the window (`Live.stop()`) during input, resumes after via `try/finally` --prevents refresh thread from overwriting the prompt; on `EOFError` (piped stdin with no answer) it **raises `TlumiError`** rather than returning `False`. Silent cancellation under a pipe makes scripted destructive ops indistinguishable from a successful no-op; matching the JSON-mode rule (`--auto-approve` required for destructive ops) failures loudly here too
- `console.show_cursor(True)` called after `Live.start()` to keep cursor visible during operations

## Change Safety Checklist

When modifying code, verify these patterns are preserved:

- **Resource resolution**: all lookup paths (URN, type::name, name-only) must handle ambiguity. If you add a new matching strategy to `resolve_resource()`, collect matches into a list and raise `WorkspaceError` when `len(matches) > 1`, consistent with the name-only branch
- **Pulumi config key namespacing**: `get_all_config()` returns keys as `namespace:key`. Project keys use `projectname:key`, provider keys use `provider:key` (e.g. `aws:region`). When processing project keys, strip the project prefix AND skip bare keys containing `:` (they belong to provider sub-namespaces that happen to share the project's prefix)
- **Symlink checks**: when adding new directory creation or file writes on user-controlled paths, add `is_symlink()` check before the operation, consistent with existing patterns in init.py, workspace.py, deps.py, and state.py
- **JSON mode contract**: any new output path in `_run()` or command code must check `json_output` and emit structured JSON instead of Rich markup. This includes error paths, interrupt handlers, and log handlers
- **Rich markup safety**: user-controlled strings (resource names, types, error messages, file paths) must pass through `escape()` before Rich f-string interpolation. Use `markup=False, highlight=False` for raw data output
- **Mutual exclusion enforcement**: `plan --destroy` rejects `--replace` and `--out`; `apply --plan` rejects `--var`, `--var-file`, `--target`, `--replace`. These are checked at the top of `run_plan()`/`run_apply()` before any setup
- **State/recovery commands must use `runtime=False`**: `state list/show/rm/mv/pull/push`, `show`, `state unlock`, and `output` call `get_stack(..., runtime=False)`. `output` reads existing stack outputs from state and does not invoke `infra.py`, so it joins the recovery cohort. Only commands that actually execute `infra.py` (plan/apply/destroy/refresh/import) use the default `runtime=True`. Recovery commands must not depend on a healthy infra.py, venv, or stack config --those are exactly what may be broken when a user reaches for state recovery

## Key Technical Details

- uv auto-installed for venv/package management --`ensure_uv()` checks PATH, then shared cache `~/.tlumi/cache/uv/<version>/uv`, then downloads from GitHub releases (SHA-256 verified)
- Venv created via `uv venv` (no pip seeded), packages installed via `uv pip install --python <venv>/bin/python`
- Pulumi CLI auto-installed via `PulumiCommand.install()` --tries shared cache `~/.tlumi/cache/pulumi/<sdk_version>/` first, falls back to per-project `.tlumi/cache/pulumi_home/`; shared-cache install failure logs at WARNING (not DEBUG) so a corrupted `~/.tlumi/cache/` surfaces instead of silently falling back
- Backend: `file://<project>/.tlumi/state` (local default); remote backend auth (S3/azblob/gs) is delegated to Pulumi's DIY backend via inherited env vars (no scheme allowlist) and documented for users in `docs/backends.md`. Keep that doc in sync when backend/env handling changes
- `PULUMI_ACCESS_TOKEN=""` to bypass Pulumi Cloud
- `pulumi_home` set to `.tlumi/cache/pulumi_home/` for full isolation
- Python venv for providers in `.tlumi/cache/venv/`
- `.tlumi/` layout separates persistent data (`state/`, `backups/`) from re-creatable caches (`cache/venv/`, `cache/pulumi_home/`)
- `sys.path.append()` (not `insert(0)`) for project dir --gives stdlib priority over user files
- Entry path nested import: `_load_inline_program()` adds entry's parent directory to sys.path when it differs from project root (e.g. `entry: src/infra.py` adds `src/` to sys.path)
- Secrets opt-in: no passphrase + `allow_unencrypted: false` (default) → `ConfigError`; set `secrets.allow_unencrypted: true` in tlumi.yaml to allow unencrypted secrets without passphrase
- Config validation: `allow_unencrypted` and `warn_unencrypted` must be actual booleans (not strings like `"false"`); `backend.url` must be `str | None`; non-dict YAML root raises `ConfigError`
- Empty passphrase notice emitted once per session as muted info (not a warning --user already opted in); suppressible via `secrets.warn_unencrypted: false`
- `_check_gitignore()` in workspace.py warns once if `.tlumi/` is not in `.gitignore`
- Remote backend URL displayed once per session in `get_stack()` when backend is not `file://` --suppressed in quiet/JSON mode; credentials in URL masked via `_mask_backend_url()`. Mitigations cover four shapes: (1) password in netloc → `user:***@host`; (2) bare-username-as-token netloc → `***@host` (backend URLs almost never use bare usernames for identification, so any lone userinfo is treated as a credential); (3) sensitive query parameters --case-insensitive exact match against `_SENSITIVE_QUERY_KEYS` (covers Azure SAS fields like `sv`/`se`/`sig` and AWS/Google variants) **AND** substring match against `_SENSITIVE_QUERY_SUBSTRINGS` (`token`, `secret`, `password`, `passwd`, `credential`, `apikey`) so `authToken`, `oauth_token`, `refresh_token`, `x-api-key` etc. are caught without enumerating every vendor; (4) URL fragments containing `=` are parsed as queries and masked the same way --catches OAuth implicit-flow tokens and signed-URL schemes that put tokens after `#`
- `get_stack()` accepts `runtime: bool = True`. Pass `runtime=False` for state-only commands (state list/show/pull/rm/mv/push, show, cancel, output) --skips the venv existence check, uses a no-op program instead of loading `infra.py`, and skips stack-config reconciliation. Recovery commands must not depend on a healthy infra.py, venv, or stack config: those are exactly what may be broken when a user reaches for `state pull` or `state unlock`. Runtime mode (default) keeps the existing behavior for plan/apply/destroy/refresh/import. **Important limitation**: `runtime=False` does *not* tolerate a broken `tlumi.yaml`. Every recovery command still starts with `load_config(find_project_dir())`, so a missing or unparseable `tlumi.yaml` blocks recovery. Real config-level recovery would require name-guessing (the Pulumi stack is tied to the project name in `tlumi.yaml`) which risks creating a new stack instead of finding the existing one --so we refuse rather than corrupt. If `tlumi.yaml` is unparseable, the user must fix it before any tlumi command will run; the state files in `.tlumi/state/` remain intact and accessible via the Pulumi CLI directly
- `find_project_dir()` checks cwd for `tlumi.yaml`, raises `ProjectNotFoundError` if not found

## Testing

- `pytest` for unit tests
- Test files in `tests/` directory
- Run: `python -m pytest tests/`

## Git

- Personal project: user.name="Jay W", user.email="git.jaydoubleu@gmail.com"
