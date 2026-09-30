"""Zotero PDF dry-run mapping and validation tools."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("zotero-fulltext-mcp")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
