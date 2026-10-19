"""Speed up FastAPI startup by caching dependency-tree introspection."""

from fastapi_cache_di.cache import DepsCache
from fastapi_cache_di.execution import is_loop_safe, loop_safe
from fastapi_cache_di.patch import (
    fastapi_deps_cache,
    patch_fastapi_deps_cache,
    patch_fastapi_resolution_plan,
    patch_fastapi_run_in_threadpool,
    patch_fastapi_solve_dependencies,
    unpatch_fastapi_deps_cache,
    unpatch_fastapi_resolution_plan,
    unpatch_fastapi_run_in_threadpool,
    unpatch_fastapi_solve_dependencies,
)

__version__ = "0.3.0"

__all__ = [
    "DepsCache",
    "__version__",
    "fastapi_deps_cache",
    "is_loop_safe",
    "loop_safe",
    "patch_fastapi_deps_cache",
    "patch_fastapi_resolution_plan",
    "patch_fastapi_run_in_threadpool",
    "patch_fastapi_solve_dependencies",
    "unpatch_fastapi_deps_cache",
    "unpatch_fastapi_resolution_plan",
    "unpatch_fastapi_run_in_threadpool",
    "unpatch_fastapi_solve_dependencies",
]
