fastapi-cache-di
================

[![Latest Version](https://img.shields.io/pypi/v/fastapi-cache-di.svg)](https://pypi.python.org/pypi/fastapi-cache-di)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/Toilal/fastapi-cache-di/blob/develop/LICENSE)
[![Build Status](https://img.shields.io/github/actions/workflow/status/Toilal/fastapi-cache-di/ci.yml?branch=develop)](https://github.com/Toilal/fastapi-cache-di/actions/workflows/ci.yml)
[![Codecov](https://img.shields.io/codecov/c/github/Toilal/fastapi-cache-di)](https://codecov.io/gh/Toilal/fastapi-cache-di)
[![semantic-release](https://img.shields.io/badge/%20%20%F0%9F%93%A6%F0%9F%9A%80-semantic--release-e10079.svg)](https://github.com/relekang/python-semantic-release)

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

Install
-------

Install fastapi-cache-di with [pip](https://pip.pypa.io/):

```bash
pip install fastapi-cache-di
```

Or add it to your project with [uv](https://docs.astral.sh/uv/):

```bash
uv add fastapi-cache-di
```

Quickstart
----------

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

That is all it takes: introspection results are memoized while routes are being
registered, and FastAPI's original functions are restored when the block exits.

See the [usage guide](./usage.md) for manual patching, sharing and inspecting a
cache, and thread-safety notes. See [how it works](./how-it-works.md) for the
caching internals, and the [API reference](./api.md) for the full public API.

Requirements
------------

- Python ≥ 3.12
- FastAPI ≥ 0.112.4

!!! note
    `0.112.4` is the floor: that is when FastAPI started calling
    `get_flat_dependant` during route construction, which is the hot path this
    library caches. Validated in CI up to FastAPI 0.139.

Support
-------

This project is hosted on [GitHub](https://github.com/Toilal/fastapi-cache-di).
Feel free to open an [issue](https://github.com/Toilal/fastapi-cache-di/issues)
if you think you have found a bug or something is missing.

License
-------

fastapi-cache-di is licensed under the
[MIT license](https://github.com/Toilal/fastapi-cache-di/blob/develop/LICENSE).
