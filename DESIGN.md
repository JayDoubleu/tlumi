# Terralumi (tlumi) Architecture

This document is the architecture reference. For installation and usage, see [README.md](README.md). For internal conventions and invariants, see [.claude/CLAUDE.md](.claude/CLAUDE.md).

## What tlumi is

A Python CLI that wraps the Pulumi Automation API to provide a Terraform-like workflow with local state by default. Users write Pulumi Python programs and tlumi handles the workspace, state, and process lifecycle.

```
tlumi.yaml + infra.py
        |
        v
+-----------------------------------------+
| tlumi CLI (Typer + Rich)                |
|   - reads tlumi.yaml                    |
|   - manages .tlumi/                     |
|   - translates commands to Automation   |
|   - renders Pulumi events to Rich Live  |
+--------------------+--------------------+
                     |
                     v
+-----------------------------------------+
| Pulumi Automation API (LocalWorkspace)  |
|   - backend = file://.tlumi/state       |
|   - PULUMI_ACCESS_TOKEN="" (Cloud opt-  |
|     out, documented Pulumi behavior)    |
|   - PULUMI_HOME=.tlumi/cache/pulumi_home|
|   - inline program = infra.py           |
+--------------------+--------------------+
                     |
                     v
+-----------------------------------------+
| Pulumi CLI binary (auto-installed)      |
|   - shared cache: ~/.tlumi/cache/pulumi |
|     /<sdk_version>/ (across projects)   |
|   - per-project fallback:               |
|     .tlumi/cache/pulumi_home/           |
+-----------------------------------------+
```

## ADRs (Architecture Decision Records)

### ADR-001: Pure Python

We use Python end-to-end (no Rust core, no hybrid). The Pulumi Automation API is a Python library; Python is already a hard requirement because users write their infra in it; CLI startup is dominated by IaC operations measured in seconds. Mixing languages would add complexity for no measurable benefit.

### ADR-002: Local state by default

The default backend is `file://.tlumi/state`. Pulumi Cloud is opt-out, not opt-in: we set `PULUMI_ACCESS_TOKEN=""` so the SDK never prompts for login. Remote backends are configured by setting `backend.url` in `tlumi.yaml`. This matches the Terraform mental model and removes "create a Pulumi account" from the first-run path.

Backend authentication is delegated entirely to Pulumi's self-managed (DIY) backend: tlumi forwards the parent environment to the engine and applies no scheme allowlist, so each cloud's standard credential chain (CLI login, managed identity, instance roles, static keys, SAS) applies. Per-cloud setup is documented in [docs/backends.md](docs/backends.md).

Trade-off: the local file backend has no distributed locking. Same-machine locking works (Pulumi writes lock files under the backend root, `.tlumi/state/.pulumi/locks/`). For team use we recommend a remote backend that supports locking. Documented as an accepted risk; surfaced in the README under "State Locking".

### ADR-003: Single internal stack "default"

User-facing model: one project directory = one deployment. Internally, all operations target a Pulumi stack named `default`. We never expose Pulumi stacks/workspaces in the CLI.

To run multiple environments, users create separate project directories (the `infra/dev/`, `infra/staging/` directory-per-environment pattern in the README). `--var` does not isolate state and is documented as such.

A future `--env` flag for named environments is on the roadmap but deferred: the directory-per-environment pattern handles current users, and named environments would add stack-management complexity (env-specific config merging, isolation rules, migration) that should be validated by real feedback first.

### ADR-004: Inline program loaded via `spec_from_file_location`

We load `infra.py` via `importlib.util.spec_from_file_location` against the exact path, namespaced as `_tlumi_entry.<stem>`. We never use `importlib.import_module` (which would search `sys.path`). This prevents import hijacking from malicious files on the user's `sys.path` and avoids stdlib-name collisions. `sys.path` is `append`-ed (not `insert(0)`) so user files cannot shadow stdlib modules (e.g. a local `secrets.py`).

### ADR-005: Two-tier Pulumi CLI install

The CLI binary is cached under `~/.tlumi/cache/pulumi/<sdk_version>/` and shared across all tlumi projects on the machine. If the shared install fails (perms, corrupted cache), tlumi falls back to per-project `.tlumi/cache/pulumi_home/` and logs a `WARNING` so the user can see the cache is unhealthy.

Trust model: we delegate binary acquisition to Pulumi's own `PulumiCommand.install()`. tlumi does no extra checksum verification on the CLI binary; that is the SDK's responsibility. uv is treated differently: when not on `PATH`, tlumi downloads it from GitHub releases with SHA-256 verification.

### ADR-006: Source position suppression (SDK patch)

`sdk_compat.py` monkey-patches private SDK functions (`_get_stack_trace`, `_get_source_position`) in `pulumi.runtime.resource` to return empty values, preventing local filesystem paths from being persisted in state files. This is load-bearing for users with remote shared backends, where local paths would otherwise leak into shared state.

The patch is private-API-coupled and fails open: if the SDK layout changes, we log a `WARNING` and let state continue (paths will reappear until either the patch is updated or upstream lands a real fix). Failing closed would break every tlumi command on SDK upgrades, which is worse than a documented behavior regression.

### ADR-007: Runtime-vs-recovery decoupling

`get_stack()` accepts `runtime: bool = True`. Recovery commands (`state list/show/rm/mv/pull/push`, `show`, `state unlock`, `output`) call with `runtime=False`, which:
- skips the venv existence check
- uses a no-op program instead of loading `infra.py`
- skips stack-config reconciliation

A user reaching for `state pull` or `state unlock` is most often doing so because something is broken: a syntax error in `infra.py`, a missing venv, or stuck stack config. Recovery must not depend on the things that are likely broken.

Operational commands (`plan`, `apply`, `destroy`, `refresh`, `import`) use the default `runtime=True`.

**Limit of the recovery contract**: `runtime=False` does not tolerate a broken `tlumi.yaml`. Every recovery command still calls `load_config(find_project_dir())` first. A real "config-broken" recovery mode would need to guess the project name from the directory or the state files, but a wrong name silently creates a *new* Pulumi stack instead of finding the existing one, so we refuse rather than corrupt. When `tlumi.yaml` is unparseable, the user must fix it before any tlumi command will run; the state files in `.tlumi/state/` remain intact and accessible via the Pulumi CLI directly.

### ADR-008: Event-driven plan/apply rendering

We use Pulumi's `on_event` callbacks (not stdout parsing) to drive a Rich `Live` display. There is exactly one `Live` window per `tlumi` invocation, owned by `_run()` in `cli.py`. The engine's `start_live()` returns a duck-typed proxy that routes calls through the existing window (Rich does not support nesting multiple `Live` contexts).

Property-level diffs (CREATE/UPDATE/REPLACE/DELETE) are extracted from `StepEventMetadata` and rendered as Rich `Tree` structures with per-leaf flattening when values exceed a length threshold.

### ADR-009: Backup-before-mutate for state ops

Every state mutation (`state rm`, `state mv`, `state push`) writes a timestamped backup to `.tlumi/backups/state_<op>_<ts>.json` via `O_CREAT|O_EXCL` + `0o600` BEFORE calling `import_stack`. On failure, the error message names the backup file with a literal `tlumi state push <file>` recovery command. Backups rotate to the most recent 10 **per op_prefix** (`state_rm_`, `state_mv_`, `state_push_` each get their own quota), so a chatty operation cannot rotate out the backup the user actually wanted to revert to.

`state mv` allows renaming a target that has children and warns, listing them: child URNs embed only the parent's TYPE chain (never its name), so rewriting each child's `parent` reference to the new URN is sufficient and the children's own URNs are unaffected. The warning covers the remaining hazard: a child whose code-side name is derived from the parent name (for example `f"{parent}-child"`) will still be replaced on the next apply.

`state push` validates the source path (refuses symlinks, including symlinked parent directories for relative paths, so a redirected path can't read process env or arbitrary host files; the leaf is opened with `O_NOFOLLOW`), the full envelope (`version: int >= 1`, `deployment: dict`, `deployment.resources: list`), each resource entry shape (dict with non-empty string `urn` and `type`, `urn` starts with `urn:pulumi:`), and referential integrity across `parent`/`dependencies`/`provider`/`deletedWith`/`propertyDependencies` BEFORE invoking `import_stack`, so malformed input fails fast.

### ADR-010: Sentinel sanitization

Pulumi marks secrets and unknown values with sentinel UUIDs in state. Our `_sanitize_value()` walks dicts and lists (depth-capped at 50) and replaces:
- secret-wrapper dicts (dicts carrying Pulumi's secret signature sentinel: the `4dabf...`/`1b470...` UUID pair) with `"(sensitive)"`
- unknown sentinel strings with `"(known after apply)"`
- leaf strings containing the secret sig (defense-in-depth for debug logs / embedded JSON)
- the literal `[secret]` string

At max depth we return `(sensitive)` as the safe default to prevent leaks under pathological nesting. `--show-secrets` bypasses sanitization for `apply`, `output`, `show`, `state show`.

### ADR-011: Variable precedence

Highest to lowest: `--var KEY=VALUE` > `--var-file PATH` > `TLUMI_VAR_*` env vars > `tlumi.yaml`. Variable values must be scalars; lists and dicts are rejected. Accepted unquoted forms are str, plain decimal int, and canonical `true`/`false` bool. Every lossy YAML form is rejected with a hint to quote the value: floats (YAML silently rewrites `1.10` to `1.1`), non-decimal ints (`0777` to 511, `0x1A` to 26, `1:30` to 90, `+7` to 7), YAML 1.1 boolean keywords (`yes`/`no`/`on`/`off`, the "Norway problem" where an ISO country code `NO` becomes `false`), and timestamps/dates (`2026-07-02T10:00:00Z`, silently reformatted). A quoted value arrives as a string. Embedded NUL bytes in a key or value are also rejected (they cannot be passed to the Pulumi CLI). YAML booleans are preserved as lowercase strings (`"true"`, `"false"`). This is enforced by a strict YAML loader that retags each lossy scalar to a raw-text marker so the rejection can quote exactly what the user wrote.

`--var` appears in shell history, so we warn when key names look sensitive (`secret`, `password`, `token`, etc.) unless the caller is in JSON/quiet mode. Documented in the README under "Security".

### ADR-012: Remote backend URL masking

When a remote backend is configured, the URL is logged once per session. We mask before display:
- password in `user:pass@host` netloc with `***`
- bare-username-as-token netloc (`token@host`, common for S3-compatible backends) entirely with `***`
- sensitive query parameters with `***`, matched two ways:
  - case-insensitive exact match against `_SENSITIVE_QUERY_KEYS` (covers Azure SAS fields `sv`/`se`/`sp`/`srt`/`ss`/`sig`/`signature`, account keys, AWS/Google variants)
  - case-insensitive substring match against `_SENSITIVE_QUERY_SUBSTRINGS` (`token`, `secret`, `password`, `passwd`, `credential`, `apikey`, `signature`) so `authToken`, `oauth_token`, `refresh_token`, `x-api-key` etc. are caught without enumerating every vendor
- URL fragments containing `=` (some OAuth implicit-flow and signed-URL schemes put tokens after `#`) are parsed as queries and masked the same way

Query key matching is case-insensitive (`key.lower() in _SENSITIVE_QUERY_KEYS`) so Azure's camelCase variants (`AccessKey`, `SharedAccessKey`, `SAS`, `Signature`) are caught.

## Project Layout

```
src/tlumi/
├── __init__.py         # __version__ via importlib.metadata (fallback "0.0.0+source")
├── __main__.py         # python -m tlumi entry point
├── cli.py              # Typer app, command registration, _run() error funnel
├── commands/
│   ├── _common.py      # setup_command(): shared config/stack/target setup
│   ├── init.py         # tlumi init
│   ├── clean.py        # tlumi clean (and --include-state)
│   ├── plan.py         # tlumi plan (--destroy, --out)
│   ├── apply.py        # tlumi apply (--plan, --show-secrets)
│   ├── destroy.py      # tlumi destroy
│   ├── cancel.py       # tlumi state unlock
│   ├── output.py       # tlumi output (--show-secrets)
│   ├── deps.py         # tlumi deps add/install/list
│   ├── validate.py     # tlumi validate (no Pulumi needed)
│   ├── fmt.py          # tlumi fmt (delegates to ruff)
│   ├── refresh.py      # tlumi refresh
│   ├── state.py        # tlumi state list/show/rm/mv/pull/push
│   ├── show.py         # tlumi show
│   └── import_cmd.py   # tlumi import
├── config.py           # tlumi.yaml parsing, ProjectConfig, merge_variables
├── workspace.py        # Pulumi Automation API wrapper, get_stack, masking
├── engine.py           # EventHandler, Rich Live display, op dispatch
├── diffs.py            # PropertyChange, diff extraction and expansion
├── sanitize.py         # Sentinel detection and marker substitution
├── sdk_compat.py       # SDK monkey-patch for source position suppression
├── resolve.py          # URN/type::name/name resolution with ambiguity errors
├── display.py          # Rich rendering, window context, animated_status
├── errors.py           # TlumiError hierarchy, Diagnostic NamedTuple
├── redact.py           # Free-form credential redaction for stderr/diagnostic strings
├── _safeio.py          # Symlink-safe write/mkdir primitives (O_NOFOLLOW)
├── uv.py               # uv binary management (download + SHA-256)
├── py.typed            # PEP 561 marker for downstream type checkers
└── templates/          # Jinja2 templates for tlumi init

examples/
├── random/             # No-credentials demo (pulumi-random)
├── aws-s3/             # Minimal S3 bucket with versioning
├── azure-modular/      # Reusable ComponentResource components
└── azure-container-apps/ # Docker build + ACR + Container App
```

On-disk layout for a user's project:

```
my-infra/
├── tlumi.yaml          # project config
├── infra.py            # entry point (Pulumi Python program)
├── requirements.txt    # provider packages
├── .gitignore          # auto-generated by tlumi init
└── .tlumi/             # internal, 0o700 perms, symlink-checked
    ├── state/          # local state files (persistent)
    ├── backups/        # state mutation backups (persistent)
    └── cache/          # re-creatable
        ├── venv/       # uv-managed venv for provider packages
        └── pulumi_home/ # per-project Pulumi home (fallback)
```

Plus the shared cache at `~/.tlumi/cache/`:
- `pulumi/<sdk_version>/`: Pulumi CLI shared across projects
- `uv/<version>/uv`: uv binary

## tlumi.yaml schema

```yaml
project:
  name: my-infrastructure
  entry: infra.py                  # default: infra.py

backend:                           # optional; default is file://.tlumi/state
  url: "s3://my-state-bucket?region=us-east-1"

secrets:                           # optional
  allow_unencrypted: false         # default: false (refuse if no passphrase)
  warn_unencrypted: true           # default: true (info-level notice once)

variables:                         # passed as Pulumi config
  environment: dev
  region: us-east-1
```

Validation enforced by `ProjectConfig.__post_init__`: name matches `[a-zA-Z][a-zA-Z0-9_-]*` (anchored with `\Z`, so a trailing newline is rejected), entry path has no `..` and is not absolute, variable keys have no `:` (reserved for Pulumi provider config namespaces). `load_config()` additionally enforces that `secrets` boolean fields are real bools (not `"false"` strings) and that null variable values are rejected rather than coerced to the string `"None"`.

## Error model

Top-level error type is `TlumiError` with subclasses for category (`ConfigError`, `WorkspaceError`, `EngineError`). Every `TlumiError` may carry a `hint` for actionable user guidance. `EngineError` additionally carries a list of structured `Diagnostic` records extracted from Pulumi events.

The CLI funnel `_run()` in `cli.py` catches `TlumiError` at the boundary and renders it via Rich (or as `{"error": ..., "hint": ..., "diagnostics": [...]}` in JSON mode). Programming errors (`TypeError`, `KeyError`, etc.) propagate with full tracebacks; we do not swallow them.

## Accepted risks

| Risk | Severity | Rationale |
|------|----------|-----------|
| Local file backend has no distributed locking | Low | Same-machine locking works; remote backends recommended for teams. Documented in README. |
| Empty `PULUMI_ACCESS_TOKEN` overrides user env | Low | Required to bypass Pulumi Cloud login. Local-first by design. |
| `site.addsitedir()` mutates the process-global `sys.path` | Low | Required for inline programs to access provider packages. No alternative without losing `on_event`. |
| Pulumi CLI auto-downloaded | Medium | Inherent to `PulumiCommand.install()`. Trust delegated to the SDK. |
| uv auto-downloaded | Medium | SHA-256 verified from GitHub releases. PATH-installed uv preferred when available. |
| SDK source position patch is private-API-coupled | Medium | Fails open with `WARNING` if SDK layout shifts. Worse alternative is hard-fail on every SDK upgrade. |
| Single stack, no env isolation via `--var` | Medium | Documented and warned about; separate directories required for true isolation. Named environments deferred. |

## Prior art

| Tool | Lesson |
|------|--------|
| Terraform | The `init`/`plan`/`apply` workflow is the right shape. Local state as default is the right call. |
| Pulumi | Excellent engine, defaults optimized for vendor lock-in. Wrap with Automation API; replace UX. |
| CDKTF (deprecated) | Don't be just a wrapper; add genuine value or don't bother. |
| AWS CDK | Construct model is good. Single-cloud lock-in is bad. |
| Terragrunt | Auto state management and DRY config are real UX wins. |
| Troposphere | Demand for Python IaC exists; template-only tools are incomplete. |

The niche tlumi occupies (simplified opinionated CLI on top of Pulumi Automation API, local-first, Terraform-like) was genuinely open at project start.
