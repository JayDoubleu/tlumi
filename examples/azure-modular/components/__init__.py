"""Reusable infrastructure components:Python's answer to Terraform modules.

In Terraform you'd write:
    module "monitoring" {
        source = "./modules/monitoring"
        ...
    }

In tlumi, you just import a class:
    from components.monitoring import MonitoringStack
    monitoring = MonitoringStack("ops", ...)
"""

from components.monitoring import MonitoringStack
from components.storage import StorageBucket

__all__ = ["MonitoringStack", "StorageBucket"]
