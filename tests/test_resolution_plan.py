"""Each route's dependencies are solved from a plan compiled once."""

import asyncio
import gc
import inspect
import itertools
import random
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from typing import Annotated, Any

import fastapi.dependencies.utils as _dep_utils
import fastapi.routing as _routing
import pytest
from _compat import (
    HAS_DEPENDENCY_SCOPE,
    requires_route_groups,
)
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    Security,
    WebSocket,
)
from fastapi.dependencies.models import Dependant
from fastapi.requests import HTTPConnection
from fastapi.routing import APIRoute
from fastapi.security import SecurityScopes
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_cache_di import (
    fastapi_deps_cache,
    patch_fastapi_deps_cache,
    patch_fastapi_resolution_plan,
    patch_fastapi_run_in_threadpool,
    patch_fastapi_solve_dependencies,
    unpatch_fastapi_deps_cache,
    unpatch_fastapi_resolution_plan,
    unpatch_fastapi_run_in_threadpool,
    unpatch_fastapi_solve_dependencies,
)
from fastapi_cache_di import patch as patch_module
from fastapi_cache_di import plan as plan_module

_FASTAPI_SOLVE_DEPENDENCIES = _dep_utils.solve_dependencies
_FASTAPI_ROUTING_SOLVE_DEPENDENCIES = _routing.solve_dependencies


def _unpatch_all() -> None:
    unpatch_fastapi_resolution_plan()
    unpatch_fastapi_solve_dependencies()
    unpatch_fastapi_run_in_threadpool()
    unpatch_fastapi_deps_cache()


@pytest.fixture(autouse=True)
def _ensure_unpatched() -> Iterator[None]:
    _unpatch_all()
    yield
    _unpatch_all()


@pytest.fixture
def recursive_solves() -> Iterator[list[Callable[..., Any]]]:
    """The call of every dependant FastAPI's own ``solve_dependencies`` solves.

    Empty when the plan solved every request alone.
    """
    calls: list[Callable[..., Any]] = []

    async def spy(**kwargs: Any) -> Any:
        calls.append(kwargs["dependant"].call)
        return await _FASTAPI_SOLVE_DEPENDENCIES(**kwargs)

    _dep_utils.solve_dependencies = spy
    try:
        yield calls
    finally:
        _dep_utils.solve_dependencies = _FASTAPI_SOLVE_DEPENDENCIES


@contextmanager
def _root_results(solved: list[Any]) -> Iterator[None]:
    """Record the ``SolvedDependency`` of every root call."""
    inner = _routing.solve_dependencies

    async def record(**kwargs: Any) -> Any:
        result = await inner(**kwargs)
        solved.append(result)
        return result

    _routing.solve_dependencies = record
    try:
        yield
    finally:
        _routing.solve_dependencies = inner


def _comparable(value: Any) -> Any:
    """``value`` with request-bound objects replaced by their type, for equality."""
    if isinstance(value, dict):
        return {_comparable(k): _comparable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_comparable(item) for item in value)
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if inspect.isclass(value) or inspect.isroutine(value):
        return value.__qualname__
    return type(value).__qualname__


def _run(
    build: Callable[[], FastAPI], requests: list[tuple[str, str, Any]]
) -> list[Any]:
    """Responses and root ``SolvedDependency`` of each request, on a fresh app."""
    solved: list[Any] = []
    with (
        _root_results(solved),
        TestClient(build(), raise_server_exceptions=False) as client,
    ):
        responses = [
            client.request(method, path, json=body) for method, path, body in requests
        ]
    return [(r.status_code, r.content, dict(r.headers)) for r in responses] + [
        (
            _comparable(s.values),
            _comparable(s.errors),
            _comparable(s.dependency_cache),
        )
        for s in solved
    ]


def _same_with_and_without_plan(
    build: Callable[[], FastAPI], *requests: tuple[str, str, Any]
) -> None:
    unpatched = _run(build, list(requests))
    patch_fastapi_resolution_plan()
    assert _run(build, list(requests)) == unpatched


def _get(path: str) -> tuple[str, str, Any]:
    return ("GET", path, None)


def _depends(dependency: Callable[..., Any], **kwargs: Any) -> Any:
    if not HAS_DEPENDENCY_SCOPE:
        kwargs.pop("scope", None)
    return Depends(dependency, **kwargs)


# ---------------------------------------------------------------------------
# Reference tree: every kind of node FastAPI solves
# ---------------------------------------------------------------------------


class Item(BaseModel):
    name: str
    price: float


def _reference_app(events: list[str]) -> FastAPI:
    class Settings:
        def __init__(self) -> None:
            events.append("Settings")

    def db(settings: Annotated[Settings, Depends()]) -> str:
        events.append("db")
        return "db"

    async def repo(dal: Annotated[str, Depends(db)], q: int) -> str:
        events.append("repo")
        return f"repo({dal}, {q})"

    def session(dal: Annotated[str, Depends(db)]) -> Iterator[str]:
        events.append("enter session")
        yield "session"
        events.append("exit session")

    async def resource(
        x_token: Annotated[str | None, Header()] = None,
    ) -> AsyncIterator[str]:
        events.append("enter resource")
        yield f"resource({x_token})"
        events.append("exit resource")

    def scopes(security_scopes: SecurityScopes) -> list[str]:
        events.append("scopes")
        return sorted(security_scopes.scopes)

    def fresh() -> int:
        events.append("fresh")
        return len(events)

    def tagged(response: Response, tasks: BackgroundTasks, request: Request) -> str:
        response.headers["x-tagged"] = request.url.path
        tasks.add_task(events.append, "task")
        return "tagged"

    def service(
        repository: Annotated[str, Depends(repo)],
        sess: Annotated[str, _depends(session, scope="request")],
        granted: Annotated[list[str], Security(scopes, scopes=["read"])],
        tag: Annotated[str, Depends(tagged)],
        counter: Annotated[int, Depends(fresh, use_cache=False)],
    ) -> list[Any]:
        events.append("service")
        return [repository, sess, granted, tag, counter]

    def payload(item: Item, dal: Annotated[str, Depends(db)]) -> str:
        events.append("payload")
        return f"{item.name}:{item.price}:{dal}"

    def audit(request: Request) -> None:
        events.append(f"audit {request.method}")

    app = FastAPI(dependencies=[Depends(audit)])

    @app.get("/items/{item_id}")
    async def read(
        item_id: int,
        svc: Annotated[list[Any], Depends(service)],
        repository: Annotated[str, Depends(repo)],
        res: Annotated[str, _depends(resource, scope="function")],
        granted: Annotated[list[str], Security(scopes, scopes=["write"])],
        counter: Annotated[int, Depends(fresh, use_cache=False)],
        tasks: BackgroundTasks,
        session_id: Annotated[str | None, Cookie()] = None,
    ) -> list[Any]:
        tasks.add_task(events.append, "endpoint task")
        return [item_id, svc, repository, res, granted, counter, session_id]

    @app.post("/items")
    async def create(
        created: Annotated[str, Depends(payload)],
        dal: Annotated[str, Depends(db)],
    ) -> list[str]:
        return [created, dal]

    return app


_REFERENCE_REQUESTS = [
    _get("/items/1?q=2"),
    _get("/items/oops?q=oops"),
    _get("/items/3"),
    ("POST", "/items", {"name": "pen", "price": 1.5}),
    ("POST", "/items", {"name": "pen"}),
]


# ---------------------------------------------------------------------------
# Patch / unpatch
# ---------------------------------------------------------------------------


_PAIRS = {
    "plan": (patch_fastapi_resolution_plan, unpatch_fastapi_resolution_plan),
    "skip": (patch_fastapi_solve_dependencies, unpatch_fastapi_solve_dependencies),
}


class TestPatchUnpatch:
    def test_patch_then_unpatch_report_whether_they_acted(self) -> None:
        assert patch_fastapi_resolution_plan() is True
        assert patch_fastapi_resolution_plan() is False
        assert unpatch_fastapi_resolution_plan() is True
        assert unpatch_fastapi_resolution_plan() is False

    def test_patch_replaces_the_routing_global_only(self) -> None:
        patch_fastapi_resolution_plan()

        assert _routing.solve_dependencies is patch_module._route_solve_dependencies
        assert _dep_utils.solve_dependencies is _FASTAPI_SOLVE_DEPENDENCIES

    def test_unpatch_restores_the_routing_global(self) -> None:
        async def other(**kwargs: Any) -> Any:
            return await _FASTAPI_SOLVE_DEPENDENCIES(**kwargs)

        _routing.solve_dependencies = other
        try:
            patch_fastapi_resolution_plan()
            unpatch_fastapi_resolution_plan()

            assert _routing.solve_dependencies is other
        finally:
            _routing.solve_dependencies = _FASTAPI_ROUTING_SOLVE_DEPENDENCIES

    @pytest.mark.parametrize(
        ("patch_order", "unpatch_order"),
        list(
            itertools.product(
                itertools.permutations(_PAIRS), itertools.permutations(_PAIRS)
            )
        ),
    )
    def test_both_solve_patches_come_off_in_any_order(
        self,
        patch_order: tuple[str, ...],
        unpatch_order: tuple[str, ...],
        recursive_solves: list[Callable[..., Any]],
    ) -> None:
        app = _shared_tree([])
        fastapi_solve = _dep_utils.solve_dependencies
        for name in patch_order:
            assert _PAIRS[name][0]() is True
            assert _PAIRS[name][0]() is False

        first, second = unpatch_order
        assert _PAIRS[first][1]() is True
        assert _PAIRS[first][1]() is False
        assert _routing.solve_dependencies is patch_module._route_solve_dependencies
        recursive_solves.clear()
        _get_json(app, "/")
        if second == "plan":
            assert recursive_solves == []
        else:
            assert len(recursive_solves) == 4

        assert _PAIRS[second][1]() is True
        assert _routing.solve_dependencies is _FASTAPI_ROUTING_SOLVE_DEPENDENCIES
        assert _dep_utils.solve_dependencies is fastapi_solve

    def test_patch_fails_loudly_when_fastapi_drops_a_function_the_plan_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(_dep_utils, "request_params_to_args")

        with pytest.raises(RuntimeError, match="request_params_to_args"):
            patch_fastapi_resolution_plan()

        assert patch_module._resolution_plan_active is False
        assert _routing.solve_dependencies is _FASTAPI_ROUTING_SOLVE_DEPENDENCIES

    def test_independent_of_the_deps_cache_and_run_in_threadpool_pairs(
        self,
    ) -> None:
        patch_fastapi_resolution_plan()

        assert patch_fastapi_deps_cache() is True
        assert patch_fastapi_run_in_threadpool() is True
        assert unpatch_fastapi_deps_cache() is True
        assert unpatch_fastapi_run_in_threadpool() is True
        with fastapi_deps_cache():
            pass
        assert _routing.solve_dependencies is patch_module._route_solve_dependencies
        assert patch_module._resolution_plan_active is True


# ---------------------------------------------------------------------------
# Same results as FastAPI
# ---------------------------------------------------------------------------


def _shared_tree(built: list[str]) -> FastAPI:
    def db() -> str:
        built.append("db")
        return "db"

    def repo(dal: Annotated[str, Depends(db)]) -> str:
        return f"repo({dal})"

    def service(
        repository: Annotated[str, Depends(repo)], dal: Annotated[str, Depends(db)]
    ) -> str:
        return f"service({repository}, {dal})"

    app = FastAPI()

    @app.get("/")
    async def endpoint(
        svc: Annotated[str, Depends(service)],
        repository: Annotated[str, Depends(repo)],
        dal: Annotated[str, Depends(db)],
    ) -> list[str]:
        return [svc, repository, dal]

    return app


def _get_json(app: FastAPI, path: str) -> Any:
    with TestClient(app) as client:
        return client.get(path).json()


class TestSameResults:
    def test_reference_tree(self, recursive_solves: list[Callable[..., Any]]) -> None:
        events: list[str] = []
        unpatched = _run(lambda: _reference_app(events), _REFERENCE_REQUESTS)
        unpatched_events = events.copy()
        events.clear()
        recursive_solves.clear()

        patch_fastapi_resolution_plan()

        assert _run(lambda: _reference_app(events), _REFERENCE_REQUESTS) == unpatched
        assert events == unpatched_events
        # Only the repeated occurrences of a dependency that failed validation
        # are solved again by FastAPI.
        names = {getattr(call, "__name__", None) for call in recursive_solves}
        assert names <= {"repo", "db", "Settings", "payload"}

    @pytest.mark.parametrize("overridden", [False, True])
    def test_security_scopes_of_an_endpoint_without_scopes(
        self, overridden: bool
    ) -> None:
        def build() -> FastAPI:
            app = FastAPI()

            @app.get("/")
            async def endpoint(security_scopes: SecurityScopes) -> list[str]:
                return security_scopes.scopes

            if overridden:
                app.dependency_overrides[_unrelated] = lambda: "fake"
            return app

        _same_with_and_without_plan(build, _get("/"))

    def test_reference_tree_with_the_skip_patch_too(self) -> None:
        events: list[str] = []
        unpatched = _run(lambda: _reference_app(events), _REFERENCE_REQUESTS)
        unpatched_events = events.copy()
        events.clear()

        patch_fastapi_solve_dependencies()
        patch_fastapi_resolution_plan()

        assert _run(lambda: _reference_app(events), _REFERENCE_REQUESTS) == unpatched
        assert events == unpatched_events

    def test_errors_of_a_repeated_dependency_are_reported_at_each_occurrence(
        self,
    ) -> None:
        def build() -> FastAPI:
            def page(size: int) -> int:
                return size

            def listing(value: Annotated[int, Depends(page)]) -> int:
                return value

            app = FastAPI()

            @app.get("/")
            async def endpoint(
                first: Annotated[int, Depends(listing)],
                second: Annotated[int, Depends(page)],
                third: Annotated[int, Depends(listing)],
            ) -> list[int]:
                return [first, second, third]

            return app

        unpatched = _run(build, [_get("/")])
        assert len(_comparable(unpatched[1][1])) == 3

        patch_fastapi_resolution_plan()

        assert _run(build, [_get("/")]) == unpatched

    def test_a_repeated_occurrence_takes_what_fastapi_solves_and_caches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[str] = []

        def page(size: int) -> int:
            return size

        def listing(value: Annotated[int, Depends(page)]) -> int:
            called.append("listing")
            return value

        def other() -> str:
            called.append("other")
            return "other"

        app = FastAPI()

        @app.get("/")
        async def endpoint(
            first: Annotated[int, Depends(listing)],
            again: Annotated[int, Depends(listing)],
            later: Annotated[str, Depends(other)],
        ) -> None:
            pass

        later_key = plan_module.request_cache_key(
            _route_of(app).dependant.dependencies[2], {}
        )

        async def solved_on_second_try(**kwargs: Any) -> Any:
            kwargs["dependency_cache"][later_key] = "cached"
            return _dep_utils.SolvedDependency(
                values={"value": 5},
                errors=[],
                background_tasks=kwargs["background_tasks"],
                response=kwargs["response"],
                dependency_cache=kwargs["dependency_cache"],
            )

        monkeypatch.setattr(_dep_utils, "solve_dependencies", solved_on_second_try)
        patch_fastapi_resolution_plan()
        solved: list[Any] = []

        with _root_results(solved), TestClient(app) as client:
            assert client.get("/").status_code == 422

        (result,) = solved
        assert result.values == {"again": 5, "later": "cached"}
        assert len(result.errors) == 1
        assert called == ["listing"]

    @pytest.mark.parametrize("scope", ["request", "function"])
    def test_yield_dependencies_exit_in_the_same_order_when_the_endpoint_raises(
        self, scope: str
    ) -> None:
        events: list[str] = []

        def build() -> FastAPI:
            def outer() -> Iterator[str]:
                events.append("enter outer")
                try:
                    yield "outer"
                except HTTPException:
                    events.append("outer saw the error")
                    raise
                finally:
                    events.append("exit outer")

            async def inner(
                value: Annotated[str, _depends(outer, scope=scope)],
            ) -> AsyncIterator[str]:
                events.append("enter inner")
                try:
                    yield f"inner({value})"
                finally:
                    events.append("exit inner")

            app = FastAPI()

            @app.get("/")
            async def endpoint(
                value: Annotated[str, _depends(inner, scope=scope)],
            ) -> str:
                raise HTTPException(status_code=418, detail=value)

            return app

        unpatched = _run(build, [_get("/")])
        unpatched_events = events.copy()
        events.clear()

        patch_fastapi_resolution_plan()

        assert _run(build, [_get("/")]) == unpatched
        assert events == unpatched_events
        assert unpatched[0][0] == 418

    def test_a_dependency_raising_before_its_yield_exits_the_others(self) -> None:
        events: list[str] = []

        def build() -> FastAPI:
            def first() -> Iterator[str]:
                events.append("enter first")
                try:
                    yield "first"
                finally:
                    events.append("exit first")

            def broken(value: Annotated[str, Depends(first)]) -> Iterator[str]:
                raise HTTPException(status_code=409, detail=value)
                yield value  # pragma: no cover

            app = FastAPI()

            @app.get("/")
            async def endpoint(value: Annotated[str, Depends(broken)]) -> str:
                return value  # pragma: no cover

            return app

        unpatched = _run(build, [_get("/")])
        unpatched_events = events.copy()
        events.clear()

        patch_fastapi_resolution_plan()

        assert _run(build, [_get("/")]) == unpatched
        assert events == unpatched_events == ["enter first", "exit first"]

    def test_use_cache_false_under_a_repeated_dependency_is_called_like_the_skip(
        self,
    ) -> None:
        built: list[str] = []

        def build() -> FastAPI:
            def fresh() -> str:
                built.append("fresh")
                return "fresh"

            def cached(value: Annotated[str, Depends(fresh, use_cache=False)]) -> str:
                return value

            app = FastAPI()

            @app.get("/")
            async def endpoint(
                first: Annotated[str, Depends(cached)],
                second: Annotated[str, Depends(cached)],
            ) -> list[str]:
                return [first, second]

            return app

        patch_fastapi_solve_dependencies()
        _run(build, [_get("/")])
        skipped = built.copy()
        built.clear()
        unpatch_fastapi_solve_dependencies()

        patch_fastapi_resolution_plan()
        _run(build, [_get("/")])

        assert built == skipped == ["fresh"]

    def test_websocket_routes(self) -> None:
        built: list[str] = []

        def build() -> FastAPI:
            def db() -> str:
                built.append("db")
                return "db"

            def repo(dal: Annotated[str, Depends(db)]) -> str:
                return f"repo({dal})"

            app = FastAPI()

            @app.websocket("/ws")
            async def ws(
                websocket: WebSocket,
                repository: Annotated[str, Depends(repo)],
                dal: Annotated[str, Depends(db)],
            ) -> None:
                await websocket.accept()
                await websocket.send_json([repository, dal])
                await websocket.close()

            return app

        def receive() -> Any:
            with (
                TestClient(build()) as client,
                client.websocket_connect("/ws") as websocket,
            ):
                return websocket.receive_json()

        unpatched = receive()
        patch_fastapi_resolution_plan()

        assert receive() == unpatched == ["repo(db)", "db"]
        assert built == ["db", "db"]

    def test_http_connection_parameter_of_a_dependency(self) -> None:
        def build() -> FastAPI:
            def client_path(connection: HTTPConnection) -> str:
                return connection.url.path

            app = FastAPI()

            @app.get("/where")
            async def endpoint(path: Annotated[str, Depends(client_path)]) -> str:
                return path

            return app

        _same_with_and_without_plan(build, _get("/where"))

    def test_routes_of_included_routers(
        self, recursive_solves: list[Callable[..., Any]]
    ) -> None:
        events: list[str] = []

        def build() -> FastAPI:
            app = FastAPI()
            inner = APIRouter(dependencies=[Depends(_record(events, "inner"))])
            inner.include_router(_reference_app(events).router, prefix="/ref")
            outer = APIRouter(prefix="/outer")
            outer.include_router(inner)
            app.include_router(outer)
            return app

        requests = [_get("/outer/ref/items/1?q=2"), _get("/outer/ref/items/oops")]
        unpatched = _run(build, requests)
        unpatched_events = events.copy()
        events.clear()

        patch_fastapi_resolution_plan()
        recursive_solves.clear()

        assert _run(build, requests) == unpatched
        assert events == unpatched_events
        assert {getattr(call, "__name__", None) for call in recursive_solves} <= {
            "repo",
            "db",
            "Settings",
        }

    @requires_route_groups
    def test_route_group_dependencies(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("<html></html>")
        built: list[str] = []

        def build() -> FastAPI:
            def db() -> str:
                built.append("db")
                return "db"

            def repo(dal: Annotated[str, Depends(db)]) -> str:
                return dal

            router = APIRouter(dependencies=[Depends(repo), Depends(db)])
            router.frontend("/", directory=tmp_path)
            app = FastAPI()
            app.include_router(router)
            return app

        unpatched = _run(build, [_get("/")])
        unpatched_built = built.copy()
        built.clear()

        patch_fastapi_resolution_plan()

        assert _run(build, [_get("/")]) == unpatched
        assert built == unpatched_built == ["db"]


def _record(events: list[str], name: str) -> Callable[[], None]:
    def dependency() -> None:
        events.append(name)

    return dependency


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


class TestOverrides:
    def test_each_request_sees_the_overrides_of_its_time(
        self, recursive_solves: list[Callable[..., Any]]
    ) -> None:
        built: list[str] = []
        app = _shared_tree(built)
        db = next(
            sub.call
            for route in app.routes
            if isinstance(route, APIRoute)
            for sub in route.dependant.dependencies
            if sub.name == "dal"
        )
        patch_fastapi_resolution_plan()
        recursive_solves.clear()

        with TestClient(app) as client:
            plain = client.get("/").json()
            app.dependency_overrides[db] = lambda: "fake"
            overridden = client.get("/").json()
            app.dependency_overrides[db] = lambda: "other"
            changed = client.get("/").json()
            app.dependency_overrides.clear()
            removed = client.get("/").json()

        assert plain == removed == ["service(repo(db), db)", "repo(db)", "db"]
        assert overridden == ["service(repo(fake), fake)", "repo(fake)", "fake"]
        assert changed == ["service(repo(other), other)", "repo(other)", "other"]
        assert recursive_solves == []

    def test_same_results_as_fastapi_under_overrides(self) -> None:
        def build() -> FastAPI:
            def query(x: int = 1) -> int:
                return x

            def needs_param(value: Annotated[int, Depends(query)]) -> int:
                return value

            def direct() -> str:
                return "direct"

            def overridden() -> str:
                return "overridden"

            def scopes(security_scopes: SecurityScopes) -> list[str]:
                return sorted(security_scopes.scopes)

            app = FastAPI()

            @app.get("/both")
            async def both(
                over: Annotated[str, Depends(overridden)],
                plain: Annotated[str, Depends(direct)],
            ) -> list[str]:
                return [over, plain]

            @app.get("/no-cache")
            async def no_cache(
                cached: Annotated[int, Depends(needs_param)],
                fresh: Annotated[int, Depends(needs_param, use_cache=False)],
            ) -> list[int]:
                return [cached, fresh]

            @app.get("/scopes")
            async def with_scopes(
                plain: Annotated[int, Depends(needs_param)],
                scoped: Annotated[int, Security(needs_param, scopes=["s"])],
                granted: Annotated[list[str], Security(scopes, scopes=["t"])],
            ) -> list[Any]:
                return [plain, scoped, granted]

            app.dependency_overrides[overridden] = direct
            return app

        _same_with_and_without_plan(
            build, _get("/both"), _get("/no-cache?x=5"), _get("/scopes?x=oops")
        )

    def test_reference_tree_under_an_unrelated_override(self) -> None:
        events: list[str] = []

        def build() -> FastAPI:
            app = _reference_app(events)
            app.dependency_overrides[_unrelated] = lambda: "fake"
            return app

        unpatched = _run(build, _REFERENCE_REQUESTS)
        unpatched_events = events.copy()
        events.clear()

        patch_fastapi_resolution_plan()

        assert _run(build, _REFERENCE_REQUESTS) == unpatched
        assert events == unpatched_events

    def test_dependants_shared_by_the_deps_cache(self) -> None:
        def build() -> FastAPI:
            def leaf() -> int:
                return 1

            def middle(value: Annotated[int, Depends(leaf)]) -> int:
                return value + 1

            app = FastAPI()
            with fastapi_deps_cache():

                @app.get("/")
                async def endpoint(
                    cached: Annotated[int, Depends(middle)],
                    fresh: Annotated[int, Depends(middle, use_cache=False)],
                ) -> list[int]:
                    return [cached, fresh]

            app.dependency_overrides[_unrelated] = lambda: "fake"
            return app

        shared = {
            id(sub.dependencies[0]) for sub in _route_of(build()).dependant.dependencies
        }
        assert len(shared) == 1

        _same_with_and_without_plan(build, _get("/"))


def _unrelated() -> str:
    return "real"


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


@pytest.fixture
def compilations(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    compiled: list[Any] = []
    compile_plan = plan_module.compile_plan

    def counting(root: Any, overrides: Any) -> Any:
        compiled.append(dict(overrides))
        return compile_plan(root, overrides)

    monkeypatch.setattr(plan_module, "compile_plan", counting)
    return compiled


class TestCompilation:
    def test_a_route_is_compiled_once(self, compilations: list[Any]) -> None:
        app = _shared_tree([])
        patch_fastapi_resolution_plan()

        with TestClient(app) as client:
            for _ in range(3):
                client.get("/")

        assert compilations == [{}]

    def test_a_route_is_compiled_again_only_when_overrides_change(
        self, compilations: list[Any]
    ) -> None:
        app = _shared_tree([])
        patch_fastapi_resolution_plan()

        def fake() -> str:
            return "fake"

        with TestClient(app) as client:
            client.get("/")
            app.dependency_overrides[_unrelated] = fake
            client.get("/")
            client.get("/")
            app.dependency_overrides[_unrelated] = _unrelated
            client.get("/")
            app.dependency_overrides.clear()
            client.get("/")

        assert compilations == [{}, {_unrelated: fake}, {_unrelated: _unrelated}]

    def test_routes_without_an_app_are_solved_by_fastapi(
        self, recursive_solves: list[Callable[..., Any]]
    ) -> None:
        def build() -> FastAPI:
            def db() -> str:
                return "db"

            router = APIRouter()

            @router.get("/")
            async def endpoint(dal: Annotated[str, Depends(db)]) -> str:
                return dal

            app = FastAPI()
            app.mount("/router", router)
            return app

        def get() -> Any:
            with TestClient(build()) as client:
                return client.get("/router/").json()

        unpatched = get()
        recursive_solves.clear()
        patch_fastapi_resolution_plan()

        assert get() == unpatched == "db"
        assert [call.__name__ for call in recursive_solves] == ["endpoint", "db"]

    def test_no_plan_without_an_app_to_store_it_on(self) -> None:
        app = _shared_tree([])
        (route,) = (r for r in app.routes if isinstance(r, APIRoute))

        assert plan_module._plan_for(route.dependant, None) is None
        assert plan_module._plan_for(route.dependant, app) is not None


# ---------------------------------------------------------------------------
# Memory: a plan keeps only what it reads
# ---------------------------------------------------------------------------


def _route_tree(root: Dependant) -> set[int]:
    found: set[int] = set()
    pending = [root]
    while pending:
        dependant = pending.pop()
        if id(dependant) not in found:
            found.add(id(dependant))
            pending.extend(dependant.dependencies)
    return found


def _retained_dependants(plan: Any, root: Dependant) -> list[Dependant]:
    """The ``Dependant`` objects the plan keeps alive outside the route's tree.

    The walk stops at callables, classes and modules: what they reference
    lives as long as the code itself, not because of the plan.
    """
    route = _route_tree(root)
    retained: list[Dependant] = []
    seen: set[int] = set()
    pending: list[Any] = [plan]
    while pending:
        obj = pending.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, Dependant):
            if id(obj) not in route:
                retained.append(obj)
            continue
        if inspect.isroutine(obj) or inspect.isclass(obj) or inspect.ismodule(obj):
            continue
        pending.extend(gc.get_referents(obj))
    return retained


def _route_of(app: FastAPI) -> APIRoute:
    (route,) = (r for r in app.routes if isinstance(r, APIRoute))
    return route


class TestMemory:
    @pytest.mark.parametrize("overridden", [False, True])
    def test_plan_retains_no_dependant_outside_the_route_tree(
        self, overridden: bool
    ) -> None:
        app, _ = _bench_app()
        root = _route_of(app).dependant
        overrides = {_unrelated: lambda: "fake"} if overridden else {}

        plan = plan_module.compile_plan(root, overrides)

        assert _retained_dependants(plan, root) == []

    def test_an_overriding_generator_keeps_one_bare_dependant(self) -> None:
        def session() -> Iterator[str]:
            yield "session"

        async def fake_session() -> AsyncIterator[str]:
            yield "fake"

        def repo(sess: Annotated[str, Depends(session)]) -> str:
            return sess

        def other(sess: Annotated[str, Depends(session)]) -> str:
            return sess

        app = FastAPI()

        @app.get("/")
        async def endpoint(
            first: Annotated[str, Depends(repo)],
            second: Annotated[str, Depends(other)],
        ) -> list[str]:
            return [first, second]

        root = _route_of(app).dependant
        plan = plan_module.compile_plan(root, {session: fake_session})

        (retained,) = _retained_dependants(plan, root)
        assert retained.call is fake_session
        assert retained.dependencies == []


class TestOverriddenGenerators:
    def test_same_results_as_fastapi(self) -> None:
        events: list[str] = []

        def build() -> FastAPI:
            def session() -> Iterator[str]:
                events.append("enter session")
                yield "session"
                events.append("exit session")

            async def fake_session(q: int) -> AsyncIterator[str]:
                events.append(f"enter fake {q}")
                try:
                    yield f"fake {q}"
                finally:
                    events.append(f"exit fake {q}")

            def repo(sess: Annotated[str, Depends(session)]) -> str:
                events.append("repo")
                return f"repo({sess})"

            app = FastAPI()

            @app.get("/")
            async def endpoint(
                repository: Annotated[str, Depends(repo)],
                sess: Annotated[str, _depends(session, scope="function")],
                again: Annotated[str, Depends(repo)],
            ) -> list[str]:
                return [repository, sess, again]

            app.dependency_overrides[session] = fake_session
            return app

        requests = [_get("/?q=1"), _get("/"), _get("/?q=oops")]
        unpatched = _run(build, requests)
        unpatched_events = events.copy()
        events.clear()

        patch_fastapi_resolution_plan()

        assert _run(build, requests) == unpatched
        assert events == unpatched_events
        assert unpatched[0][0] == 200
        assert unpatched[1][0] == 422


# ---------------------------------------------------------------------------
# Guards against FastAPI internals changing
# ---------------------------------------------------------------------------


class TestGuards:
    def test_root_call_parameters(self) -> None:
        # Fails if FastAPI changes the parameters of ``solve_dependencies``: the
        # plan would then ignore or drop one.
        parameters = set(inspect.signature(_FASTAPI_SOLVE_DEPENDENCIES).parameters)

        assert parameters - {"_uses_scopes_cache"} == {
            *plan_module._ROOT_KWARGS,
            "dependency_cache",
        }

    def test_no_request_parameter_extracts_nothing(self) -> None:
        # The plan skips the extraction of a request parameter source a
        # dependant has no field for.
        assert _dep_utils.request_params_to_args([], {"x": "1"}) == ({}, [])

    def test_route_handlers_call_the_routing_global(
        self, recursive_solves: list[Callable[..., Any]]
    ) -> None:
        # Fails if FastAPI stops resolving ``fastapi.routing.solve_dependencies``
        # at request time: the plan would then silently never run.
        patch_fastapi_resolution_plan()

        assert _get_json(_shared_tree([]), "/") == [
            "service(repo(db), db)",
            "repo(db)",
            "db",
        ]
        assert recursive_solves == []


# ---------------------------------------------------------------------------
# Benchmark gate
# ---------------------------------------------------------------------------

# The plan must at least halve the time FastAPI takes to solve a route, with the
# sub-tree skip of patch_fastapi_solve_dependencies already active. Both are
# measured on the same machine, so the ratio does not depend on runner speed.
_REQUIRED_PLAN_SPEEDUP = 2.0
_BENCH_UNIQUE_DEPENDENCIES = 100
_BENCH_ROOT_DEPENDENCIES = 10
_BENCH_ROUNDS = 5
_BENCH_SOLVES_PER_ROUND = 200


def _bench_app() -> tuple[FastAPI, int]:
    """A route on a random DAG of async dependencies, and its edge count.

    Same shape as the busiest production routes: about 100 unique dependencies
    and 190 edges, each shared dependency reached at several depths.
    """
    rng = random.Random(7)
    nodes: list[Callable[..., Any]] = []
    edges = 0
    for index in range(_BENCH_UNIQUE_DEPENDENCIES):
        parents = rng.sample(nodes, k=min(len(nodes), rng.choice((1, 2, 2, 3))))
        edges += len(parents)
        nodes.append(_bench_node(index, parents))
    roots = nodes[-_BENCH_ROOT_DEPENDENCIES:]
    app = FastAPI()
    app.add_api_route("/", _bench_node(-1, roots))
    return app, edges + len(roots)


def _bench_node(index: int, parents: list[Callable[..., Any]]) -> Callable[..., Any]:
    async def node(**_: Any) -> int:
        return index

    node.__signature__ = inspect.Signature(
        [
            inspect.Parameter(
                f"p{position}", inspect.Parameter.KEYWORD_ONLY, default=Depends(parent)
            )
            for position, parent in enumerate(parents)
        ]
    )
    return node


async def _best_solve_time(app: FastAPI) -> float:
    (route,) = (r for r in app.routes if isinstance(r, APIRoute))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "path_params": {},
        }
    )
    best = float("inf")
    async with AsyncExitStack() as request_stack, AsyncExitStack() as function_stack:
        request.scope["fastapi_inner_astack"] = request_stack
        request.scope["fastapi_function_astack"] = function_stack
        for _ in range(_BENCH_ROUNDS):
            start = time.perf_counter()
            for _ in range(_BENCH_SOLVES_PER_ROUND):
                solved = await _routing.solve_dependencies(
                    request=request,
                    dependant=route.dependant,
                    dependency_overrides_provider=app,
                    async_exit_stack=request_stack,
                    embed_body_fields=False,
                )
                assert not solved.errors
            best = min(best, time.perf_counter() - start)
    return best / _BENCH_SOLVES_PER_ROUND


@pytest.mark.benchmark
@pytest.mark.timing_sensitive
class TestResolutionPlanGate:
    def test_plan_solves_a_production_sized_route_at_least_twice_as_fast(
        self,
    ) -> None:
        app, edges = _bench_app()
        assert edges >= 190

        patch_fastapi_solve_dependencies()
        skipping = asyncio.run(_best_solve_time(app))
        patch_fastapi_resolution_plan()
        planned = asyncio.run(_best_solve_time(app))

        speedup = skipping / planned
        assert speedup >= _REQUIRED_PLAN_SPEEDUP, (
            f"resolution plan speedup {speedup:.1f}x is below the required "
            f"{_REQUIRED_PLAN_SPEEDUP}x (plan={planned * 1e6:.0f}us "
            f"skip={skipping * 1e6:.0f}us per request)"
        )
