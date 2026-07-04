# fastapi-cache-di

Speed up **FastAPI startup** by caching its dependency-tree introspection.

At startup, FastAPI re-introspects every route's dependency tree with no caching,
so dependencies shared across many routes (auth checks, DB handles, role guards …)
are parsed from scratch again and again. `fastapi-cache-di` memoizes that work for
the duration of route loading, turning `O(routes × tree-depth)` into
`O(routes + unique-deps)`, then restores FastAPI's originals.

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

Manual patch/unpatch, sharing and inspecting the cache, and the cache-key details
are covered in the documentation.

## Documentation

Full documentation is available at
[toilal.github.io/fastapi-cache-di](https://toilal.github.io/fastapi-cache-di/)
(usage guide, internals, and API reference).

## Requirements

- Python ≥ 3.12
- FastAPI ≥ 0.112.4

## License

[MIT](./LICENSE) © Rémi Alvergnat
