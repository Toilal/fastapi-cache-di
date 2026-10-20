Usage
=====

The dependency cache is a **startup-only** optimization. The cache must be active
while FastAPI introspects your routes' dependency trees — that is, while routers
are being included / routes registered — and torn down afterwards.

There are three ways to drive it: the context manager, manual patch/unpatch, and
a caller-owned shared cache you can inspect afterwards.

Separately, three opt-in runtime patches cut per-request costs, see
[Calling loop-safe dependencies inline](#calling-loop-safe-dependencies-inline),
[Skipping the sub-tree of cached dependencies](#skipping-the-sub-tree-of-cached-dependencies)
and
[Solving routes from a precompiled plan](#solving-routes-from-a-precompiled-plan).

## Context manager (recommended)

`fastapi_deps_cache` enables caching for the duration of the block, then restores
FastAPI's originals and frees the cache memory on exit:

```python
from fastapi import APIRouter, FastAPI

from fastapi_cache_di import fastapi_deps_cache

app = FastAPI()
users = APIRouter()
orders = APIRouter()

with fastapi_deps_cache(warm_up=app):
    app.include_router(users)
    app.include_router(orders)
# included routes built, originals restored here; cache memory freed
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

### Warming up included routers

From FastAPI 0.137, `include_router` no longer copies the included routes into
the app: it adds a wrapper, and FastAPI builds the effective routes on the first
request through each included router (or the first `/openapi.json`). That build
introspects every dependency tree again, after the block has exited and without
the cache, so the first request is as slow as without the library.

`warm_up` takes the app (or an `APIRouter`) whose included routes are built when
the block exits normally, while the cache is still active:

```python
with fastapi_deps_cache(warm_up=app):
    app.include_router(users)
```

- It relies on `fastapi.routing.iter_route_contexts`, and has no effect on a
  FastAPI that lacks it (before 0.137.2). Before 0.137, nothing is deferred.
- It does not run when the block raises; the exception propagates unchanged.
- Routes added after the block, routes of mounted sub-applications
  (`app.mount(...)`) and `frontend()` routes are still built on first use,
  without the cache: warm a mounted FastAPI app with its own nested
  `fastapi_deps_cache(warm_up=subapp)` block.
- It is ignored with `deps_cache=False`.
- Nested in another block, or under an active `patch_fastapi_deps_cache()`, it
  runs with that cache.

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

## Calling loop-safe dependencies inline

At **request time**, FastAPI calls every dependency that is neither a coroutine
nor a generator through the threadpool: every `Depends(SomeClass)` and every
plain `def` dependency costs a round trip to a worker thread, often far more
than a class whose `__init__` only stores its arguments.

`patch_fastapi_run_in_threadpool()` lets such dependencies run directly on the
event loop. It is opt-in, because the library cannot tell whether a call blocks:

- `loop_safe` marks one sync function or class (it returns the same object, so
  `Depends(...)` and `dependency_overrides` keep working);
- `inline_classes=True` treats every class as loop-safe;
- everything else still goes through the threadpool, as do sync generator
  dependencies and sync endpoints.

```python
from typing import Annotated

from fastapi import Depends, FastAPI

from fastapi_cache_di import loop_safe, patch_fastapi_run_in_threadpool

patch_fastapi_run_in_threadpool(inline_classes=True)


@loop_safe
def get_tenant_id() -> str:
    return "acme"


class UserRepository:
    def __init__(self, tenant_id: Annotated[str, Depends(get_tenant_id)]) -> None:
        self.tenant_id = tenant_id


app = FastAPI()


@app.get("/users")
async def list_users(repo: Annotated[UserRepository, Depends()]) -> str:
    return repo.tenant_id
```

Unlike the dependency cache, this patch stays active for the app's lifetime;
`unpatch_fastapi_run_in_threadpool()` restores FastAPI's behavior. The patches
of this library are independent: any of them can be used without the others.

!!! warning
    **A loop-safe callable must never block**: no I/O, no lock, no heavy
    computation. A blocking call on the event loop stalls every in-flight
    request. A JWT dependency that fetches its signing keys over HTTP when they
    are not cached, for instance, must stay in the threadpool. The marker is
    never inherited: a subclass of a marked class must be marked on its own.

An override in `dependency_overrides` is judged on its own marker, not on the
original's: an unmarked override (a test lambda, for instance) goes through the
threadpool.

The marker is the `__fastapi_loop_safe__` attribute, a contract shared with
[fastapi-standalone-di](https://github.com/Toilal/fastapi-standalone-di): an
object marked by either library's `loop_safe` is recognised by both, without one
depending on the other.

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

`unpatch_fastapi_solve_dependencies()` restores FastAPI's behavior.

The values injected into endpoints are unchanged. The only visible difference: a
`use_cache=False` dependency nested under a cached dependency is no longer called
again during the skipped walk, whose result was discarded anyway. It works the
same with `dependency_overrides` set, as in most test suites.

## Solving routes from a precompiled plan

Even with the sub-tree skip, FastAPI still walks the dependency tree
recursively at every request, and recomputes for each dependency what only
depends on the route: its cache key, whether it is a coroutine, a generator or
a plain function, which request parameters it reads. On routes with about a
hundred dependencies, that framework work outweighs the dependencies
themselves.

`patch_fastapi_resolution_plan()` compiles each route's dependency tree once,
at the route's first request, into a flat plan in FastAPI's evaluation order,
then solves every request of that route from the plan in a single loop. On a
route of 100 dependencies and 211 edges, solving a request takes about a tenth
of the time it takes with the sub-tree skip alone. It is opt-in, and stays
active for the app's lifetime; `unpatch_fastapi_resolution_plan()` restores
FastAPI's recursive walk.

```python
from fastapi import FastAPI

from fastapi_cache_di import patch_fastapi_resolution_plan

patch_fastapi_resolution_plan()

app = FastAPI()
```

The values injected into endpoints, the validation errors (including the ones a
dependency reports at each of its occurrences), the request's dependency cache
and the exit order of `yield` dependencies are the ones FastAPI produces. Like
the sub-tree skip, a `use_cache=False` dependency nested under an already solved
dependency is not called again.

- **Overrides** — the plan is compiled with `dependency_overrides` applied, and
  compiled again when they change, so a test suite that sets overrides is solved
  by the plan too. A route keeps one plan without overrides and one for the last
  overrides it saw. A plan only keeps what it reads at request time, not the
  sub-trees FastAPI rebuilds under overrides: a few tens of KiB for a route of
  100 dependencies, with or without overrides.
- **Storage** — a route's plans are stored on its app and freed with it. A
  dependency tree changed after the route's first request is not compiled
  again.
- **Combining** — it is independent of the other patches. Combined with
  `patch_fastapi_solve_dependencies()`, either can be removed first; the plan
  takes over while both are active. Sync dependencies still go through
  `patch_fastapi_run_in_threadpool()` when it is active.

!!! warning
    The plan reproduces FastAPI internals (`solve_dependencies`, its request
    cache keys and its evaluation order). The test suite compares the plan with
    FastAPI on a reference dependency tree for every FastAPI version of the
    compatibility matrix, and a benchmark gate fails the pipeline if the plan
    stops being at least twice as fast as the sub-tree skip. The patch raises a
    `RuntimeError` on a FastAPI version that no longer defines a function the
    plan calls.
