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
from fastapi import FastAPI
from fastapi_cache_di import fastapi_deps_cache

app = FastAPI()

with fastapi_deps_cache():
    from myapp.routers import users, orders, billing  # importing registers routes
    app.include_router(users.router)
    app.include_router(orders.router)
    app.include_router(billing.router)
# originals restored here; cache memory freed
```

Or patch/unpatch manually (call `patch` **before** routes are loaded):

```python
from fastapi_cache_di import patch_fastapi_deps_cache, unpatch_fastapi_deps_cache

patch_fastapi_deps_cache()
try:
    load_all_routes(app)
finally:
    unpatch_fastapi_deps_cache()  # logs hit/miss stats, restores originals
```

### Sharing / inspecting the cache

```python
from fastapi_cache_di import DepsCache, fastapi_deps_cache

cache = DepsCache()
with fastapi_deps_cache(deps_cache=cache):
    load_all_routes(app)

print(len(cache.dependants), len(cache.flat_dependants))
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

## Requirements

- Python ≥ 3.12
- FastAPI ≥ 0.135

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

## License

[MIT](./LICENSE) © Rémi Alvergnat
