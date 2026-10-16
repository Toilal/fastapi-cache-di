"""Loop-safe dependencies called inline instead of through the threadpool."""

import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any

import fastapi.dependencies.utils as _dep_utils
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient

from fastapi_cache_di import (
    fastapi_deps_cache,
    loop_safe,
    patch_fastapi_deps_cache,
    patch_fastapi_run_in_threadpool,
    unpatch_fastapi_deps_cache,
    unpatch_fastapi_run_in_threadpool,
)
from fastapi_cache_di import patch as patch_module

_STARLETTE_RUN_IN_THREADPOOL = _dep_utils.run_in_threadpool


@pytest.fixture(autouse=True)
def _ensure_unpatched() -> Iterator[None]:
    unpatch_fastapi_run_in_threadpool()
    unpatch_fastapi_deps_cache()
    yield
    unpatch_fastapi_run_in_threadpool()
    unpatch_fastapi_deps_cache()


@pytest.fixture
def delegated() -> Iterator[list[Callable[..., Any]]]:
    """Every callable that reaches FastAPI's own ``run_in_threadpool``.

    The spy is installed as the module global before patching, so the patch
    captures it as the original it delegates to.
    """
    calls: list[Callable[..., Any]] = []

    async def spy(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        calls.append(func)
        return await _STARLETTE_RUN_IN_THREADPOOL(func, *args, **kwargs)

    _dep_utils.run_in_threadpool = spy  # type: ignore[attr-defined]
    try:
        yield calls
    finally:
        unpatch_fastapi_run_in_threadpool()
        _dep_utils.run_in_threadpool = _STARLETTE_RUN_IN_THREADPOOL  # type: ignore[attr-defined]


class Plain:
    def __init__(self) -> None:
        self.thread = threading.get_ident()


@loop_safe
class Marked:
    def __init__(self) -> None:
        self.thread = threading.get_ident()


class UnmarkedChild(Marked):
    def __init__(self) -> None:
        self.thread = threading.get_ident()


def plain_fn() -> int:
    return threading.get_ident()


@loop_safe
def marked_fn() -> int:
    return threading.get_ident()


def sync_gen() -> Iterator[int]:
    yield threading.get_ident()


def _get(app: FastAPI, path: str) -> Any:
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# AC-1: patch / unpatch
# ---------------------------------------------------------------------------


class TestPatchUnpatch:
    def test_patch_then_unpatch_report_whether_they_acted(self) -> None:
        assert patch_fastapi_run_in_threadpool() is True
        assert patch_fastapi_run_in_threadpool() is False
        assert unpatch_fastapi_run_in_threadpool() is True
        assert unpatch_fastapi_run_in_threadpool() is False

    def test_unpatch_restores_the_original(self) -> None:
        original = _dep_utils.run_in_threadpool

        patch_fastapi_run_in_threadpool()
        assert _dep_utils.run_in_threadpool is not original

        unpatch_fastapi_run_in_threadpool()
        assert _dep_utils.run_in_threadpool is original

    def test_repatching_with_another_option_is_ignored_with_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        patch_fastapi_run_in_threadpool(inline_classes=True)

        with caplog.at_level(logging.WARNING, logger=patch_module.__name__):
            assert patch_fastapi_run_in_threadpool(inline_classes=False) is False

        assert "inline_classes=True" in caplog.text
        assert patch_module._inline_classes is True

    def test_patch_fails_loudly_when_fastapi_drops_the_global(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(_dep_utils, "run_in_threadpool")

        with pytest.raises(RuntimeError, match="not supported"):
            patch_fastapi_run_in_threadpool()

        assert not hasattr(_dep_utils, "run_in_threadpool")
        assert patch_module._original_run_in_threadpool is None

    def test_unpatch_resets_inline_classes(self) -> None:
        patch_fastapi_run_in_threadpool(inline_classes=True)
        unpatch_fastapi_run_in_threadpool()

        assert patch_module._inline_classes is False

    def test_independent_of_the_deps_cache_pair(self) -> None:
        patch_fastapi_run_in_threadpool()
        patched = _dep_utils.run_in_threadpool

        assert patch_fastapi_deps_cache() is True
        assert unpatch_fastapi_deps_cache() is True
        assert _dep_utils.run_in_threadpool is patched

        assert unpatch_fastapi_run_in_threadpool() is True
        assert patch_fastapi_deps_cache() is True
        assert patch_fastapi_run_in_threadpool() is True
        assert unpatch_fastapi_run_in_threadpool() is True
        assert unpatch_fastapi_deps_cache() is True

    def test_independent_of_the_deps_cache_context_manager(self) -> None:
        patch_fastapi_run_in_threadpool()
        patched = _dep_utils.run_in_threadpool

        with fastapi_deps_cache():
            assert _dep_utils.run_in_threadpool is patched
        assert _dep_utils.run_in_threadpool is patched


# ---------------------------------------------------------------------------
# AC-3 / AC-7: which calls leave the threadpool
# ---------------------------------------------------------------------------


def _threads_app() -> FastAPI:
    app = FastAPI()

    @app.get("/deps")
    async def deps(
        plain: Annotated[Plain, Depends()],
        marked: Annotated[Marked, Depends()],
        child: Annotated[UnmarkedChild, Depends()],
        plain_thread: Annotated[int, Depends(plain_fn)],
        marked_thread: Annotated[int, Depends(marked_fn)],
        gen_thread: Annotated[int, Depends(sync_gen)],
    ) -> dict[str, bool]:
        loop = threading.get_ident()
        return {
            "plain": plain.thread == loop,
            "marked": marked.thread == loop,
            "child": child.thread == loop,
            "plain_fn": plain_thread == loop,
            "marked_fn": marked_thread == loop,
            "sync_gen": gen_thread == loop,
        }

    @app.get("/sync-endpoint")
    def sync_endpoint(loop: Annotated[int, Depends(_loop_thread)]) -> bool:
        return threading.get_ident() == loop

    return app


async def _loop_thread() -> int:
    return threading.get_ident()


class TestDispatch:
    def test_unpatched_everything_runs_in_the_threadpool(self) -> None:
        on_loop = _get(_threads_app(), "/deps")

        assert not any(on_loop.values())

    def test_marked_callables_run_on_the_loop(
        self, delegated: list[Callable[..., Any]]
    ) -> None:
        patch_fastapi_run_in_threadpool()
        on_loop = _get(_threads_app(), "/deps")

        assert on_loop == {
            "plain": False,
            "marked": True,
            "child": False,
            "plain_fn": False,
            "marked_fn": True,
            "sync_gen": False,
        }
        assert set(delegated) == {Plain, UnmarkedChild, plain_fn}

    def test_inline_classes_runs_every_class_on_the_loop(
        self, delegated: list[Callable[..., Any]]
    ) -> None:
        patch_fastapi_run_in_threadpool(inline_classes=True)
        on_loop = _get(_threads_app(), "/deps")

        assert on_loop == {
            "plain": True,
            "marked": True,
            "child": True,
            "plain_fn": False,
            "marked_fn": True,
            "sync_gen": False,
        }
        assert set(delegated) == {plain_fn}

    def test_sync_endpoint_stays_in_the_threadpool(self) -> None:
        patch_fastapi_run_in_threadpool(inline_classes=True)

        assert _get(_threads_app(), "/sync-endpoint") is False

    def test_guard_fastapi_resolves_the_module_global_per_call(
        self, delegated: list[Callable[..., Any]]
    ) -> None:
        # Fails if a FastAPI upgrade stops awaiting sync dependencies through
        # ``fastapi.dependencies.utils.run_in_threadpool``: the patch would then
        # silently become a no-op.
        app = FastAPI()

        @app.get("/")
        async def endpoint(_: Annotated[int, Depends(plain_fn)]) -> None:
            pass

        _get(app, "/")

        assert delegated == [plain_fn]


# ---------------------------------------------------------------------------
# AC-4: per-request dependency cache
# ---------------------------------------------------------------------------


class TestPerRequestCache:
    def test_shared_class_is_built_once_per_request(self) -> None:
        built: list[object] = []

        class Dal:
            def __init__(self) -> None:
                built.append(self)

        class Repo:
            def __init__(self, dal: Annotated[Dal, Depends()]) -> None:
                self.dal = dal

        class Service:
            def __init__(
                self,
                repo: Annotated[Repo, Depends()],
                dal: Annotated[Dal, Depends()],
            ) -> None:
                self.repo = repo
                self.dal = dal

        app = FastAPI()

        @app.get("/")
        async def endpoint(
            service: Annotated[Service, Depends()],
            dal: Annotated[Dal, Depends()],
        ) -> bool:
            return service.dal is dal and service.repo.dal is dal

        patch_fastapi_run_in_threadpool(inline_classes=True)
        with TestClient(app) as client:
            assert client.get("/").json() is True
            assert client.get("/").json() is True

        assert len(built) == 2

    def test_use_cache_false_is_called_at_every_occurrence(self) -> None:
        built: list[object] = []

        class Fresh:
            def __init__(self) -> None:
                built.append(self)

        class Holder:
            def __init__(
                self, fresh: Annotated[Fresh, Depends(use_cache=False)]
            ) -> None:
                self.fresh = fresh

        app = FastAPI()

        @app.get("/")
        async def endpoint(
            holder: Annotated[Holder, Depends()],
            fresh: Annotated[Fresh, Depends(use_cache=False)],
        ) -> bool:
            return holder.fresh is not fresh

        patch_fastapi_run_in_threadpool(inline_classes=True)

        assert _get(app, "/") is True
        assert len(built) == 2


# ---------------------------------------------------------------------------
# AC-5: dependency_overrides
# ---------------------------------------------------------------------------


class _Original:
    def __init__(self) -> None:
        self.name = "original"


class _Override:
    def __init__(self) -> None:
        self.name = "override"


@loop_safe
def _marked_original() -> str:
    return "original"


@loop_safe
def _marked_override() -> str:
    return "override"


def _unmarked_override() -> int:
    return threading.get_ident()


def _direct_dep() -> str:
    return "direct"


def _overridden_dep() -> str:
    return "overridden"


def _overrides_app() -> FastAPI:
    app = FastAPI()

    @app.get("/class")
    async def by_class(dep: Annotated[_Original, Depends()]) -> str:
        return dep.name

    @app.get("/marked")
    async def by_marked(dep: Annotated[str, Depends(_marked_original)]) -> str:
        return dep

    @app.get("/both")
    async def both(
        overridden: Annotated[str, Depends(_overridden_dep)],
        direct: Annotated[str, Depends(_direct_dep)],
    ) -> list[str]:
        return [overridden, direct]

    app.dependency_overrides = {
        _Original: _Override,
        _marked_original: _marked_override,
        _overridden_dep: _direct_dep,
    }
    return app


class TestOverrides:
    @pytest.mark.parametrize("path", ["/class", "/marked", "/both"])
    def test_injected_values_match_the_unpatched_app(self, path: str) -> None:
        unpatched = _get(_overrides_app(), path)

        patch_fastapi_run_in_threadpool(inline_classes=True)

        assert _get(_overrides_app(), path) == unpatched

    def test_unmarked_override_of_a_marked_original_uses_the_threadpool(
        self, delegated: list[Callable[..., Any]]
    ) -> None:
        app = FastAPI()

        @app.get("/")
        async def endpoint(thread: Annotated[int, Depends(_marked_original)]) -> bool:
            return thread == threading.get_ident()

        app.dependency_overrides[_marked_original] = _unmarked_override
        patch_fastapi_run_in_threadpool(inline_classes=True)

        assert _get(app, "/") is False
        assert delegated == [_unmarked_override]


# ---------------------------------------------------------------------------
# AC-6: errors, WebSocket, route groups
# ---------------------------------------------------------------------------


class _Boom:
    def __init__(self) -> None:
        raise ValueError("boom")


class _Forbidden:
    def __init__(self) -> None:
        raise HTTPException(status_code=403, detail="nope")


def _errors_app() -> FastAPI:
    app = FastAPI()

    @app.get("/boom")
    async def boom(_: Annotated[_Boom, Depends()]) -> None:
        pass

    @app.get("/forbidden")
    async def forbidden(_: Annotated[_Forbidden, Depends()]) -> None:
        pass

    return app


class TestErrorsAndRouteKinds:
    @pytest.mark.parametrize("patched", [False, True])
    def test_exception_from_init_propagates(self, patched: bool) -> None:
        if patched:
            patch_fastapi_run_in_threadpool(inline_classes=True)

        with (
            TestClient(_errors_app()) as client,
            pytest.raises(ValueError, match="boom"),
        ):
            client.get("/boom")

    @pytest.mark.parametrize("patched", [False, True])
    def test_http_exception_from_init_becomes_a_response(self, patched: bool) -> None:
        if patched:
            patch_fastapi_run_in_threadpool(inline_classes=True)

        with TestClient(_errors_app()) as client:
            response = client.get("/forbidden")

        assert response.status_code == 403
        assert response.json() == {"detail": "nope"}

    def test_websocket_dependencies_run_on_the_loop(self) -> None:
        app = FastAPI()

        @app.websocket("/ws")
        async def ws(websocket: WebSocket, dep: Annotated[Plain, Depends()]) -> None:
            await websocket.accept()
            await websocket.send_json(dep.thread == threading.get_ident())
            await websocket.close()

        patch_fastapi_run_in_threadpool(inline_classes=True)

        with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
            assert websocket.receive_json() is True

    @pytest.mark.skipif(
        not hasattr(APIRouter, "frontend"),
        reason="route groups (APIRouter.frontend) are not in this FastAPI",
    )
    def test_route_group_dependencies_run_on_the_loop(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("<html></html>")
        threads: dict[str, int] = {}

        class GroupDep:
            def __init__(self) -> None:
                threads["dep"] = threading.get_ident()

        async def loop_thread() -> None:
            threads["loop"] = threading.get_ident()

        router = APIRouter(dependencies=[Depends(loop_thread), Depends(GroupDep)])
        router.frontend("/", directory=tmp_path)
        app = FastAPI()
        app.include_router(router)

        patch_fastapi_run_in_threadpool(inline_classes=True)

        with TestClient(app) as client:
            assert client.get("/").status_code == 200

        assert threads["dep"] == threads["loop"]
