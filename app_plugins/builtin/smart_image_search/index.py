"""Legacy index import compatibility; storage paths and formats stay unchanged."""
from model.image_retrieval.index import (
    ImageSearchIndex, INDEX_ROOT, THUMBNAIL_VERSION, discover_external_groups,
)

__all__ = ["ImageSearchIndex", "INDEX_ROOT", "THUMBNAIL_VERSION", "discover_external_groups"]
