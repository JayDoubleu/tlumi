# AWS S3 Bucket

A minimal example that creates an S3 bucket with versioning enabled.

## Usage

```bash
tlumi init
tlumi plan
tlumi apply
```

Requires AWS credentials (`aws configure` or environment variables like `AWS_ACCESS_KEY_ID`).

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `environment` | `dev` | Environment tag value |
| `region` | `us-east-1` | AWS region the bucket is created in (wired to an explicit `aws.Provider`) |

## Clean up

S3 buckets bill for storage; remove everything when you are done:

```bash
tlumi destroy
```
