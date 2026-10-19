"""Integration tests: a real FastAPI app loaded under the deps cache."""

from collections.abc import Callable
from typing import Annotated, Any

import fastapi.dependencies.utils as _dep_utils
import fastapi.routing as _routing
import pytest
from _compat import (
    HAS_FLAT_DEPENDANT,
    requires_iter_route_contexts,
    requires_no_iter_route_contexts,
)
from fastapi import APIRouter, Depends, FastAPI, Security, WebSocket
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from fastapi_cache_di import (
    DepsCache,
    fastapi_deps_cache,
    patch_fastapi_deps_cache,
    unpatch_fastapi_deps_cache,
)


def _shared_dep() -> str:
    return "shared"


def _route_dep_calls(app: FastAPI, path: str) -> list[str]:
    """Names of the top-level dependencies FastAPI attached to ``path``."""
    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == path)
    return sorted(d.call.__name__ for d in route.dependant.dependencies if d.call)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/a")
    def route_a(dep: Annotated[str, Depends(_shared_dep)]) -> dict[str, str]:
        return {"route": "a", "dep": dep}

    @app.get("/b")
    def route_b(dep: Annotated[str, Depends(_shared_dep)]) -> dict[str, str]:
        return {"route": "b", "dep": dep}

    return app


class TestBehaviorUnchanged:
    def test_routes_work_when_loaded_under_cache(self) -> None:
        with fastapi_deps_cache():
            app = _build_app()

        client = TestClient(app)
        assert client.get("/a").json() == {"route": "a", "dep": "shared"}
        assert client.get("/b").json() == {"route": "b", "dep": "shared"}

    def test_openapi_still_generates(self) -> None:
        with fastapi_deps_cache():
            app = _build_app()

        schema = app.openapi()
        assert "/a" in schema["paths"]
        assert "/b" in schema["paths"]

    def test_matches_uncached_app(self) -> None:
        uncached = _build_app()
        with fastapi_deps_cache():
            cached = _build_app()

        assert (
            TestClient(cached).get("/a").json() == TestClient(uncached).get("/a").json()
        )


class TestCachePopulated:
    def test_shared_cache_records_entries(self) -> None:
        cache = DepsCache()
        with fastapi_deps_cache(deps_cache=cache):
            _build_app()
            assert len(cache.signatures) > 0
            assert len(cache.dependants) > 0
            assert bool(cache.flat_dependants) is HAS_FLAT_DEPENDANT

    def test_caller_owned_cache_survives_context_exit(self) -> None:
        cache = DepsCache()
        with fastapi_deps_cache(deps_cache=cache):
            _build_app()
        # A caller-provided cache is left populated for post-hoc inspection.
        assert len(cache.signatures) > 0
        assert len(cache.dependants) > 0
        assert bool(cache.flat_dependants) is HAS_FLAT_DEPENDANT

    def test_cache_disabled_is_noop(self) -> None:
        with fastapi_deps_cache(deps_cache=False):
            app = _build_app()
        assert TestClient(app).get("/a").status_code == 200


def _dep_a() -> None: ...
def _dep_b() -> None: ...
def _shared_endpoint() -> dict[str, str]:
    return {}


def _build_shared_endpoint_app(*, cache: bool) -> FastAPI:
    """Two routes reusing one endpoint callable, each with its own dependency."""
    app = FastAPI()
    with fastapi_deps_cache(deps_cache=cache):
        app.add_api_route("/one", _shared_endpoint, dependencies=[Depends(_dep_a)])
        app.add_api_route("/two", _shared_endpoint, dependencies=[Depends(_dep_b)])
    return app


class TestNoCrossRouteDependencyLeak:
    """Regression for the shared-mutable-Dependant corruption (issue #1)."""

    def test_route_level_dependencies_stay_isolated(self) -> None:
        uncached = _build_shared_endpoint_app(cache=False)
        cached = _build_shared_endpoint_app(cache=True)

        assert _route_dep_calls(cached, "/one") == _route_dep_calls(uncached, "/one")
        assert _route_dep_calls(cached, "/two") == _route_dep_calls(uncached, "/two")
        assert _route_dep_calls(cached, "/one") == ["_dep_a"]
        assert _route_dep_calls(cached, "/two") == ["_dep_b"]

    def test_security_dependency_does_not_leak_to_sibling(self) -> None:
        def guard() -> None: ...

        def endpoint() -> dict[str, str]:
            return {}

        app = FastAPI()
        with fastapi_deps_cache():
            app.add_api_route("/guarded", endpoint, dependencies=[Security(guard)])
            app.add_api_route("/open", endpoint)

        assert _route_dep_calls(app, "/guarded") == ["guard"]
        assert _route_dep_calls(app, "/open") == []

    def test_route_level_security_requirements_do_not_accumulate(self) -> None:
        """Two routes sharing one Security dependency must not have its security
        requirement duplicated under the cache (FastAPI < 0.121 appends to the
        returned dependant's ``security_requirements`` list)."""
        from fastapi.security import OAuth2PasswordBearer

        scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

        def endpoint() -> dict[str, str]:
            return {}

        def security_req_counts(app: FastAPI, path: str) -> list[int]:
            route = next(
                r for r in app.routes if isinstance(r, APIRoute) and r.path == path
            )
            return [
                len(getattr(d, "security_requirements", []))
                for d in route.dependant.dependencies
            ]

        def build(*, cache: bool) -> FastAPI:
            app = FastAPI()
            with fastapi_deps_cache(deps_cache=cache):
                app.add_api_route(
                    "/one", endpoint, dependencies=[Security(scheme, scopes=["read"])]
                )
                app.add_api_route(
                    "/two", endpoint, dependencies=[Security(scheme, scopes=["read"])]
                )
            return app

        uncached = build(cache=False)
        cached = build(cache=True)
        for path in ("/one", "/two"):
            assert security_req_counts(cached, path) == security_req_counts(
                uncached, path
            )

    def test_use_cache_false_dependency_isolated(self) -> None:
        def dep_x() -> None: ...
        def dep_y() -> None: ...

        def endpoint() -> dict[str, str]:
            return {}

        app = FastAPI()
        with fastapi_deps_cache():
            app.add_api_route(
                "/x", endpoint, dependencies=[Depends(dep_x, use_cache=False)]
            )
            app.add_api_route(
                "/y", endpoint, dependencies=[Depends(dep_y, use_cache=False)]
            )

        assert _route_dep_calls(app, "/x") == ["dep_x"]
        assert _route_dep_calls(app, "/y") == ["dep_y"]


def _leaf() -> int:
    return 1


def _head(value: Annotated[int, Depends(_leaf)]) -> int:
    return value + 1


def _build_nested_router() -> APIRouter:
    """A child router (HTTP and WebSocket routes) included in a parent router."""
    child = APIRouter()

    @child.get("/items/{item_id}", name="item")
    def item(item_id: int, value: Annotated[int, Depends(_head)]) -> int:
        return item_id + value

    @child.websocket("/ws")
    async def ws(websocket: WebSocket, value: Annotated[int, Depends(_head)]) -> None:
        await websocket.accept()
        await websocket.send_json(value)
        await websocket.close()

    parent = APIRouter()
    parent.include_router(child, prefix="/child", dependencies=[Depends(_leaf)])
    return parent


def _install_counter(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Count the ``get_dependant`` calls made from now on.

    Installed after the deps cache context has exited, so every counted call
    is an introspection the cache did not serve.
    """
    original = _dep_utils.get_dependant
    calls = 0

    def counting(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(_dep_utils, "get_dependant", counting)
    monkeypatch.setattr(_routing, "get_dependant", counting)
    return lambda: calls


def _exercise(app: FastAPI) -> None:
    """Hit every lazy build path of an included router's routes."""
    with TestClient(app) as client:
        assert client.get("/api/child/items/3").json() == 5
        with client.websocket_connect("/api/child/ws") as conn:
            assert conn.receive_json() == 2
        assert (
            "/api/child/items/{item_id}" in client.get("/openapi.json").json()["paths"]
        )
    assert app.url_path_for("item", item_id="7") == "/api/child/items/7"


@requires_iter_route_contexts
class TestWarmUp:
    def test_warm_up_builds_included_routes_under_the_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = FastAPI()
        with fastapi_deps_cache(warm_up=app):
            app.include_router(_build_nested_router(), prefix="/api")

        calls = _install_counter(monkeypatch)
        _exercise(app)
        assert calls() == 0

    def test_without_warm_up_included_routes_are_built_after_the_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = FastAPI()
        with fastapi_deps_cache():
            app.include_router(_build_nested_router(), prefix="/api")

        calls = _install_counter(monkeypatch)
        _exercise(app)
        assert calls() > 0

    def test_include_dependencies_stay_per_inclusion(self) -> None:
        seen: list[str] = []

        def record() -> None:
            seen.append("record")

        child = APIRouter()

        @child.get("/x")
        def x(value: Annotated[int, Depends(_leaf)]) -> int:
            return value

        guarded, plain = FastAPI(), FastAPI()
        with fastapi_deps_cache(warm_up=guarded):
            guarded.include_router(child, dependencies=[Depends(record)])
        with fastapi_deps_cache(warm_up=plain):
            plain.include_router(child)

        assert TestClient(plain).get("/x").json() == 1
        assert seen == []
        assert TestClient(guarded).get("/x").json() == 1
        assert seen == ["record"]
        declared = next(r for r in child.routes if isinstance(r, APIRoute))
        assert [d.call for d in declared.dependant.dependencies] == [_leaf]

    def test_no_warm_up_when_the_block_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = _dep_utils.get_dependant
        app = FastAPI()

        def load_then_fail() -> None:
            with fastapi_deps_cache(warm_up=app):
                app.include_router(_build_nested_router(), prefix="/api")
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            load_then_fail()

        assert _dep_utils.get_dependant is original
        calls = _install_counter(monkeypatch)
        _exercise(app)
        assert calls() > 0

    def test_nested_context_warms_up_with_the_outer_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = _dep_utils.get_dependant
        app = FastAPI()
        patch_fastapi_deps_cache()
        try:
            with fastapi_deps_cache(warm_up=app):
                app.include_router(_build_nested_router(), prefix="/api")
            assert _dep_utils.get_dependant is not original
        finally:
            unpatch_fastapi_deps_cache()

        calls = _install_counter(monkeypatch)
        _exercise(app)
        assert calls() == 0

    def test_warm_up_ignored_when_caching_is_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = FastAPI()
        with fastapi_deps_cache(deps_cache=False, warm_up=app):
            app.include_router(_build_nested_router(), prefix="/api")

        calls = _install_counter(monkeypatch)
        _exercise(app)
        assert calls() > 0


@requires_no_iter_route_contexts
def test_warm_up_is_a_noop_without_iter_route_contexts() -> None:
    app = FastAPI()
    with fastapi_deps_cache(warm_up=app):
        app.include_router(_build_nested_router(), prefix="/api")

    _exercise(app)
