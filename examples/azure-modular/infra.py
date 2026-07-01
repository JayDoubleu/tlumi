"""Azure Modular Infrastructure: reusable components in action.

This example demonstrates how Python replaces Terraform modules.
Instead of HCL module blocks, you import Python classes.
Instead of variable/output blocks, you use constructor args and attributes.
Instead of count/for_each, you use loops and conditionals.

Terraform equivalent would require:
    modules/monitoring/main.tf + variables.tf + outputs.tf
    modules/storage/main.tf + variables.tf + outputs.tf
    main.tf with 4 module blocks

Here it's just Python imports and class instantiation.
"""

import pulumi

# Local imports - these ARE the "modules"
from components import MonitoringStack, StorageBucket
from pulumi_azure_native import resources

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

config = pulumi.Config()
location = config.get("location") or "westeurope"
environment = config.get("environment") or "staging"

common_tags = {
    "environment": environment,
    "managed-by": "tlumi",
    "project": "modular-example",
}

# ---------------------------------------------------------------------------
# Shared Resource Group
# ---------------------------------------------------------------------------

rg = resources.ResourceGroup(
    "rg",
    resource_group_name=f"rg-modular-{environment}",
    location=location,
    tags=common_tags,
)

# ---------------------------------------------------------------------------
# Monitoring - one component, replaces a whole Terraform module directory
# ---------------------------------------------------------------------------

monitoring = MonitoringStack(
    "ops",
    resource_group_name=rg.name,
    location=location,
    retention_days=30,
    tags=common_tags,
)

# ---------------------------------------------------------------------------
# Storage - same component, three different configurations
#
# In Terraform this would be three module blocks pointing at the same source,
# each with different variables. Here it's just three constructor calls.
# ---------------------------------------------------------------------------

# Application uploads - hot storage, multiple containers
uploads = StorageBucket(
    "uploads",
    resource_group_name=rg.name,
    location=location,
    containers=["images", "documents", "avatars"],
    access_tier="Hot",
    tags=common_tags,
)

# Backups - cool storage for cost savings, geo-redundant
backups = StorageBucket(
    "backups",
    resource_group_name=rg.name,
    location=location,
    containers=["db-snapshots", "config-backups"],
    access_tier="Cool",
    replication="GRS",
    tags=common_tags,
)

# Logs - hot storage, single container
logs = StorageBucket(
    "logs",
    resource_group_name=rg.name,
    location=location,
    containers=["app-logs"],
    access_tier="Hot",
    tags=common_tags,
)

# ---------------------------------------------------------------------------
# Conditional logic - something Terraform can't do cleanly
# ---------------------------------------------------------------------------

# Only create a premium storage bucket in production
if environment == "production":
    premium = StorageBucket(
        "premium",
        resource_group_name=rg.name,
        location=location,
        containers=["critical-data"],
        replication="RAGRS",
        tags={**common_tags, "tier": "premium"},
    )

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

pulumi.export("resource_group", rg.name)
pulumi.export("workspace_id", monitoring.workspace_id)
pulumi.export("instrumentation_key", monitoring.instrumentation_key)
pulumi.export("uploads_endpoint", uploads.primary_endpoint)
pulumi.export("backups_endpoint", backups.primary_endpoint)
pulumi.export("logs_endpoint", logs.primary_endpoint)
