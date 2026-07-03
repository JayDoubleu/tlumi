# Azure Modular Infrastructure

Demonstrates reusable components using Pulumi's `ComponentResource` class, which is Python's answer to Terraform modules.

## Structure

```
components/
  monitoring.py   # MonitoringStack: Log Analytics + Application Insights
  storage.py      # StorageBucket: Storage Account + N blob containers
infra.py          # Main program that imports and uses components
tlumi.yaml        # Project configuration
```

## What It Shows

- **Reusable components** via `ComponentResource` (replaces Terraform modules)
- **Multiple instances** of the same component with different configurations
- **Conditional logic** that creates resources only in specific environments
- **Python imports** instead of `module {}` blocks

## Usage

```bash
tlumi init           # creates .tlumi/, installs deps from requirements.txt
tlumi plan
tlumi apply
```

Requires Azure credentials (`az login` or service principal environment variables).

## Clean up

The resource group and everything in it bill while they exist; remove them when you are done:

```bash
tlumi destroy
```
