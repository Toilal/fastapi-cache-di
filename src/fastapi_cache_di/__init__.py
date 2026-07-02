"""Speed up FastAPI startup by caching dependency-tree introspection."""

from fastapi_cache_di.cache import DepsCache
from fastapi_cache_di.patch import (
    fastapi_deps_cache,
    patch_fastapi_deps_cache,
    unpatch_fastapi_deps_cache,
)

__version__ = "0.1.0"

__all__ = [
    "DepsCache",
    "__version__",
    "fastapi_deps_cache",
    "patch_fastapi_deps_cache",
    "unpatch_fastapi_deps_cache",
]
