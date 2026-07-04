API reference
=============

Everything below is exported from the top-level `fastapi_cache_di` package.

```python
from fastapi_cache_di import (
    DepsCache,
    fastapi_deps_cache,
    patch_fastapi_deps_cache,
    unpatch_fastapi_deps_cache,
)
```

## `fastapi_deps_cache`

```python
fastapi_deps_cache(*, deps_cache: DepsCache | bool = True) -> ContextManager[None]
```

Context manager that enables dependency caching for the block's duration and
restores FastAPI's originals on exit.

**Parameters**

- `deps_cache`:
    - `True` (default) — use a temporary cache scoped to this context.
    - `False` — no-op; caching is disabled.
    - a `DepsCache` instance — use (and populate) a shared cache that survives
      the block.

The context manager only tears down what it installed: because
`patch_fastapi_deps_cache` returns `False` when caching is already active, a
nested block does not unpatch and destroy the still-wanted outer cache.

## `patch_fastapi_deps_cache`

```python
patch_fastapi_deps_cache(deps_cache: DepsCache | None = None) -> bool
```

Install caching wrappers for `get_typed_signature`, `get_dependant` and
`get_flat_dependant`. Call it **once**, before the app loads its routes.

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
summary at `INFO` level. An internally-created cache is cleared; a caller-owned
cache is left populated (entries and counters) for post-hoc inspection.

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
| `flat_dependants` | `dict[tuple, Dependant]` | `get_flat_dependant` results, keyed by `(id(dependant), parent_oauth_scopes)` |
| `sig_hits`, `sig_misses` | `int` | `get_typed_signature` cache hits / misses |
| `dep_hits`, `dep_misses` | `int` | `get_dependant` cache hits / misses |
| `flat_hits`, `flat_misses` | `int` | `get_flat_dependant` cache hits / misses |

**Methods**

- `keep_alive(*objs: object) -> None` — pin objects so their `id()` stays
  reserved while they are used as cache keys (guards against `id()` recycling
  after garbage collection). Called internally by the wrappers.
- `clear() -> None` — drop all cached entries and reset every hit/miss counter.

## `__version__`

The installed package version, as a string.
