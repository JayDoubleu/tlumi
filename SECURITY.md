# Security Policy

## Supported versions

tlumi is pre-1.0. Security fixes are made against `main` and shipped in the next release. There is no LTS branch.

## Reporting a vulnerability

Please do not file a public GitHub issue for security-sensitive reports. Instead, use [GitHub private vulnerability reporting](https://github.com/jaydoubleu/tlumi/security/advisories/new): open the repository's **Security** tab and click **Report a vulnerability**. If you cannot use GitHub, email the maintainer at git.jaydoubleu@gmail.com. Include:

- A description of the vulnerability
- A reproduction (minimal `tlumi.yaml` + `infra.py` + command sequence)
- Your assessment of impact
- Any suggested remediation

If you do not receive an acknowledgement within 7 days, please follow up. Coordinated disclosure is welcome; we will agree on a disclosure timeline once the issue is confirmed.

## Threat model

tlumi runs locally and operates on resources you have credentials for. The trust boundary is your local machine. Specifically:

- **`infra.py` is executed Python.** Only run `tlumi plan/apply/destroy/refresh/import` in directories you trust. The entry file is loaded via `importlib.util.spec_from_file_location` against an exact path (no `sys.path` search), but it still executes as Python with your privileges.
- **State files contain plaintext secrets unless you opt into encryption.** Set `TLUMI_SECRETS_PASSPHRASE` to enable encryption; otherwise tlumi refuses to proceed unless `secrets.allow_unencrypted: true` is set in `tlumi.yaml`. The `.tlumi/` directory is created with `0700` permissions to limit local exposure.
- **Auto-installed binaries.** tlumi downloads the Pulumi CLI (via `PulumiCommand.install()`, trust delegated to the Pulumi SDK) into `~/.tlumi/cache/pulumi/<sdk_version>/`, falling back to the per-project `.tlumi/cache/pulumi_home/`; a `pulumi` binary on `PATH` is never used. uv is resolved from `PATH` first; if absent, tlumi downloads it from GitHub releases (verified against a SHA-256 checksum) into `~/.tlumi/cache/uv/`. In restricted environments, pre-install uv on `PATH` and pre-seed the Pulumi CLI at `~/.tlumi/cache/pulumi/<sdk_version>/bin/pulumi` (matching the installed Pulumi SDK version) to avoid both downloads.
- **Remote backend URL credentials.** tlumi masks passwords, bare-username tokens, and sensitive query parameters before echoing backend URLs to the terminal. Real credentials still travel to the backend; the masking only protects the display path.
- **State backups.** State mutations create timestamped backups under `.tlumi/backups/` with `0600` perms via `O_CREAT|O_EXCL`. Symlink checks precede every mkdir and file write on user-controlled paths.
- **SDK monkey-patch.** `sdk_compat.py` patches private Pulumi SDK functions to suppress local filesystem paths from persisting in state. This is documented in [DESIGN.md](DESIGN.md) ADR-006. The patch fails open with a warning if the SDK layout changes.

## Out of scope

- Vulnerabilities in dependencies (Pulumi SDK, uv, Rich, Typer) should be reported to those projects. tlumi will track upstream advisories and bump.
- Issues that require physical access to the machine running tlumi.
- Social engineering of users into running malicious `infra.py` files (the trust model assumes the user trusts the project directory).

## Hardening recommendations for users

- Never commit `.tlumi/state/` to a public repository. The auto-generated `.gitignore` excludes it; verify before pushing.
- Use a remote backend with locking (S3, Azure Blob) for team and CI use. The local file backend has no distributed locking.
- Set `TLUMI_SECRETS_PASSPHRASE` for any project that uses Pulumi secret values, even in development.
- **Project variables are NOT secrets.** Values from `--var`, `--var-file`, `TLUMI_VAR_*`, and `tlumi.yaml` are written as **plaintext** Pulumi config (stored unencrypted in `.tlumi/Pulumi.<stack>.yaml`, visible in the `pulumi config set` process arguments) regardless of `TLUMI_SECRETS_PASSPHRASE`. Do not put real secrets in any variable channel. For values that must be encrypted at rest, create them as Pulumi secrets inside `infra.py` (e.g. `config.require_secret(...)` or `pulumi.Output.secret(...)`); those are encrypted with the passphrase. Among the variable channels, prefer `--var-file` (gitignored) or `TLUMI_VAR_*` over `--var KEY=VALUE`, which additionally lands in shell history.
