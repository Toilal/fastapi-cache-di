# fastapi-cache-di

[![Latest Version](https://img.shields.io/pypi/v/fastapi-cache-di.svg)](https://pypi.python.org/pypi/fastapi-cache-di)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/Toilal/fastapi-cache-di/blob/develop/LICENSE)
[![Build Status](https://img.shields.io/github/actions/workflow/status/Toilal/fastapi-cache-di/ci.yml?branch=develop)](https://github.com/Toilal/fastapi-cache-di/actions/workflows/ci.yml)
[![Codecov](https://img.shields.io/codecov/c/github/Toilal/fastapi-cache-di)](https://codecov.io/gh/Toilal/fastapi-cache-di)
[![semantic-release](https://img.shields.io/badge/%20%20%F0%9F%93%A6%F0%9F%9A%80-semantic--release-e10079.svg)](https://github.com/relekang/python-semantic-release)

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

with fastapi_deps_cache(warm_up=app):
    # Registering routes introspects each dependency tree; caching is active here.
    app.include_router(users)
    app.include_router(orders)
    app.include_router(billing)
# included routes built, originals restored here; cache memory freed
```

`warm_up=app` matters from FastAPI 0.137: `include_router` no longer builds the
included routes, FastAPI builds them on the first request through each included
router (or the first `/openapi.json`), after the block has exited and without
the cache. `warm_up` builds them when the block exits normally, while the cache
is still active. It has no effect before FastAPI 0.137.2, and does not cover
routes added after the block, mounted sub-applications or `frontend()` routes.

Manual patch/unpatch, sharing and inspecting the cache, and the cache-key details
are covered in the documentation.

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

**A loop-safe callable must never block**: no I/O, no lock, no heavy
computation. A blocking call on the event loop stalls every in-flight request.
See the documentation for overrides and the marker shared with
[fastapi-standalone-di](https://github.com/Toilal/fastapi-standalone-di).

## Skipping the sub-tree of cached dependencies

At **request time**, FastAPI solves the whole sub-tree of a dependency before
checking whether its value is already in the request's cache, so a dependency
shared across a deep tree is solved again at every occurrence.
`patch_fastapi_solve_dependencies()` stops that walk as soon as the value is
cached. It is opt-in and stays active for the app's lifetime:

```python
from fastapi import FastAPI

from fastapi_cache_di import patch_fastapi_solve_dependencies

patch_fastapi_solve_dependencies()

app = FastAPI()
```

The values injected into endpoints are unchanged; see the documentation for the
details.

## Documentation

Full documentation is available at
[toilal.github.io/fastapi-cache-di](https://toilal.github.io/fastapi-cache-di/)
(usage guide, internals, and API reference).

## Requirements

- Python ≥ 3.12
- FastAPI ≥ 0.112.4

## License

[MIT](./LICENSE) © Rémi Alvergnat
