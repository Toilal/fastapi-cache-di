"""Cached sub-dependencies are not walked again at request time."""

import asyncio
import dataclasses
import inspect
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Annotated, Any

import fastapi.dependencies.utils as _dep_utils
import fastapi.routing as _routing
import httpx
import pytest
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Response,
    Security,
    WebSocket,
)
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute
from fastapi.security import SecurityScopes
from fastapi.testclient import TestClient

from fastapi_cache_di import (
    fastapi_deps_cache,
    patch_fastapi_deps_cache,
    patch_fastapi_solve_dependencies,
    unpatch_fastapi_deps_cache,
    unpatch_fastapi_solve_dependencies,
)
from fastapi_cache_di import patch as patch_module

_FASTAPI_SOLVE_DEPENDENCIES = _dep_utils.solve_dependencies

requires_dependency_scope = pytest.mark.skipif(
    "scope" not in inspect.signature(Depends).parameters,
    reason="Depends(scope=...) arrived in FastAPI 0.121",
)


@pytest.fixture(autouse=True)
def _ensure_unpatched() -> Iterator[None]:
    unpatch_fastapi_solve_dependencies()
    unpatch_fastapi_deps_cache()
    yield
    unpatch_fastapi_solve_dependencies()
    unpatch_fastapi_deps_cache()


@pytest.fixture
def walks() -> Iterator[list[Callable[..., Any]]]:
    """The call of every dependant FastAPI's own ``solve_dependencies`` walks.

    The spy is installed in both modules before patching, so the patch captures
    it as the original it delegates to.
    """
    calls: list[Callable[..., Any]] = []

    async def spy(**kwargs: Any) -> Any:
        calls.append(kwargs["dependant"].call)
        return await _FASTAPI_SOLVE_DEPENDENCIES(**kwargs)

    _dep_utils.solve_dependencies = spy
    _routing.solve_dependencies = spy
    try:
        yield calls
    finally:
        unpatch_fastapi_solve_dependencies()
        _dep_utils.solve_dependencies = _FASTAPI_SOLVE_DEPENDENCIES
        _routing.solve_dependencies = _FASTAPI_SOLVE_DEPENDENCIES


def _responses(build: Callable[[], FastAPI], *paths: str) -> list[Any]:
    """Status, body and headers of each GET, on a freshly built app."""
    with TestClient(build()) as client:
        responses = [client.get(path) for path in paths]
    return [(r.status_code, r.json(), dict(r.headers)) for r in responses]


def _same_with_and_without_patch(build: Callable[[], FastAPI], *paths: str) -> None:
    unpatched = _responses(build, *paths)
    patch_fastapi_solve_dependencies()
    assert _responses(build, *paths) == unpatched


# ---------------------------------------------------------------------------
# A dependency shared at several depths
# ---------------------------------------------------------------------------


def _unrelated() -> str:
    return "real"


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


# ---------------------------------------------------------------------------
# Patch / unpatch
# ---------------------------------------------------------------------------


class TestPatchUnpatch:
    def test_patch_then_unpatch_report_whether_they_acted(self) -> None:
        assert patch_fastapi_solve_dependencies() is True
        assert patch_fastapi_solve_dependencies() is False
        assert unpatch_fastapi_solve_dependencies() is True
        assert unpatch_fastapi_solve_dependencies() is False

    def test_patch_replaces_both_patch_points(self) -> None:
        patch_fastapi_solve_dependencies()

        assert _dep_utils.solve_dependencies is not _FASTAPI_SOLVE_DEPENDENCIES
        assert _routing.solve_dependencies is _dep_utils.solve_dependencies

    def test_unpatch_restores_the_original_of_each_module(self) -> None:
        async def other(**kwargs: Any) -> Any:
            return await _FASTAPI_SOLVE_DEPENDENCIES(**kwargs)

        _routing.solve_dependencies = other
        try:
            patch_fastapi_solve_dependencies()
            unpatch_fastapi_solve_dependencies()

            assert _dep_utils.solve_dependencies is _FASTAPI_SOLVE_DEPENDENCIES
            assert _routing.solve_dependencies is other
        finally:
            _routing.solve_dependencies = _FASTAPI_SOLVE_DEPENDENCIES

    @pytest.mark.parametrize("module", [_dep_utils, _routing])
    def test_patch_fails_loudly_when_fastapi_drops_a_patch_point(
        self, module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(module, "solve_dependencies")

        with pytest.raises(RuntimeError, match="not supported"):
            patch_fastapi_solve_dependencies()

        assert patch_module._original_solve_dependencies is None
        for patch_point in (_dep_utils, _routing):
            assert (
                getattr(patch_point, "solve_dependencies", _FASTAPI_SOLVE_DEPENDENCIES)
                is _FASTAPI_SOLVE_DEPENDENCIES
            )

    def test_independent_of_the_deps_cache_pair(self) -> None:
        patch_fastapi_solve_dependencies()
        patched = _dep_utils.solve_dependencies

        assert patch_fastapi_deps_cache() is True
        assert unpatch_fastapi_deps_cache() is True
        assert _dep_utils.solve_dependencies is patched

        assert unpatch_fastapi_solve_dependencies() is True
        assert patch_fastapi_deps_cache() is True
        assert patch_fastapi_solve_dependencies() is True
        assert unpatch_fastapi_solve_dependencies() is True
        assert unpatch_fastapi_deps_cache() is True

    def test_independent_of_the_deps_cache_context_manager(self) -> None:
        patch_fastapi_solve_dependencies()
        patched = _dep_utils.solve_dependencies

        with fastapi_deps_cache():
            assert _dep_utils.solve_dependencies is patched
        assert _dep_utils.solve_dependencies is patched


# ---------------------------------------------------------------------------
# Fewer walks, each dependency called once
# ---------------------------------------------------------------------------


class TestWalks:
    def test_shared_dependency_is_walked_and_called_once_per_request(
        self, walks: list[Callable[..., Any]]
    ) -> None:
        built: list[str] = []
        app = _shared_tree(built)

        _responses(lambda: app, "/")
        unpatched_walks = len(walks)
        walks.clear()

        patch_fastapi_solve_dependencies()
        _responses(lambda: app, "/")

        names = [getattr(call, "__name__", None) for call in walks]
        assert names == ["endpoint", "service", "repo", "db"]
        assert len(walks) < unpatched_walks
        assert built == ["db", "db"]

    def test_overrides_do_not_disable_the_skip(
        self, walks: list[Callable[..., Any]]
    ) -> None:
        built: list[str] = []
        app = _shared_tree(built)
        app.dependency_overrides[_unrelated] = lambda: "fake"

        unpatched = _responses(lambda: app, "/")
        unpatched_walks = len(walks)
        walks.clear()
        built.clear()

        patch_fastapi_solve_dependencies()

        assert _responses(lambda: app, "/") == unpatched
        names = [getattr(call, "__name__", None) for call in walks]
        assert names == ["endpoint", "service", "repo", "db"]
        assert len(walks) < unpatched_walks
        assert built == ["db"]

    def test_overridden_shared_dependency_is_walked_and_called_once(
        self, walks: list[Callable[..., Any]]
    ) -> None:
        built: list[str] = []

        def db() -> str:
            built.append("db")
            return "db"

        def fake_db() -> str:
            built.append("fake")
            return "fake"

        def repo(dal: Annotated[str, Depends(db)]) -> str:
            return f"repo({dal})"

        app = FastAPI()

        @app.get("/")
        async def endpoint(
            repository: Annotated[str, Depends(repo)],
            dal: Annotated[str, Depends(db)],
        ) -> list[str]:
            return [repository, dal]

        app.dependency_overrides[db] = fake_db

        unpatched = _responses(lambda: app, "/")
        unpatched_walks = len(walks)
        walks.clear()
        built.clear()

        patch_fastapi_solve_dependencies()

        assert _responses(lambda: app, "/") == unpatched
        assert len(walks) < unpatched_walks
        assert built == ["fake"]

    def test_use_cache_false_is_still_called_at_every_occurrence(self) -> None:
        built: list[object] = []

        def fresh() -> object:
            built.append(object())
            return built[-1]

        def holder(value: Annotated[object, Depends(fresh, use_cache=False)]) -> int:
            return id(value)

        app = FastAPI()

        @app.get("/")
        async def endpoint(
            held: Annotated[int, Depends(holder)],
            value: Annotated[object, Depends(fresh, use_cache=False)],
        ) -> bool:
            return held != id(value)

        patch_fastapi_solve_dependencies()

        assert _responses(lambda: app, "/")[0][1] is True
        assert len(built) == 2

    def test_use_cache_false_under_a_cached_dependency_is_not_called_again(
        self,
    ) -> None:
        built: list[str] = []

        def fresh() -> str:
            built.append("fresh")
            return "fresh"

        def cached(value: Annotated[str, Depends(fresh, use_cache=False)]) -> str:
            return value

        def build() -> FastAPI:
            app = FastAPI()

            @app.get("/")
            async def endpoint(
                first: Annotated[str, Depends(cached)],
                second: Annotated[str, Depends(cached)],
            ) -> list[str]:
                return [first, second]

            return app

        _responses(build, "/")
        assert len(built) == 2

        built.clear()
        patch_fastapi_solve_dependencies()
        _responses(build, "/")
        assert len(built) == 1


# ---------------------------------------------------------------------------
# Same values and errors as without the patch
# ---------------------------------------------------------------------------


class TestSameResults:
    @requires_dependency_scope
    def test_generator_dependencies_of_both_scopes(self) -> None:
        events: list[str] = []

        def build() -> FastAPI:
            async def resource() -> AsyncIterator[str]:
                events.append("enter resource")
                yield "resource"
                events.append("exit resource")

            def session() -> Iterator[str]:
                events.append("enter session")
                yield "session"
                events.append("exit session")

            def user(
                res: Annotated[str, Depends(resource, scope="function")],
                sess: Annotated[str, Depends(session, scope="request")],
            ) -> str:
                return f"{res}+{sess}"

            app = FastAPI()

            @app.get("/")
            async def endpoint(
                first: Annotated[str, Depends(user)],
                res: Annotated[str, Depends(resource, scope="function")],
                sess: Annotated[str, Depends(session, scope="request")],
            ) -> list[str]:
                return [first, res, sess]

            return app

        unpatched = _responses(build, "/")
        unpatched_events = events.copy()
        events.clear()

        patch_fastapi_solve_dependencies()

        assert _responses(build, "/") == unpatched
        assert events == unpatched_events

    def test_generator_dependencies(self) -> None:
        events: list[str] = []

        def build() -> FastAPI:
            def session() -> Iterator[str]:
                events.append("enter")
                yield "session"
                events.append("exit")

            def repo(sess: Annotated[str, Depends(session)]) -> str:
                return sess

            app = FastAPI()

            @app.get("/")
            async def endpoint(
                repository: Annotated[str, Depends(repo)],
                sess: Annotated[str, Depends(session)],
            ) -> list[str]:
                return [repository, sess]

            return app

        unpatched = _responses(build, "/")
        assert events == ["enter", "exit"]
        events.clear()

        patch_fastapi_solve_dependencies()

        assert _responses(build, "/") == unpatched
        assert events == ["enter", "exit"]

    def test_security_scopes(self) -> None:
        def build() -> FastAPI:
            def scopes(security_scopes: SecurityScopes) -> list[str]:
                return sorted(security_scopes.scopes)

            def plain() -> str:
                return "plain"

            def reader(
                granted: Annotated[list[str], Security(scopes, scopes=["read"])],
                value: Annotated[str, Depends(plain)],
            ) -> list[str]:
                return [*granted, value]

            app = FastAPI()

            @app.get("/")
            async def endpoint(
                read: Annotated[list[str], Depends(reader)],
                write: Annotated[list[str], Security(scopes, scopes=["write"])],
                none: Annotated[list[str], Depends(scopes)],
                admin: Annotated[list[str], Security(plain, scopes=["admin"])],
                value: Annotated[str, Depends(plain)],
            ) -> list[Any]:
                return [read, write, none, admin, value]

            return app

        _same_with_and_without_patch(build, "/")

    def test_background_tasks_and_response_under_a_shared_dependency(self) -> None:
        ran: list[str] = []

        def build() -> FastAPI:
            def tagged(response: Response, tasks: BackgroundTasks) -> str:
                response.headers["x-tagged"] = "yes"
                tasks.add_task(ran.append, "task")
                return "tagged"

            def wrapper(value: Annotated[str, Depends(tagged)]) -> str:
                return value

            app = FastAPI()

            @app.get("/")
            async def endpoint(
                wrapped: Annotated[str, Depends(wrapper)],
                value: Annotated[str, Depends(tagged)],
                tasks: BackgroundTasks,
            ) -> list[str]:
                tasks.add_task(ran.append, "endpoint task")
                return [wrapped, value]

            return app

        unpatched = _responses(build, "/")
        assert unpatched[0][2]["x-tagged"] == "yes"
        unpatched_ran = ran.copy()
        ran.clear()

        patch_fastapi_solve_dependencies()

        assert _responses(build, "/") == unpatched
        assert ran == unpatched_ran == ["task", "endpoint task"]

    def test_validation_error_in_a_shared_sub_dependency(self) -> None:
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
            ) -> list[int]:
                return [first, second]

            return app

        _same_with_and_without_patch(build, "/?size=3", "/?size=oops", "/")

    def test_dependency_overrides(self) -> None:
        def build() -> FastAPI:
            def query(x: int = 1) -> int:
                return x

            def needs_param(value: Annotated[int, Depends(query)]) -> int:
                return value

            def direct() -> str:
                return "direct"

            def overridden() -> str:
                return "overridden"

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
            async def scopes(
                plain: Annotated[int, Depends(needs_param)],
                scoped: Annotated[int, Security(needs_param, scopes=["s"])],
            ) -> list[int]:
                return [plain, scoped]

            def page(size: int) -> int:
                return size

            def listing(value: Annotated[int, Depends(page)]) -> int:
                return value

            @app.get("/page")
            async def paged(
                first: Annotated[int, Depends(listing)],
                second: Annotated[int, Depends(page)],
            ) -> list[int]:
                return [first, second]

            app.dependency_overrides[overridden] = direct
            return app

        _same_with_and_without_patch(
            build,
            "/both",
            "/no-cache?x=5",
            "/scopes?x=5",
            "/page?size=3",
            "/page?size=oops",
        )

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
        patch_fastapi_solve_dependencies()

        assert receive() == unpatched
        assert built == ["db", "db"]

    @pytest.mark.skipif(
        not hasattr(APIRouter, "frontend"),
        reason="route groups (APIRouter.frontend) are not in this FastAPI",
    )
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

        with TestClient(build()) as client:
            assert client.get("/").status_code == 200

        patch_fastapi_solve_dependencies()

        with TestClient(build()) as client:
            assert client.get("/").status_code == 200
        assert built == ["db", "db"]


# ---------------------------------------------------------------------------
# Guards against FastAPI internals changing
# ---------------------------------------------------------------------------


def _sub_dependants(dependant: Dependant) -> Iterator[Dependant]:
    for sub in dependant.dependencies:
        yield sub
        yield from _sub_dependants(sub)


class TestGuards:
    def test_recursion_resolves_the_module_global_per_call(
        self, walks: list[Callable[..., Any]]
    ) -> None:
        # Fails if FastAPI stops resolving ``solve_dependencies`` through the
        # module globals: the patch would then silently become a no-op.
        _responses(lambda: _shared_tree([]), "/")

        names = {getattr(call, "__name__", None) for call in walks}
        assert names == {"endpoint", "service", "repo", "db"}

    def test_solved_dependency_fields(self) -> None:
        fields = {
            field.name for field in dataclasses.fields(_dep_utils.SolvedDependency)
        }

        assert fields == {
            "values",
            "errors",
            "background_tasks",
            "response",
            "dependency_cache",
        }

    def test_cache_key_is_the_one_fastapi_stores_values_under(self) -> None:
        # Fails if FastAPI changes how it keys ``dependency_cache``: the patch
        # would then never find a cached value, or find the wrong one.
        caches: list[dict[Any, Any]] = []

        async def capture(**kwargs: Any) -> Any:
            solved = await _FASTAPI_SOLVE_DEPENDENCIES(**kwargs)
            caches.append(solved.dependency_cache)
            return solved

        def scopes(security_scopes: SecurityScopes) -> list[str]:
            return security_scopes.scopes

        def plain() -> str:
            return "plain"

        def reader(
            granted: Annotated[list[str], Security(scopes, scopes=["read"])],
            value: Annotated[str, Depends(plain)],
        ) -> str:
            return value

        app = FastAPI()

        @app.get("/")
        async def endpoint(
            read: Annotated[str, Depends(reader)],
            admin: Annotated[str, Security(plain, scopes=["admin"])],
            none: Annotated[list[str], Depends(scopes)],
        ) -> None:
            pass

        _routing.solve_dependencies = capture
        try:
            _responses(lambda: app, "/")
        finally:
            _routing.solve_dependencies = _FASTAPI_SOLVE_DEPENDENCIES

        (route,) = (r for r in app.routes if isinstance(r, APIRoute))
        (cache,) = caches
        keys = {
            patch_module._request_cache_key(sub, {})
            for sub in _sub_dependants(route.dependant)
        }
        assert keys == set(cache)

    def test_recursion_solves_each_sub_dependant_once_and_in_order(self) -> None:
        # Fails if FastAPI stops calling ``solve_dependencies`` exactly once per
        # sub-dependant, in the order of ``dependant.dependencies``: the patch
        # would then match a recursive call with the wrong original.
        mismatches: list[str] = []
        pending: list[Iterator[Dependant]] = []
        checked: list[str] = []

        async def check(**kwargs: Any) -> Any:
            received: Dependant = kwargs["dependant"]
            if kwargs.get("dependency_cache") is not None:
                overrides = kwargs["dependency_overrides_provider"].dependency_overrides
                expected = next(pending[-1], None)
                if (
                    expected is None
                    or expected.name != received.name
                    or overrides.get(expected.call, expected.call) is not received.call
                ):
                    mismatches.append(f"unexpected {received.name}")
                checked.append(str(received.name))
            pending.append(iter(received.dependencies))
            try:
                return await _FASTAPI_SOLVE_DEPENDENCIES(**kwargs)
            finally:
                leftover = next(pending.pop(), None)
                if leftover is not None:
                    mismatches.append(f"{leftover.name} never solved")

        def page(size: int) -> int:
            return size

        def scopes(security_scopes: SecurityScopes) -> list[str]:
            return security_scopes.scopes

        def listing(
            value: Annotated[int, Depends(page)],
            granted: Annotated[list[str], Security(scopes, scopes=["read"])],
        ) -> int:
            return value

        app = FastAPI()

        @app.get("/")
        async def endpoint(
            first: Annotated[int, Depends(listing)],
            fresh: Annotated[int, Depends(page, use_cache=False)],
            fake: Annotated[str, Depends(_unrelated)],
        ) -> None:
            pass

        app.dependency_overrides[_unrelated] = lambda: "fake"

        _dep_utils.solve_dependencies = check
        _routing.solve_dependencies = check
        try:
            _responses(lambda: app, "/?size=3", "/?size=oops")
        finally:
            _dep_utils.solve_dependencies = _FASTAPI_SOLVE_DEPENDENCIES
            _routing.solve_dependencies = _FASTAPI_SOLVE_DEPENDENCIES

        assert mismatches == []
        assert checked == ["first", "value", "granted", "fresh", "fake"] * 2


# ---------------------------------------------------------------------------
# Matching a recursive call with the caller's sub-dependant
# ---------------------------------------------------------------------------


def _leaf() -> str:
    return "leaf"


def _other_leaf() -> str:
    return "other"


def _parent(
    leaf: Annotated[str, Depends(_leaf)], other: Annotated[str, Depends(_other_leaf)]
) -> str:
    return leaf + other


def _sub_dependants_of_parent() -> list[Dependant]:
    return get_dependant(path="/", call=_parent).dependencies


class TestCallerSubDependant:
    def test_no_pending_sub_dependants(self) -> None:
        (leaf, _) = _sub_dependants_of_parent()

        assert patch_module._caller_sub_dependant(leaf, {}) is None

    def test_next_pending_sub_dependant_is_matched(self) -> None:
        leaf, other = _sub_dependants_of_parent()
        token = patch_module._pending_sub_dependants.set(iter([leaf, other]))
        try:
            assert patch_module._caller_sub_dependant(leaf, {}) is leaf
            assert patch_module._caller_sub_dependant(other, {}) is other
            assert patch_module._caller_sub_dependant(other, {}) is None
        finally:
            patch_module._pending_sub_dependants.reset(token)

    def test_rebuilt_copy_of_an_overridden_dependency_is_matched(self) -> None:
        (leaf, _) = _sub_dependants_of_parent()
        rebuilt = get_dependant(path="/", call=_other_leaf, name=leaf.name)
        token = patch_module._pending_sub_dependants.set(iter([leaf]))
        try:
            matched = patch_module._caller_sub_dependant(rebuilt, {_leaf: _other_leaf})
        finally:
            patch_module._pending_sub_dependants.reset(token)

        assert matched is leaf

    @pytest.mark.parametrize(
        ("name", "call"),
        [("other", _leaf), ("leaf", _other_leaf)],
        ids=["name differs", "call differs"],
    )
    def test_mismatch_is_not_matched(self, name: str, call: Callable[..., Any]) -> None:
        (leaf, _) = _sub_dependants_of_parent()
        received = get_dependant(path="/", call=call, name=name)
        token = patch_module._pending_sub_dependants.set(iter([leaf]))
        try:
            assert patch_module._caller_sub_dependant(received, {}) is None
        finally:
            patch_module._pending_sub_dependants.reset(token)

    def test_root_call_does_not_consume_pending_sub_dependants(self) -> None:
        leaf, other = _sub_dependants_of_parent()
        roots: list[Dependant] = []

        async def solve(**kwargs: Any) -> None:
            roots.append(kwargs["dependant"])

        pending = iter([leaf, other])
        token = patch_module._pending_sub_dependants.set(pending)
        patch_module._original_solve_dependencies = solve
        try:
            root = get_dependant(path="/", call=_parent)
            asyncio.run(
                patch_module._solve_dependencies_skipping_cached(dependant=root)
            )
        finally:
            patch_module._original_solve_dependencies = None
            patch_module._pending_sub_dependants.reset(token)

        assert roots == [root]
        assert next(pending) is leaf

    def test_pending_sub_dependants_are_restored_when_solving_fails(self) -> None:
        async def solve(**kwargs: Any) -> None:
            raise RuntimeError("boom")

        async def scenario() -> Any:
            root = get_dependant(path="/", call=_parent)
            with pytest.raises(RuntimeError, match="boom"):
                await patch_module._solve_dependencies_skipping_cached(dependant=root)
            return patch_module._pending_sub_dependants.get()

        patch_module._original_solve_dependencies = solve
        try:
            assert asyncio.run(scenario()) is None
        finally:
            patch_module._original_solve_dependencies = None

    def test_concurrent_requests_do_not_share_pending_sub_dependants(
        self, walks: list[Callable[..., Any]]
    ) -> None:
        # Both requests pause inside a dependency body, mid-walk; the first one
        # then finishes its walk while the second is still paused, so the walks
        # interleave instead of nesting.
        async def scenario() -> list[Any]:
            arrived: list[str] = []
            both_arrived = asyncio.Event()

            async def db() -> str:
                return "db"

            async def repo(dal: Annotated[str, Depends(db)]) -> str:
                return f"repo({dal})"

            async def wait_for_the_other_request(
                dal: Annotated[str, Depends(db)],
            ) -> str:
                arrived.append(dal)
                if len(arrived) == 2:
                    both_arrived.set()
                    await asyncio.sleep(0.05)
                await asyncio.wait_for(both_arrived.wait(), timeout=5)
                return dal

            app = FastAPI()

            @app.get("/a")
            async def endpoint_a(
                waited: Annotated[str, Depends(wait_for_the_other_request)],
                repository: Annotated[str, Depends(repo)],
                dal: Annotated[str, Depends(db)],
            ) -> list[str]:
                return [waited, repository, dal]

            @app.get("/b")
            async def endpoint_b(
                waited: Annotated[str, Depends(wait_for_the_other_request)],
                dal: Annotated[str, Depends(db)],
            ) -> list[str]:
                return [waited, dal]

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                responses = await asyncio.gather(client.get("/a"), client.get("/b"))
            return [r.json() for r in responses]

        patch_fastapi_solve_dependencies()

        assert asyncio.run(scenario()) == [["db", "repo(db)", "db"], ["db", "db"]]
        request_a = ["endpoint_a", "wait_for_the_other_request", "db", "repo"]
        request_b = ["endpoint_b", "wait_for_the_other_request", "db"]
        names = sorted(getattr(call, "__name__", "") for call in walks)
        assert names == sorted([*request_a, *request_b])
