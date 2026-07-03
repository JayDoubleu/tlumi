# tlumi

[![CI](https://github.com/jaydoubleu/tlumi/actions/workflows/ci.yml/badge.svg)](https://github.com/jaydoubleu/tlumi/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://github.com/jaydoubleu/tlumi)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](https://github.com/jaydoubleu/tlumi/blob/main/LICENSE)

*terra + lumi: Terraform workflow, Pulumi engine.*

Terraform-like workflow for Python infrastructure-as-code, powered by Pulumi.

**tlumi** gives you the simplicity of `terraform init/plan/apply` with the power of writing your infrastructure in Python. It uses the Pulumi Automation API as its engine but keeps state local by default. No cloud accounts, no Pulumi home directory pollution, no ceremony.

**For you if:** you reach for Terraform's workflow but want a real programming language for your IaC, and you don't want a vendor login or cloud-hosted state as the default.

> [!WARNING]
> **Alpha / experiment.** tlumi is early and the CLI surface may still change. That said, I use it daily for my own infrastructure, it has a broad test suite, and because it is a thin workflow layer over the Pulumi Automation API, your infrastructure code stays plain Pulumi: the same `infra.py` runs under the `pulumi` CLI directly, so you are never locked in. Try it, file issues, and pin a commit if you depend on it.

## Why tlumi?

| What we love about Terraform | What we love about Pulumi | What both get wrong |
|------------------------------|---------------------------|---------------------|
| Simple workflow: init, plan, apply | Python and real languages for IaC | Terraform: HCL is limiting |
| Local state by default | 150+ providers, multi-cloud | Pulumi: cloud state by default, `~/.pulumi` pollution |
| Just `cd dir && terraform init` | Testable, composable infrastructure | Both: ceremony and complexity where it shouldn't exist |

tlumi takes the workflow you know from Terraform and combines it with the full power of Python and Pulumi's provider ecosystem.

## Prerequisites

- **Python 3.10+**
- **Linux and macOS.** Windows is not currently supported.
- **Cloud provider credentials** only for cloud deploys. You can try tlumi with zero credentials using the `pulumi-random` provider.

## Quick Start (no credentials needed)

tlumi is not yet on PyPI. Install from source using [uv](https://github.com/astral-sh/uv):

```bash
git clone https://github.com/jaydoubleu/tlumi.git
cd tlumi
uv tool install .            # installs the `tlumi` CLI from source
cd examples/random
tlumi init
tlumi plan
tlumi apply
```

This creates a few `pulumi-random` resources (random IDs, pets, a password) entirely in local state. No AWS, no Azure, no Pulumi Cloud account.

When you're ready to do real work, swap to an example with a provider you care about:
- [`examples/aws-s3`](https://github.com/jaydoubleu/tlumi/tree/main/examples/aws-s3): minimal S3 bucket with versioning
- [`examples/azure-container-apps`](https://github.com/jaydoubleu/tlumi/tree/main/examples/azure-container-apps): Docker build + ACR + Container App
- [`examples/azure-modular`](https://github.com/jaydoubleu/tlumi/tree/main/examples/azure-modular): reusable `ComponentResource` modules

Or start from scratch:

```bash
mkdir my-infra && cd my-infra
tlumi init
tlumi deps add pulumi-aws       # or any other Pulumi provider
# Edit infra.py
tlumi plan
tlumi apply
```

You can also run tlumi as a module: `python -m tlumi <command>`.

## Commands

| Command | Description |
|---------|-------------|
| `tlumi init` | Initialize a new project |
| `tlumi plan` | Preview infrastructure changes (supports `--destroy`, `--out`) |
| `tlumi apply` | Apply infrastructure changes (supports `--plan`, `--show-secrets`) |
| `tlumi destroy` | Tear down all resources |
| `tlumi refresh` | Reconcile state with actual cloud resources |
| `tlumi validate` | Validate project configuration and syntax (no Pulumi needed) |
| `tlumi fmt` | Format project Python files (uses ruff) |
| `tlumi import` | Import existing cloud resources into state |
| `tlumi show` | Display all resources in state (supports `--show-secrets`) |
| `tlumi output` | Display output values (supports `--show-secrets`) |
| `tlumi clean` | Remove caches (keeps state); add `--include-state` to remove `.tlumi/` entirely |
| `tlumi version` | Show tlumi version |
| `tlumi state list` | List resources in state |
| `tlumi state show` | Show details of a specific resource (supports `--show-secrets`) |
| `tlumi state rm` | Remove a resource from state |
| `tlumi state mv` | Rename a resource in state (rewrites child references, warns about name-derived children) |
| `tlumi state pull` | Export state as JSON |
| `tlumi state push` | Replace state from a JSON file (supports `--auto-approve`) |
| `tlumi state unlock` | Release a stale state lock |
| `tlumi deps add` | Add provider packages |
| `tlumi deps install` | Reinstall dependencies (after manual requirements.txt edits) |
| `tlumi deps list` | List installed packages |

Common flags:
- `--json`: machine-readable output (plan, apply, destroy, refresh, validate, output, show, and state list/show/push)
- `--verbose`: full error output (before or after the command name)
- `--var KEY=VALUE` / `--var-file PATH`: pass variables to `plan`/`apply`/`destroy`/`refresh`/`validate`/`import` (also via `TLUMI_VAR_*` env vars)
- `--target NAME`: limit operations to specific resources (`plan`/`apply`/`destroy`/`refresh`)
- `--replace NAME`: force replacement of specific resources (`plan`/`apply` only; Pulumi does not support replace on `destroy` or `refresh`)
- `--auto-approve`: skip confirmation prompts on destructive operations
- `--show-secrets`: display secret values instead of `(sensitive)` (`apply`, `output`, `show`, `state show`). Note: `--json` output stays masked by default (unlike Terraform's `output -json`, which prints secrets); combine `--json --show-secrets` to emit real values.

## How It Works

- Write infrastructure in **Python** using Pulumi providers (`infra.py`)
- Configure your project in **`tlumi.yaml`**
- State is stored **locally** in `.tlumi/state/` (or use a remote backend like S3, Azure Blob, or GCS by setting `backend.url` in `tlumi.yaml`; see [docs/backends.md](https://github.com/jaydoubleu/tlumi/blob/main/docs/backends.md) for authentication)
- No Pulumi Cloud account needed
- No `~/.pulumi` directory created
- Pulumi CLI is auto-installed on first use, cached at `~/.tlumi/cache/pulumi/<version>/` (shared across projects) with per-project fallback at `.tlumi/cache/pulumi_home/`

## Terraform to tlumi

Python replaces most of Terraform's DSL constructs with standard language features:

| Terraform | tlumi / Python |
|---|---|
| `locals { name = "..." }` | Python variables: `name = f"rg-{env}"` |
| `variable` + `validation {}` | `--var` / `--var-file` / `TLUMI_VAR_*` + Python assertions |
| `provider "azurerm" { ... }` | `provider_config:` in `tlumi.yaml` (e.g. `azure-native:location: westeurope`) |
| `output {}` | `pulumi.export()` |
| `module {}` | `ComponentResource` classes + Python imports |
| `count` / `for_each` | Python loops and list comprehensions |
| `condition ? true : false` | Python `if`/`else` |
| `templatefile()` | f-strings or Jinja2 |
| `depends_on` | `pulumi.ResourceOptions(depends_on=[...])` |
| `lifecycle` | `pulumi.ResourceOptions(ignore_changes=[...])` etc. |
| `moved {}` | `pulumi.ResourceOptions(aliases=[...])` |
| `data {}` sources | Pulumi `get_*()` functions |
| `import {}` | `tlumi import` |
| `provisioner` | `subprocess.run()` or any Python library |
| `terraform fmt` | `tlumi fmt` (runs `ruff format`) |
| `terraform console` | `python -c` with your project imports |
| `terraform output -json` (prints secrets) | `tlumi output --json` masks secrets; add `--show-secrets` to reveal |

## Design Philosophy

- **Local-first**: state lives in your project directory by default
- **Zero ceremony**: no login, no cloud accounts, no stacks to manage
- **Terraform workflow**: `init -> plan -> apply -> destroy`, the workflow you know
- **Python power**: real programming language, real testing, real abstractions

See [DESIGN.md](https://github.com/jaydoubleu/tlumi/blob/main/DESIGN.md) for the architecture and the decisions behind it.

## Powered by Pulumi

tlumi uses the [Pulumi Automation API](https://www.pulumi.com/docs/using-pulumi/automation-api/) as its engine. Your `infra.py` files are standard Pulumi Python programs and work with both tlumi and the Pulumi CLI. The two tools are complementary, not competing.

Key points:
- Pulumi SDK and CLI are open source (Apache 2.0)
- The state format is Pulumi-specific (no cross-tool migration today)
- Pulumi Inc. is VC-funded; the same licensing dynamics that affected Terraform could apply here
- Pulumi's DCO contribution model makes re-licensing harder, but not impossible

## Multiple Environments

tlumi uses a single state per project directory. The `--var` flag changes variable values but does **not** isolate state. Running `tlumi destroy` destroys whatever is in the current state, regardless of which `--var` values you pass.

To manage multiple environments (dev, staging, prod), use **separate directories** with their own `tlumi.yaml` and state:

```
infra/
  dev/
    tlumi.yaml    # variables: environment: dev
    infra.py -> ../shared/infra.py
  staging/
    tlumi.yaml    # variables: environment: staging
    infra.py -> ../shared/infra.py
  shared/
    infra.py      # shared infrastructure code
```

A built-in `--env` flag for named environments is planned for a future release.

## State Locking (read this before team use)

The local file backend takes a per-stack lock (under `.tlumi/state/.pulumi/locks/`), so a second `tlumi apply` against the same project directory fails with a lock error rather than running concurrently. This protection is best-effort and single-machine only: lock acquisition is not atomic, and there is no distributed locking across machines or shared filesystems. For team use, configure a remote backend (S3, Azure Blob) that supports locking via `tlumi.yaml`:

```yaml
backend:
  url: s3://my-tlumi-state-bucket?region=us-east-1
```

`tlumi state unlock` releases a stale lock from an interrupted operation.

Remote backends authenticate via your ambient cloud credentials (CLI login, managed identity, instance roles, static keys, or SAS), not via `tlumi.yaml`. See [docs/backends.md](https://github.com/jaydoubleu/tlumi/blob/main/docs/backends.md) for per-cloud setup and gotchas.

## Provider Version Pinning

tlumi uses `requirements.txt` for provider dependencies. Pin versions to avoid surprises:

```
pulumi-aws>=7.0.0,<8.0.0
pulumi-random>=4.16.0,<5.0.0
```

For fully reproducible builds (locking transitive dependencies), use `uv pip compile`:

```bash
uv pip compile requirements.txt -o requirements.lock
# Then install from the lock file:
# uv pip install -r requirements.lock --python .tlumi/cache/venv/bin/python
```

There is no built-in equivalent of Terraform's `.terraform.lock.hcl` yet. This is a known gap.

## Security

- **`infra.py` is executed code.** Only run `tlumi plan/apply` in directories you trust. The entry file is loaded and executed by the Python interpreter.
- **`.tlumi/` directory permissions.** Created with `0700` (owner-only access) to protect state and secrets.
- **Secrets encryption.** Set `TLUMI_SECRETS_PASSPHRASE` to encrypt sensitive values in state. If no passphrase is set and you have not opted into plaintext, tlumi refuses to proceed; explicitly set `secrets.allow_unencrypted: true` in `tlumi.yaml` to allow unencrypted secrets. tlumi will not silently store secrets in plaintext.
- **`--var` and shell history.** Variable values passed via `--var KEY=VALUE` appear in shell history. Use `--var-file` with a gitignored file or `TLUMI_VAR_*` environment variables for secrets.
- **Remote-backend URL masking.** When a remote backend is configured, tlumi masks passwords, bare-username tokens, and sensitive query parameters (access keys, SAS tokens, signed-URL signatures) when echoing the backend URL to the terminal.
- **Pulumi CLI auto-install.** On first run, tlumi downloads the Pulumi CLI binary via `PulumiCommand.install()` from the Pulumi SDK. Trust is delegated entirely to the SDK; tlumi does not perform additional checksum verification on the CLI binary.
- **uv auto-install.** tlumi uses [uv](https://github.com/astral-sh/uv) for fast dependency management. If `uv` is already on your PATH, tlumi uses it directly. Otherwise, it downloads uv from GitHub releases with SHA-256 checksum verification and caches it at `~/.tlumi/cache/uv/`. In restricted environments, pre-install uv on your PATH to avoid the download.

For vulnerability disclosure, see [SECURITY.md](https://github.com/jaydoubleu/tlumi/blob/main/SECURITY.md).

## Contributing

See [CONTRIBUTING.md](https://github.com/jaydoubleu/tlumi/blob/main/CONTRIBUTING.md) for development setup, testing, and the PR process.

## License

Apache 2.0

## Trademarks

Terraform is a trademark of HashiCorp (an IBM company). Pulumi is a trademark of Pulumi Corporation. tlumi is an independent project and is not affiliated with, sponsored by, or endorsed by either company.
