"""tlumi - Terraform-like workflow for Python infrastructure-as-code."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _meta_version

try:
    __version__ = _meta_version("tlumi")
except PackageNotFoundError:
    # Source import without an installed distribution (doc builds,
    # PYTHONPATH=src python, certain test harnesses).
    __version__ = "0.0.0+source"
