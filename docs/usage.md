Usage
=====

The dependency cache is a **startup-only** optimization. The cache must be active
while FastAPI introspects your routes' dependency trees — that is, while routers
are being included / routes registered — and torn down afterwards.

There are three ways to drive it: the context manager, manual patch/unpatch, and
a caller-owned shared cache you can inspect afterwards.

Separately, an opt-in runtime patch cuts a per-request cost, see
[Skipping the sub-tree of cached dependencies](#skipping-the-sub-tree-of-cached-dependencies).

## Context manager (recommended)

`fastapi_deps_cache` enables caching for the duration of the block, then restores
FastAPI's originals and frees the cache memory on exit:

```python
from fastapi import APIRouter, FastAPI

from fastapi_cache_di import fastapi_deps_cache

app = FastAPI()
users = APIRouter()
orders = APIRouter()

with fastapi_deps_cache():
    app.include_router(users)
    app.include_router(orders)
# originals restored here; cache memory freed
```

The `deps_cache` keyword controls the backing store:

- `True` (default) — use a temporary cache scoped to this context.
- `False` — no-op; caching is disabled (handy behind a feature flag).
- a `DepsCache` instance — use (and populate) a shared cache that survives the
  block, see [Sharing and inspecting the cache](#sharing-and-inspecting-the-cache).

```python
# Disable caching without changing the call site.
with fastapi_deps_cache(deps_cache=False):
    app.include_router(users)
```

!!! note
    The context manager is safe to nest. `patch_fastapi_deps_cache` is a no-op
    when caching is already active, so an inner block never tears down the
    still-wanted outer cache — only the block that actually installed the patch
    unpatches on exit.

## Manual patch / unpatch

When your route loading is not expressible as a single block, patch **before**
any routes are loaded and unpatch in a `finally`:

```python
from fastapi import FastAPI

from fastapi_cache_di import patch_fastapi_deps_cache, unpatch_fastapi_deps_cache


def load_all_routes(app: FastAPI) -> None:
    """Register the application's routers (introspection happens here)."""


app = FastAPI()

patch_fastapi_deps_cache()  # call before any routes are loaded
try:
    load_all_routes(app)
finally:
    unpatch_fastapi_deps_cache()  # logs hit/miss stats, restores originals
```

`patch_fastapi_deps_cache()` returns `True` on the first call and `False` if
caching is already active. `unpatch_fastapi_deps_cache()` returns `True` if it
restored the originals and `False` if nothing was patched. On unpatch, a summary
of the hit/miss counters is logged at `INFO` level under the
`fastapi_cache_di.patch` logger.

## Sharing and inspecting the cache

Pass your own `DepsCache` to keep the memoized data and the hit/miss counters
after the context exits — a caller-owned cache is **not** cleared on unpatch, so
you can measure how effective the caching was:

```python
from fastapi import FastAPI

from fastapi_cache_di import DepsCache, fastapi_deps_cache


def load_all_routes(app: FastAPI) -> None:
    """Register the application's routers (introspection happens here)."""


app = FastAPI()
cache = DepsCache()

with fastapi_deps_cache(deps_cache=cache):
    load_all_routes(app)

# The cache survives the context, so you can inspect what was memoized
# and how effective it was (hit/miss counters live on the instance).
print(len(cache.dependants), len(cache.flat_dependants))
print(cache.flat_hits, cache.flat_misses)
```

`DepsCache` exposes three memoization stores and a hit/miss counter pair for
each:

| Store | Counters | Populated from |
|---|---|---|
| `signatures` | `sig_hits`, `sig_misses` | `get_typed_signature` |
| `dependants` | `dep_hits`, `dep_misses` | `get_dependant` |
| `flat_dependants` | `flat_hits`, `flat_misses` | `get_flat_dependant` (FastAPI ≤ 0.140; empty from 0.141) |

Call `cache.clear()` to drop all entries and reset every counter, so the same
instance can be reused for another loading pass.

## Thread-safety

!!! warning
    All patch state is **process-global** and patching swaps FastAPI module
    attributes for the whole process. It is **not re-entrant across threads**:
    call `patch`/`unpatch` (or the context manager) from a single thread, during
    startup only.

Because the counters live on the `DepsCache` instance rather than on module
globals, a caller-owned cache still exposes its statistics after `unpatch`.

Calling `patch_fastapi_deps_cache(deps_cache=...)` while a patch is already
active keeps the **existing** cache and logs a warning — the new cache is ignored
until you `unpatch` first.

## Skipping the sub-tree of cached dependencies

At **request time**, FastAPI solves the whole sub-tree of a dependency before
checking whether its value is already in the request's cache. The cache saves the
call, not the walk: a dependency shared across a deep tree (a DB handle, a
session service, repositories …) has its sub-tree solved again at every
occurrence, query, header and cookie parameters included, and the result is
thrown away. The cost grows with the number of occurrences in the tree, not with
the number of unique dependencies.

`patch_fastapi_solve_dependencies()` stops that walk as soon as the value is
cached; FastAPI then reuses the cached value as before. Unlike the dependency
cache, it is meant to stay active for the app's lifetime:

```python
from fastapi import FastAPI

from fastapi_cache_di import patch_fastapi_solve_dependencies

patch_fastapi_solve_dependencies()

app = FastAPI()
```

`unpatch_fastapi_solve_dependencies()` restores FastAPI's behavior. The two
patches are independent: either can be used without the other.

The values injected into endpoints are unchanged. The only visible difference: a
`use_cache=False` dependency nested under a cached dependency is no longer called
again during the skipped walk, whose result was discarded anyway.

!!! note
    While `dependency_overrides` is not empty, requests are solved exactly as
    without the patch: FastAPI then rebuilds every sub-dependency, which no longer
    carries the key the cached value is stored under.
