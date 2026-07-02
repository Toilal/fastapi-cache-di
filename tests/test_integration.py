"""Integration tests: a real FastAPI app loaded under the deps cache."""

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from fastapi_cache_di import DepsCache, fastapi_deps_cache


def _shared_dep() -> str:
    return "shared"


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
            # The cache is populated during route loading; unpatch (on context
            # exit) clears it, so entries must be observed inside the block.
            assert len(cache.signatures) > 0
            assert len(cache.dependants) > 0
            assert len(cache.flat_dependants) > 0

    def test_cache_disabled_is_noop(self) -> None:
        with fastapi_deps_cache(deps_cache=False):
            app = _build_app()
        assert TestClient(app).get("/a").status_code == 200
