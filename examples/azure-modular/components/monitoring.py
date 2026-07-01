"""Monitoring stack component:Log Analytics + Application Insights.

Terraform equivalent: a module with log_analytics.tf + app_insights.tf
that you'd call with `module "monitoring" { source = "./modules/monitoring" }`.

Here it's just a Python class. You get type checking, IDE autocomplete,
and can use any Python logic in the constructor.
"""

import pulumi
from pulumi_azure_native import insights, operationalinsights


class MonitoringStack(pulumi.ComponentResource):
    """Bundled monitoring infrastructure.

    Creates a Log Analytics workspace and Application Insights instance
    wired together. Exposes workspace_id and instrumentation_key as
    outputs for other components to consume.

    Terraform module equivalent:
        module "monitoring" {
            source              = "./modules/monitoring"
            resource_group_name = azurerm_resource_group.rg.name
            location            = var.location
            retention_days      = 30
            tags                = local.common_tags
        }

    tlumi:
        monitoring = MonitoringStack("ops",
            resource_group_name=rg.name,
            location=location,
            retention_days=30,
            tags=common_tags,
        )
    """

    workspace_id: pulumi.Output[str]
    workspace_name: pulumi.Output[str]
    shared_key: pulumi.Output[str]
    instrumentation_key: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        resource_group_name: pulumi.Input[str],
        location: pulumi.Input[str],
        retention_days: int = 30,
        tags: dict[str, str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("custom:monitoring:MonitoringStack", name, {}, opts)

        child_opts = pulumi.ResourceOptions(parent=self)
        component_tags = {**(tags or {}), "component": "monitoring"}

        workspace = operationalinsights.Workspace(
            f"{name}-logs",
            resource_group_name=resource_group_name,
            location=location,
            sku=operationalinsights.WorkspaceSkuArgs(name="PerGB2018"),
            retention_in_days=retention_days,
            tags=component_tags,
            opts=child_opts,
        )

        app_insights = insights.Component(
            f"{name}-insights",
            resource_group_name=resource_group_name,
            location=location,
            kind="web",
            application_type=insights.ApplicationType.WEB,
            workspace_resource_id=workspace.id,
            tags=component_tags,
            opts=child_opts,
        )

        shared_keys = pulumi.Output.all(resource_group_name, workspace.name).apply(
            lambda args: operationalinsights.get_shared_keys(
                resource_group_name=args[0],
                workspace_name=args[1],
            )
        )

        # Expose outputs, like Terraform module outputs
        self.workspace_id = workspace.id
        self.workspace_name = workspace.name
        self.shared_key = shared_keys.apply(lambda k: k.primary_shared_key)
        self.instrumentation_key = app_insights.instrumentation_key

        self.register_outputs({
            "workspace_id": self.workspace_id,
            "instrumentation_key": self.instrumentation_key,
        })
