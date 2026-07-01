# Azure Container Apps Example

This example deploys a FastAPI application to Azure Container Apps, including building and pushing the Docker image, all from a single `tlumi apply`.

## Why This Matters

In Terraform, deploying a container app requires **three separate steps**:

```
terraform apply          # 1. Create ACR
docker build && push     # 2. Build and push image (OUTSIDE Terraform)
terraform apply          # 3. Create Container App referencing the image
```

With tlumi, it's **one step**:

```
tlumi apply              # Creates ACR, builds+pushes image, deploys app
```

This is possible because Python can orchestrate the Docker build as part of the same deployment graph.

## What Gets Created

- **Resource Group**: contains everything
- **Storage Account**: StorageV2 account for application data; its access key is passed to the app as the `storage-connection` secret
- **Blob Container**: `app-data` container with public access disabled
- **Azure Container Registry**: stores the Docker image
- **Docker Image**: built from `./app/` and pushed to ACR
- **Log Analytics Workspace**: required by Container Apps
- **Container Apps Environment**: the hosting environment
- **Container App**: runs the FastAPI app with HTTP ingress, scales from 1 to `max_replicas` replicas (default 5) via HTTP and CPU rules

## Prerequisites

- Azure CLI authenticated (`az login`)
- Docker running locally (for image build)
- Python 3.10+

## Usage

```bash
cd examples/azure-container-apps

# Initialize the project (sets up venv and installs providers)
tlumi init

# Preview what will be created
tlumi plan

# Deploy everything
tlumi apply

# Get the app URL
tlumi output app_url

# Tear it all down
tlumi destroy
```

## Configuration

Edit `tlumi.yaml` to change:

```yaml
variables:
  location: westeurope      # Azure region
  environment: staging      # Tag value, resource-group name, APP_ENV
  log_retention_days: 60    # Log Analytics retention
  app_cpu: "0.5"            # Container CPU cores (quote decimals; tlumi rejects unquoted floats)
  app_memory: 1.0Gi         # Container memory
  max_replicas: 5           # Container App scale ceiling (min is 1)
```

Or override at deploy time:

```bash
tlumi apply --var location=uksouth --var environment=prod
```

## Project Structure

```
azure-container-apps/
├── tlumi.yaml            # Project config
├── infra.py              # Infrastructure definitions
├── requirements.txt      # Pulumi providers (azure-native, docker-build)
└── app/                  # Application to deploy
    ├── Dockerfile
    ├── main.py           # FastAPI app
    └── requirements.txt  # App dependencies
```
