API reference
=============

Everything below is exported from the top-level `fastapi_cache_di` package.

```python
from fastapi_cache_di import (
    DepsCache,
    fastapi_deps_cache,
    is_loop_safe,
    loop_safe,
    patch_fastapi_deps_cache,
    patch_fastapi_resolution_plan,
    patch_fastapi_run_in_threadpool,
    patch_fastapi_solve_dependencies,
    unpatch_fastapi_deps_cache,
    unpatch_fastapi_resolution_plan,
    unpatch_fastapi_run_in_threadpool,
    unpatch_fastapi_solve_dependencies,
)
```

## `fastapi_deps_cache`

```python
fastapi_deps_cache(
    *,
    deps_cache: DepsCache | bool = True,
    warm_up: FastAPI | APIRouter | None = None,
) -> ContextManager[None]
```

Context manager that enables dependency caching for the block's duration and
restores FastAPI's originals on exit.

**Parameters**

- `deps_cache`:
    - `True` (default) — use a temporary cache scoped to this context.
    - `False` — no-op; caching is disabled (`warm_up` is ignored).
    - a `DepsCache` instance — use (and populate) a shared cache that survives
      the block.
- `warm_up`: an app or router whose included routers' routes are built when the
  block exits normally, while the cache is still active. From FastAPI 0.137,
  FastAPI builds them on the first request through each included router
  instead, after the cache is gone. Mounted sub-applications are not walked:
  warm each one in its own nested block. No effect on a FastAPI without
  `fastapi.routing.iter_route_contexts` (before 0.137.2).

The context manager only tears down what it installed: because
`patch_fastapi_deps_cache` returns `False` when caching is already active, a
nested block does not unpatch and destroy the still-wanted outer cache.

## `patch_fastapi_deps_cache`

```python
patch_fastapi_deps_cache(deps_cache: DepsCache | None = None) -> bool
```

Install caching wrappers for `get_typed_signature`, `get_dependant` and, on
FastAPI ≤ 0.140, `get_flat_dependant`. Call it **once**, before the app loads
its routes.

**Parameters**

- `deps_cache`: a `DepsCache` instance to use as backing store. If `None`
  (default), a new cache is created internally and cleared on `unpatch`.

**Returns** `True` on the first call, `False` if already patched. When already
patched, passing a different `deps_cache` is ignored (the existing cache is kept)
and a warning is logged.

## `unpatch_fastapi_deps_cache`

```python
unpatch_fastapi_deps_cache() -> bool
```

Restore the original FastAPI functions and free cache memory. Logs a hit/miss
summary of the patched functions at `INFO` level. An internally-created cache is cleared; a caller-owned
cache is left populated (entries and counters) for post-hoc inspection.

**Returns** `True` if unpatched, `False` if not currently patched.

## `patch_fastapi_run_in_threadpool`

```python
patch_fastapi_run_in_threadpool(*, inline_classes: bool = False) -> bool
```

Call loop-safe sync dependencies directly on the event loop. Replaces
`fastapi.dependencies.utils.run_in_threadpool`, which FastAPI awaits for every
sync, non-generator dependency at request time; meant to stay active for the
app's lifetime. A callable marked with `loop_safe` is called inline; everything
else still goes through the threadpool. Sync generator dependencies and sync
endpoints are not affected.

**Parameters**

- `inline_classes`: treat every class as loop-safe, marked or not. Only enable it
  when no injected class does I/O, takes a lock or computes heavily in its
  `__init__`.

**Returns** `True` on the first call, `False` if already patched. Re-patching
with a different `inline_classes` is ignored and logs a warning.

**Raises** `RuntimeError` on a FastAPI version that no longer calls dependencies
through that global, rather than silently losing the optimization.

## `unpatch_fastapi_run_in_threadpool`

```python
unpatch_fastapi_run_in_threadpool() -> bool
```

Restore FastAPI's `run_in_threadpool` for dependencies.

**Returns** `True` if unpatched, `False` if not currently patched.

## `loop_safe`

```python
loop_safe(obj: T) -> T
```

Mark a sync function or a class as safe to call on the event loop, by setting
its `__fastapi_loop_safe__` attribute. Returns `obj` itself, so `Depends(obj)`
and `dependency_overrides[obj]` keep working. A loop-safe callable must never
block.

## `is_loop_safe`

```python
is_loop_safe(call: Callable[..., Any]) -> bool
```

`True` if `call` itself carries the loop-safe marker. The marker is read from the
object's own namespace, never inherited; `functools.partial` wrappers are
unwrapped, and a bound method follows its function's marker.

## `patch_fastapi_solve_dependencies`

```python
patch_fastapi_solve_dependencies() -> bool
```

Skip re-walking the sub-tree of dependencies already solved for a request.
Replaces `solve_dependencies` in `fastapi.dependencies.utils` and
`fastapi.routing`; meant to stay active for the app's lifetime, independently of
the dependency cache.

**Returns** `True` on the first call, `False` if already patched.

**Raises** `RuntimeError` on a FastAPI version that no longer defines
`solve_dependencies` in either module, rather than silently losing the
optimization.

## `unpatch_fastapi_solve_dependencies`

```python
unpatch_fastapi_solve_dependencies() -> bool
```

Restore FastAPI's `solve_dependencies` in both modules. `fastapi.routing` keeps
the patched function while `patch_fastapi_resolution_plan` is active.

**Returns** `True` if unpatched, `False` if not currently patched.

## `patch_fastapi_resolution_plan`

```python
patch_fastapi_resolution_plan() -> bool
```

Solve each route's dependencies from a plan compiled on its first request, and
compiled again when `dependency_overrides` change. Replaces `solve_dependencies`
in `fastapi.routing`, the root call of HTTP routes, WebSocket routes and route
groups; meant to stay active for the app's lifetime, independently of the other
patches.

**Returns** `True` on the first call, `False` if already patched.

**Raises** `RuntimeError` on a FastAPI version missing a function the plan
calls.

## `unpatch_fastapi_resolution_plan`

```python
unpatch_fastapi_resolution_plan() -> bool
```

Solve routes recursively again, as FastAPI does. `fastapi.routing` keeps the
sub-tree skip while `patch_fastapi_solve_dependencies` is active.

**Returns** `True` if unpatched, `False` if not currently patched.

## `DepsCache`

```python
DepsCache()
```

Unified backing store for the three levels of memoized introspection data. The
hit/miss counters live on the instance (not on module globals), so a
caller-owned cache still exposes its effectiveness after `unpatch`.

**Attributes**

| Attribute | Type | Description |
|---|---|---|
| `signatures` | `dict[int, inspect.Signature]` | `get_typed_signature` results, keyed by `id(call)` |
| `dependants` | `dict[tuple, Dependant]` | `get_dependant` results, keyed by a composite tuple of every argument that affects the produced `Dependant` |
| `flat_dependants` | `dict[tuple, Dependant]` | `get_flat_dependant` results, keyed by `(id(dependant), parent_oauth_scopes)`; always empty on FastAPI ≥ 0.141 |
| `sig_hits`, `sig_misses` | `int` | `get_typed_signature` cache hits / misses |
| `dep_hits`, `dep_misses` | `int` | `get_dependant` cache hits / misses |
| `flat_hits`, `flat_misses` | `int` | `get_flat_dependant` cache hits / misses; always 0 on FastAPI ≥ 0.141 |

**Methods**

- `keep_alive(*objs: object) -> None` — pin objects so their `id()` stays
  reserved while they are used as cache keys (guards against `id()` recycling
  after garbage collection). Called internally by the wrappers.
- `clear() -> None` — drop all cached entries and reset every hit/miss counter.

## `__version__`

The installed package version, as a string.
