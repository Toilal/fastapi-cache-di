"""FastAPI app used to measure real uvicorn startup time.

Run by the startup benchmark as ``uvicorn _bench_app:app``. Behaviour is driven
by environment variables so the same module serves the cached and uncached
runs:

- ``BENCH_DEPTH``      depth of the shared dependency chain (default 30)
- ``BENCH_N_ROUTES``   number of routes sharing that chain (default 800)
- ``BENCH_USE_CACHE``  ``"1"`` to load routes inside ``fastapi_deps_cache()``

Every route injects the same deep dependency chain, which is exactly the case
the startup cache accelerates.
"""

import os
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI

from fastapi_cache_di import fastapi_deps_cache

_DEPTH = int(os.environ.get("BENCH_DEPTH", "30"))
_N_ROUTES = int(os.environ.get("BENCH_N_ROUTES", "800"))
_USE_CACHE = os.environ.get("BENCH_USE_CACHE") == "1"


def _build_shared_chain(depth: int) -> Callable[..., Any]:
    def leaf() -> int:
        return 0

    head: Callable[..., Any] = leaf
    for _ in range(depth):
        prev = head

        def node(value: int = Depends(prev)) -> int:
            return value

        head = node
    return head


def _register_routes(app: FastAPI, head: Callable[..., Any]) -> None:
    for i in range(_N_ROUTES):

        def endpoint(value: int = Depends(head)) -> int:
            return value

        endpoint.__name__ = f"route_{i}"
        app.add_api_route(f"/r{i}", endpoint)


def _build_app() -> FastAPI:
    app = FastAPI()
    head = _build_shared_chain(_DEPTH)
    if _USE_CACHE:
        with fastapi_deps_cache():
            _register_routes(app, head)
    else:
        _register_routes(app, head)

    @app.get("/__ready__")
    def ready() -> dict[str, bool]:
        return {"ok": True}

    return app


app = _build_app()
