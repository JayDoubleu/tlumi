# Random Provider Example

A "hello world" example that needs no cloud credentials. Uses the `pulumi-random` provider to create random IDs, passwords, and pet names.

## Usage

```bash
tlumi init
tlumi plan
tlumi apply
```

No cloud credentials required.

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `pet_count` | `3` | Number of random pets to create |

## What It Shows

- Reading variables from `tlumi.yaml` via `pulumi.Config()`
- Creating secret values with `RandomPassword` and `pulumi.Output.secret()`
- Using Python loops to create multiple resources
- Exporting values with `pulumi.export()`
