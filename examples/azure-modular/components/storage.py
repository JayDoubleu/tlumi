"""Storage bucket component:Storage Account + Blob Container.

Terraform equivalent: a module that creates an azurerm_storage_account
and azurerm_storage_container with consistent naming and policies.

The Python advantage: you can use conditionals, loops, and real logic
in the constructor; no count/for_each hacks needed.
"""

import pulumi
from pulumi_azure_native import storage


class StorageBucket(pulumi.ComponentResource):
    """Managed storage bucket with consistent configuration.

    Creates a storage account and one or more blob containers with
    enforced security defaults (TLS 1.2, HTTPS-only, no public access).

    Terraform module equivalent:
        module "uploads" {
            source              = "./modules/storage"
            resource_group_name = azurerm_resource_group.rg.name
            location            = var.location
            containers          = ["images", "documents"]
            access_tier         = "Hot"
            tags                = local.common_tags
        }

    tlumi:
        uploads = StorageBucket("uploads",
            resource_group_name=rg.name,
            location=location,
            containers=["images", "documents"],
            access_tier="Hot",
            tags=common_tags,
        )
    """

    account_name: pulumi.Output[str]
    primary_endpoint: pulumi.Output[str]
    container_names: list[pulumi.Output[str]]

    def __init__(
        self,
        name: str,
        *,
        resource_group_name: pulumi.Input[str],
        location: pulumi.Input[str],
        containers: list[str] | None = None,
        access_tier: str = "Hot",
        replication: str = "LRS",
        tags: dict[str, str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("custom:storage:StorageBucket", name, {}, opts)

        child_opts = pulumi.ResourceOptions(parent=self)
        component_tags = {**(tags or {}), "component": f"storage-{name}"}

        # Map friendly names to SDK enums
        sku_map = {
            "LRS": storage.SkuName.STANDARD_LRS,
            "GRS": storage.SkuName.STANDARD_GRS,
            "ZRS": storage.SkuName.STANDARD_ZRS,
            "RAGRS": storage.SkuName.STANDARD_RAGRS,
        }
        tier_map = {
            "Hot": storage.AccessTier.HOT,
            "Cool": storage.AccessTier.COOL,
        }

        account = storage.StorageAccount(
            f"{name}account",
            resource_group_name=resource_group_name,
            location=location,
            kind=storage.Kind.STORAGE_V2,
            sku=storage.SkuArgs(name=sku_map.get(replication, storage.SkuName.STANDARD_LRS)),
            access_tier=tier_map.get(access_tier, storage.AccessTier.HOT),
            enable_https_traffic_only=True,
            minimum_tls_version=storage.MinimumTlsVersion.TLS1_2,
            allow_blob_public_access=False,
            tags=component_tags,
            opts=child_opts,
        )

        # Create containers; in Terraform you'd need for_each or count.
        # In Python, it's just a loop.
        self.container_names = []
        for container_name in (containers or ["default"]):
            container = storage.BlobContainer(
                f"{name}-{container_name}",
                resource_group_name=resource_group_name,
                account_name=account.name,
                container_name=container_name,
                public_access=storage.PublicAccess.NONE,
                opts=child_opts,
            )
            self.container_names.append(container.name)

        # Expose outputs
        self.account_name = account.name
        self.primary_endpoint = account.primary_endpoints.apply(lambda e: e.blob)

        self.register_outputs({
            "account_name": self.account_name,
            "primary_endpoint": self.primary_endpoint,
        })
