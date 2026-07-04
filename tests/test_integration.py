"""Integration tests: a real FastAPI app loaded under the deps cache."""

from typing import Annotated

from fastapi import Depends, FastAPI, Security
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from fastapi_cache_di import DepsCache, fastapi_deps_cache


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
            assert len(cache.flat_dependants) > 0

    def test_caller_owned_cache_survives_context_exit(self) -> None:
        cache = DepsCache()
        with fastapi_deps_cache(deps_cache=cache):
            _build_app()
        # A caller-provided cache is left populated for post-hoc inspection.
        assert len(cache.signatures) > 0
        assert len(cache.dependants) > 0
        assert len(cache.flat_dependants) > 0

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
