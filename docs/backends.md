# State Backends and Authentication

tlumi stores Pulumi state in a backend. By default that is the local filesystem
(`.tlumi/state/`). For team use or durability you can point it at cloud object
storage by setting `backend.url` in `tlumi.yaml`:

```yaml
backend:
  url: s3://my-tlumi-state-bucket?region=eu-west-1
```

This page documents how tlumi authenticates to each remote backend. The short
version: **you are not limited to static keys or SAS tokens.** Azure CLI login,
managed identity, AWS instance roles, AWS SSO, and GCP workload identity all
work, because tlumi delegates backend authentication to Pulumi's self-managed
(DIY) backend, which uses each cloud SDK's standard default credential chain.

## How tlumi resolves backend credentials

There are three things to understand:

1. **tlumi forwards your shell environment to the engine.** When tlumi runs
   Pulumi it overrides only the `PULUMI_*` variables it manages and blanks its
   own `TLUMI_SECRETS_PASSPHRASE` and `TLUMI_VAR_*` variables. Every other
   environment variable, including `AWS_*`, `AZURE_*`, `GOOGLE_*`, and `ARM_*`,
   is inherited by Pulumi and its provider plugins. This is how cloud
   credentials reach the backend.

2. **Credentials live in the environment, not in `tlumi.yaml`.** `backend.url`
   names *where* the state lives. *Who* you are is resolved from your ambient
   cloud credentials (environment variables, CLI login sessions, attached
   identities). Do not put secrets in `tlumi.yaml`.

3. **`backend.url` has no scheme allowlist.** tlumi passes the URL through to
   Pulumi verbatim, so `file://`, `s3://`, `azblob://`, `gs://`, and any other
   Pulumi-supported scheme are all accepted.

`TLUMI_SECRETS_PASSPHRASE` (translated to `PULUMI_CONFIG_PASSPHRASE`) is a
separate concern: it encrypts secret *values inside* the state. It does not
authenticate to the blob store. See [Secrets](#secrets).

> You must create the bucket or container yourself first. The state backend
> does not provision its own storage.

## Local (default)

No configuration needed. State is written to `.tlumi/state/` and protected by a
best-effort per-stack lock. This is single-machine only. For team use, move to a
remote backend that supports locking (S3, Azure Blob, GCS).

## AWS S3

```yaml
backend:
  url: s3://my-tlumi-state-bucket?region=eu-west-1
```

Authentication uses the standard AWS SDK default credential provider chain (the
same chain the AWS CLI uses). All of these work:

| Method | How |
|---|---|
| Static keys | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (`AWS_SESSION_TOKEN` for temporary credentials) |
| Shared profile | `AWS_PROFILE` plus `~/.aws/credentials` and `~/.aws/config` |
| EC2 instance profile | Picked up automatically via IMDS, no keys needed |
| ECS task role | Picked up automatically via the container credentials endpoint |
| EKS IRSA / web identity | Picked up automatically from the pod's injected web-identity token |
| AWS SSO | `aws sso login` plus an SSO profile (see caveat below) |

**Region** must be resolvable. Set `AWS_REGION` or add `?region=<region>` to the
URL, otherwise you get a "Missing region configuration" error.

**SSO caveat.** Older Pulumi builds default the underlying driver to AWS SDK v1,
which does not read `[sso-session]` profiles. If SSO login fails against the
backend, add `?awssdk=v2` to the URL (optionally with `&profile=<name>`). Newer
builds default to v2 and need no flag.

**S3-compatible storage** (MinIO, Ceph, SeaweedFS) is configured with URL query
parameters, not an `AWS_ENDPOINT_URL` variable:

```yaml
backend:
  url: s3://my-bucket?endpoint=minio.local:9000&disableSSL=true&s3ForcePathStyle=true
```

## Azure Blob Storage

```yaml
backend:
  url: azblob://my-container?storage_account=mystorageacct
```

The `?storage_account=` query parameter requires Pulumi CLI 3.41.1 or newer. On
older versions, set `AZURE_STORAGE_ACCOUNT` in the environment instead. Either
way, the storage account must be named under every authentication mode.

Credentials are selected by **precedence**. The driver picks the first that is
present:

1. Account key: `AZURE_STORAGE_ACCOUNT` + `AZURE_STORAGE_KEY`
2. Connection string: `AZURE_STORAGE_CONNECTION_STRING`
3. SAS token: `AZURE_STORAGE_SAS_TOKEN`
4. If none of the above is set, the driver falls back to Azure AD / Entra auth
   via `DefaultAzureCredential`.

That fallback is the keyless path. With no static credential set,
`DefaultAzureCredential` walks its own chain and covers:

| Method | How |
|---|---|
| Azure CLI session | A prior `az login` is used automatically |
| Managed identity | System-assigned works as-is; for user-assigned, set `AZURE_CLIENT_ID` to the identity's client ID |
| Workload identity (AKS) | The `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_FEDERATED_TOKEN_FILE` variables are normally injected into the pod automatically |
| Service principal | `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_CLIENT_SECRET` |

**RBAC.** AD-based auth needs the data-plane role **Storage Blob Data
Contributor** on the account or container. Control-plane roles such as Owner or
Contributor are not sufficient.

**Gotchas worth knowing:**

- There is **no `AZURE_STORAGE_AUTH_MODE=login` switch.** That variable is an
  Azure CLI / azcopy concept and is never read here. You enable Entra auth by
  the *absence* of static credentials, not by a flag.
- A stray `AZURE_STORAGE_KEY` (or connection string or SAS token) in your
  environment **silently wins** over your intended identity auth, because it
  sits higher in the precedence order.
- The Pulumi Azure *provider* variables `ARM_CLIENT_ID` / `ARM_TENANT_ID` /
  `ARM_USE_OIDC` are **not** honored by the backend. Only the Azure SDK for Go
  `AZURE_*` variables are.

## Google Cloud Storage

```yaml
backend:
  url: gs://my-tlumi-state-bucket
```

Authentication uses the standard Google Application Default Credentials (ADC)
chain. GCS has no SAS-token or shared-key concept; auth is identity-based.

| Method | How |
|---|---|
| Service account key file | `GOOGLE_APPLICATION_CREDENTIALS` pointing at a JSON key |
| CLI session | `gcloud auth application-default login`, no key file needed |
| Workload / attached identity | The service account returned by the GCE / GKE / Cloud Run metadata server, no key file needed |
| Inline / token | `GOOGLE_CREDENTIALS` (inline JSON) or `GOOGLE_OAUTH_ACCESS_TOKEN` (short-lived token) |

No project variable (`GOOGLE_CLOUD_PROJECT` and similar) is required to
authenticate to the state backend; the bucket name in the URL fully identifies
the target.

## Secrets

Backend authentication and state-secret encryption are independent:

- **Backend auth** decides whether tlumi can read and write the state object. It
  is resolved from your cloud credentials as described above.
- **State-secret encryption** protects sensitive *values* stored inside the
  state. Set `TLUMI_SECRETS_PASSPHRASE` to enable it; tlumi translates it to
  `PULUMI_CONFIG_PASSPHRASE`. Without a passphrase, tlumi refuses to store
  secrets unless you opt into plaintext with `secrets.allow_unencrypted: true`
  in `tlumi.yaml`.

## URL masking

When a non-local backend is configured, tlumi masks credentials embedded in the
backend URL (passwords, bare-username tokens, SAS tokens, signed-URL
signatures) before echoing the URL to the terminal. This is display-only and
does not affect authentication.
