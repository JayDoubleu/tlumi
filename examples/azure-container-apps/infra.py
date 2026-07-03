"""Azure Container Apps: deployed from a single `tlumi apply`.

This example shows what's impossible in Terraform without external scripts:
  1. Create an Azure Container Registry
  2. Build a Docker image and push it to that registry
  3. Deploy a Container App using that image
  4. Set up monitoring with a Log Analytics workspace (wired into the Container Apps environment)
  5. Add a Storage Account for persistent data

In Terraform, step 2 requires a separate `docker build && docker push`
between `terraform apply` runs, or hacky null_resource/local-exec provisioners.

With tlumi (Python + Pulumi), it's all one atomic deployment.
"""

import pulumi
import pulumi_docker_build as docker_build
from pulumi_azure_native import app, containerregistry, operationalinsights, resources, storage

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

config = pulumi.Config()
location = config.get("location") or "westeurope"
environment = config.get("environment") or "staging"
app_name = "myapp"
team = "platform"

common_tags = {
    "environment": environment,
    "managed-by": "tlumi",
    "team": team,
    "cost-centre": "eng-42",
}

# ---------------------------------------------------------------------------
# Resource Group
# ---------------------------------------------------------------------------

resource_group = resources.ResourceGroup(
    "rg",
    resource_group_name=f"rg-{app_name}-{environment}",
    location=location,
    tags=common_tags,
)

# ---------------------------------------------------------------------------
# Storage Account (for application data)
# ---------------------------------------------------------------------------

storage_account = storage.StorageAccount(
    "storage",
    resource_group_name=resource_group.name,
    location=location,
    kind=storage.Kind.STORAGE_V2,
    sku=storage.SkuArgs(name=storage.SkuName.STANDARD_LRS),
    enable_https_traffic_only=True,
    minimum_tls_version=storage.MinimumTlsVersion.TLS1_2,
    tags=common_tags,
)

blob_container = storage.BlobContainer(
    "app-data",
    resource_group_name=resource_group.name,
    account_name=storage_account.name,
    container_name="app-data",
    public_access=storage.PublicAccess.NONE,
)

# ---------------------------------------------------------------------------
# Container Registry (with admin credentials for simplicity)
# ---------------------------------------------------------------------------

registry = containerregistry.Registry(
    "acr",
    resource_group_name=resource_group.name,
    location=location,
    sku=containerregistry.SkuArgs(name="Standard"),
    admin_user_enabled=True,
    tags={**common_tags, "component": "registry"},
)

# Fetch admin credentials: this is where Python shines.
# In Terraform you'd need a separate data source + depends_on gymnastics.
credentials = pulumi.Output.all(resource_group.name, registry.name).apply(
    lambda args: containerregistry.list_registry_credentials(
        resource_group_name=args[0],
        registry_name=args[1],
    )
)

admin_username = credentials.apply(lambda c: c.username)
admin_password = credentials.apply(lambda c: c.passwords[0].value)

# ---------------------------------------------------------------------------
# Build & Push Docker Image
#
# This is the step that makes Terraform users cry. We build the Docker image
# and push it to our newly-created ACR, all within the same deployment.
# No shell scripts, no CI/CD pipelines, no separate steps.
# ---------------------------------------------------------------------------

image = docker_build.Image(
    "app-image",
    tags=[
        registry.login_server.apply(lambda server: f"{server}/{app_name}:latest"),
        registry.login_server.apply(lambda server: f"{server}/{app_name}:v2"),
    ],
    context=docker_build.BuildContextArgs(
        location="./app",
    ),
    dockerfile=docker_build.DockerfileArgs(
        location="./app/Dockerfile",
    ),
    platforms=[
        docker_build.Platform.LINUX_AMD64,
        docker_build.Platform.LINUX_ARM64,
    ],
    push=True,
    registries=[
        docker_build.RegistryArgs(
            address=registry.login_server,
            username=admin_username,
            password=admin_password,
        )
    ],
)

# ---------------------------------------------------------------------------
# Log Analytics Workspace (required by Container Apps Environment)
# ---------------------------------------------------------------------------

log_retention_days = int(config.get("log_retention_days") or "60")

log_workspace = operationalinsights.Workspace(
    "logs",
    resource_group_name=resource_group.name,
    location=location,
    sku=operationalinsights.WorkspaceSkuArgs(name="PerGB2018"),
    retention_in_days=log_retention_days,
    tags={**common_tags, "component": "monitoring"},
)

log_shared_keys = pulumi.Output.all(resource_group.name, log_workspace.name).apply(
    lambda args: operationalinsights.get_shared_keys(
        resource_group_name=args[0],
        workspace_name=args[1],
    )
)

# ---------------------------------------------------------------------------
# Container Apps Environment
# ---------------------------------------------------------------------------

container_env = app.ManagedEnvironment(
    "env",
    resource_group_name=resource_group.name,
    location=location,
    zone_redundant=False,
    app_logs_configuration=app.AppLogsConfigurationArgs(
        destination="log-analytics",
        log_analytics_configuration=app.LogAnalyticsConfigurationArgs(
            customer_id=log_workspace.customer_id,
            shared_key=log_shared_keys.apply(lambda keys: keys.primary_shared_key),
        ),
    ),
    tags={**common_tags, "component": "container-env"},
)

# ---------------------------------------------------------------------------
# Container App
#
# References the image we just built and pushed. The ACR credentials are
# passed as secrets. Pulumi wires this all together automatically via
# its dependency graph. No manual ordering needed.
# ---------------------------------------------------------------------------

app_cpu = float(config.get("app_cpu") or "0.5")
app_memory = config.get("app_memory") or "1.0Gi"
max_replicas = int(config.get("max_replicas") or "5")

container_app = app.ContainerApp(
    "app",
    resource_group_name=resource_group.name,
    managed_environment_id=container_env.id,
    configuration=app.ConfigurationArgs(
        ingress=app.IngressArgs(
            external=True,
            target_port=8000,
            transport=app.IngressTransportMethod.HTTP,
            traffic=[
                app.TrafficWeightArgs(
                    latest_revision=True,
                    weight=100,
                ),
            ],
        ),
        registries=[
            app.RegistryCredentialsArgs(
                server=registry.login_server,
                username=admin_username,
                password_secret_ref="acr-password",
            )
        ],
        secrets=[
            app.SecretArgs(
                name="acr-password",
                value=admin_password,
            ),
            app.SecretArgs(
                name="storage-connection",
                value=pulumi.Output.all(
                    resource_group.name, storage_account.name
                ).apply(
                    lambda args: storage.list_storage_account_keys(
                        resource_group_name=args[0],
                        account_name=args[1],
                    )
                ).apply(
                    lambda keys: keys.keys[0].value
                ),
            ),
        ],
    ),
    template=app.TemplateArgs(
        containers=[
            app.ContainerArgs(
                name=app_name,
                image=image.ref,
                resources=app.ContainerResourcesArgs(
                    cpu=app_cpu,
                    memory=app_memory,
                ),
                env=[
                    app.EnvironmentVarArgs(
                        name="APP_ENV",
                        value=environment,
                    ),
                    app.EnvironmentVarArgs(
                        name="STORAGE_CONNECTION",
                        secret_ref="storage-connection",
                    ),
                ],
                probes=[
                    app.ContainerAppProbeArgs(
                        type=app.Type.LIVENESS,
                        http_get=app.ContainerAppProbeHttpGetArgs(
                            path="/health",
                            port=8000,
                        ),
                        initial_delay_seconds=10,
                        period_seconds=30,
                    ),
                ],
            )
        ],
        scale=app.ScaleArgs(
            min_replicas=1,
            max_replicas=max_replicas,
            rules=[
                app.ScaleRuleArgs(
                    name="http-rule",
                    http=app.HttpScaleRuleArgs(
                        metadata={"concurrentRequests": "100"},
                    ),
                ),
                app.ScaleRuleArgs(
                    name="cpu-rule",
                    custom=app.CustomScaleRuleArgs(
                        type="cpu",
                        metadata={"type": "Utilization", "value": "70"},
                    ),
                ),
            ],
        ),
    ),
    tags={**common_tags, "component": "app"},
)

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

pulumi.export("resource_group", resource_group.name)
pulumi.export("registry_server", registry.login_server)
pulumi.export("image_ref", image.ref)
pulumi.export("storage_account", storage_account.name)
pulumi.export("blob_container", blob_container.name)
pulumi.export(
    "app_url",
    container_app.configuration.apply(
        lambda c: f"https://{c.ingress.fqdn}" if c and c.ingress else "N/A"
    ),
)
