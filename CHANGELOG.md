# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-03

Initial public release.

### Added
- Terraform-like CLI over the Pulumi Automation API: `init`, `plan`, `apply`, `destroy`, `refresh`, `import`, `output`, `show`, `validate`, `fmt`, `clean`, and `version`.
- Local state by default in `.tlumi/state/`, with no Pulumi Cloud account and no `~/.pulumi` pollution. Optional remote backends (AWS S3, Azure Blob, GCS) via `backend.url` in `tlumi.yaml`; see [docs/backends.md](docs/backends.md).
- Opt-in secret encryption via `TLUMI_SECRETS_PASSPHRASE`. Without a passphrase, tlumi refuses to store secrets in plaintext unless `secrets.allow_unencrypted: true` is set.
- State management: `state list`, `state show`, `state rm`, `state mv`, `state pull`, `state push`, and `state unlock`, with a timestamped backup written before every state mutation.
- Dependency management: `deps add`, `deps install`, and `deps list`, using an auto-installed `uv` for the provider virtualenv.
- Variables via `--var`, `--var-file`, and `TLUMI_VAR_*`; operation flags `--target`, `--replace`, `--destroy`, `--out`, `--plan`, `--show-secrets`, `--auto-approve`, `--json`, and `--verbose`.
- Property-level diffs in `plan` and `apply` previews, rendered from Pulumi engine events.
- Four runnable examples: `random` (no credentials), `aws-s3`, `azure-container-apps`, and `azure-modular`.
- Architecture Decision Records in `DESIGN.md`, plus `CONTRIBUTING.md` and `SECURITY.md`.
