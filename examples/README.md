# tlumi Examples

| Example | Description | Cloud Provider |
|---------|-------------|----------------|
| [random](random/) | Random IDs, passwords, and pet names (no cloud credentials needed) | None |
| [aws-s3](aws-s3/) | S3 bucket with tags and versioning | AWS |
| [azure-container-apps](azure-container-apps/) | Docker build + ACR + Container App in a single deploy | Azure |
| [azure-modular](azure-modular/) | Reusable `ComponentResource` components (Python modules) | Azure |

## Getting Started

Pick an example, `cd` into its directory, and run:

```bash
tlumi init
tlumi plan
```

`tlumi init` already installs the example's `requirements.txt` for you; there's no need to run `tlumi deps install` separately on a fresh clone.

The `random` example needs no cloud credentials and is a good place to start.
