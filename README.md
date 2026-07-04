# fastapi-cache-di

Speed up **FastAPI startup** by caching its dependency-tree introspection.

At startup, FastAPI introspects every route's dependency tree — calling
`get_typed_signature`, `get_dependant` and `get_flat_dependant` — with **no
caching**. Dependencies shared across many routes (auth checks, DB handles,
role guards …) are re-introspected from scratch for every route. On a large API
this dominates import/boot time.

`fastapi-cache-di` monkeypatches those three functions to memoize their results
for the duration of route loading, then restores the originals. The heavy one is
`get_flat_dependant`, called once per route in `APIRoute.__init__`: caching its
recursive results turns `O(routes × tree-depth)` into `O(routes + unique-deps)`.

## Install

```bash
pip install fastapi-cache-di
# or
uv add fastapi-cache-di
```

## Usage

Wrap the code that loads your routes with the context manager:

```python
from fastapi import APIRouter, FastAPI

from fastapi_cache_di import fastapi_deps_cache

app = FastAPI()
users = APIRouter()
orders = APIRouter()
billing = APIRouter()

with fastapi_deps_cache():
    # Registering routes introspects each dependency tree; caching is active here.
    app.include_router(users)
    app.include_router(orders)
    app.include_router(billing)
# originals restored here; cache memory freed
```

Or patch/unpatch manually (call `patch` **before** routes are loaded):

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

### Sharing / inspecting the cache

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

## How it works

`patch_fastapi_deps_cache()` replaces, in `fastapi.dependencies.utils` (plus the
`fastapi.routing` and `fastapi.openapi.utils` re-imports):

| Function | Cache key | Why |
|---|---|---|
| `get_typed_signature` | `id(call)` | avoid repeated signature parsing |
| `get_dependant` | `(id(call), path params, name, scopes, use_cache, scope)` | avoid redundant tree introspection |
| `get_flat_dependant` | `(id(dependant), parent_oauth_scopes)` | the dominant startup cost |

`get_flat_dependant` results are shallow-copied on the way out so callers can
safely mutate the returned lists. Calls with `skip_repeats=True` (OpenAPI
generation) bypass the cache since they depend on a mutable `visited` list.

> **Note** — this patches FastAPI internals, so it is pinned to a compatible
> FastAPI range. It is a **startup-only** optimization: patch before loading
> routes, unpatch afterwards.

> **Thread-safety** — all patch state is process-global and patching swaps
> FastAPI module attributes for the whole process. It is not re-entrant across
> threads: call `patch`/`unpatch` (or the context manager) from a single thread
> during startup only. Calling `patch_fastapi_deps_cache(deps_cache=...)` while a
> patch is already active keeps the existing cache and logs a warning — the new
> cache is ignored until you `unpatch` first.

## Documentation

Full documentation is published at
**<https://toilal.github.io/fastapi-cache-di>** (usage guide, internals, and API
reference). The in-progress `develop` build is previewed at
<https://toilal.github.io/fastapi-cache-di/dev/>.

## Requirements

- Python ≥ 3.12
- FastAPI ≥ 0.112.4

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

## License

[MIT](./LICENSE) © Rémi Alvergnat
