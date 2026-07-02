"""Tests proving the cache reduces work (and wall-clock time) when many routes
share the same dependency tree.

The scenario mirrors a real API: a deep chain of shared service dependencies
(``head -> ... -> leaf``) injected by a large number of routes. Without the
cache, FastAPI re-flattens the whole shared chain for every route
(``O(routes * depth)``); with the cache the shared sub-tree is flattened once
and every later route is served from cache (``O(routes + depth)``).
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import fastapi.dependencies.utils as _dep_utils
import fastapi.routing as _routing
import pytest
from fastapi import Depends, FastAPI

from fastapi_cache_di import DepsCache, fastapi_deps_cache
from fastapi_cache_di import patch as patch_module

_DEPTH = 25
_N_ROUTES = 300

# The pipeline fails if caching does not speed route loading up by at least this
# factor. The assertion compares cached vs uncached on the same machine, so the
# ratio is independent of runner speed.
_REQUIRED_SPEEDUP = 3.0


def _build_shared_chain(depth: int) -> Callable[..., Any]:
    """Build a chain of ``depth`` nested dependencies and return its head.

    Every node depends on the previous one, so flattening the head walks the
    whole chain. The same node objects are reused by every route, which is what
    makes them cacheable.
    """

    def leaf() -> int:
        return 0

    head: Callable[..., Any] = leaf
    for _ in range(depth):
        prev = head

        def node(value: int = Depends(prev)) -> int:
            return value

        head = node
    return head


def _build_app(n_routes: int, head: Callable[..., Any]) -> FastAPI:
    """Register ``n_routes`` routes that all inject the shared chain head."""
    app = FastAPI()
    for i in range(n_routes):

        def endpoint(value: int = Depends(head)) -> int:
            return value

        endpoint.__name__ = f"route_{i}"
        app.add_api_route(f"/r{i}", endpoint)
    return app


def _count_flat_dependant_executions(build: Callable[[], Any]) -> int:
    """Count how many times get_flat_dependant's body runs during *build*,
    with no caching in place (the baseline)."""
    original = _dep_utils.get_flat_dependant
    count = 0

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal count
        count += 1
        return original(*args, **kwargs)

    _dep_utils.get_flat_dependant = counting
    _routing.get_flat_dependant = counting  # type: ignore[attr-defined]
    try:
        build()
    finally:
        _dep_utils.get_flat_dependant = original
        _routing.get_flat_dependant = original  # type: ignore[attr-defined]
    return count


def _cached_flat_stats(build: Callable[[], Any]) -> tuple[int, int]:
    """Return (misses, hits) for get_flat_dependant when *build* runs cached.

    ``misses`` = how many times the expensive original body actually ran.
    Read inside the context because unpatch resets the counters.
    """
    cache = DepsCache()
    with fastapi_deps_cache(deps_cache=cache):
        build()
        return patch_module._flat_misses, patch_module._flat_hits


class TestWorkElimination:
    def test_cache_runs_the_expensive_body_far_less(self) -> None:
        head = _build_shared_chain(depth=15)
        n_routes = 120

        baseline = _count_flat_dependant_executions(lambda: _build_app(n_routes, head))
        misses, hits = _cached_flat_stats(lambda: _build_app(n_routes, head))

        # Most flatten calls are served from cache instead of re-traversing.
        assert hits > 0
        assert misses < baseline
        # The redundant per-route re-traversal of the shared chain is gone:
        # the expensive body runs a small fraction of the uncached count.
        assert misses < baseline / 5, (misses, baseline)

    def test_redundant_work_grows_with_route_count_only_without_cache(self) -> None:
        head = _build_shared_chain(depth=15)

        base_small = _count_flat_dependant_executions(lambda: _build_app(10, head))
        base_large = _count_flat_dependant_executions(lambda: _build_app(100, head))

        miss_small, _ = _cached_flat_stats(lambda: _build_app(10, head))
        miss_large, _ = _cached_flat_stats(lambda: _build_app(100, head))

        # Uncached, expensive executions scale with routes x depth.
        assert base_large > base_small * 5
        # Cached, they grow far more slowly (the shared chain is done once).
        assert (miss_large / miss_small) < (base_large / base_small)


class TestWallClockBenchmark:
    """Wall-clock benchmarks (pytest-benchmark). Both live in the same group so
    ``pytest --benchmark-only`` / ``--benchmark-compare`` show the speedup of
    caching vs no caching for the same many-routes-share-a-deep-chain workload.
    """

    @pytest.mark.benchmark(group="route-loading")
    def test_uncached_route_loading(self, benchmark: Any) -> None:
        head = _build_shared_chain(_DEPTH)
        benchmark(_build_app, _N_ROUTES, head)

    @pytest.mark.benchmark(group="route-loading")
    def test_cached_route_loading(self, benchmark: Any) -> None:
        head = _build_shared_chain(_DEPTH)

        def build() -> None:
            with fastapi_deps_cache():
                _build_app(_N_ROUTES, head)

        benchmark(build)


# --- real uvicorn startup gate ---------------------------------------------

# Bigger than the in-process benchmark so that route loading dominates
# uvicorn's fixed startup overhead (interpreter + imports + event loop + socket
# bind), which is identical cached and uncached and would otherwise dilute the
# ratio. A slower CI runner only increases the ratio (route building dominates
# even more), so the 3x gate stays robust.
_STARTUP_DEPTH = 40
_STARTUP_N_ROUTES = 1200
_STARTUP_ROUNDS = 2


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _uvicorn_startup_time(*, use_cache: bool) -> float:
    """Spawn uvicorn on the benchmark app and time until it serves a request."""
    port = _free_port()
    env = {
        **os.environ,
        "BENCH_USE_CACHE": "1" if use_cache else "0",
        "BENCH_DEPTH": str(_STARTUP_DEPTH),
        "BENCH_N_ROUTES": str(_STARTUP_N_ROUTES),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "_bench_app:app",
            "--port",
            str(port),
            "--log-level",
            "critical",
        ],
        cwd=str(Path(__file__).parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    start = time.perf_counter()
    url = f"http://127.0.0.1:{port}/__ready__"
    try:
        deadline = start + 120
        while True:
            try:
                with urllib.request.urlopen(url, timeout=0.5) as resp:
                    if resp.status == 200:
                        return time.perf_counter() - start
            except OSError:
                if time.perf_counter() > deadline:
                    raise TimeoutError("uvicorn did not become ready") from None
                time.sleep(0.01)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.benchmark
class TestUvicornStartupGate:
    """Measure real uvicorn startup time and fail if the cache does not speed it
    up by at least the required factor. This is the pipeline gate."""

    @staticmethod
    def _best(use_cache: bool) -> float:
        return min(
            _uvicorn_startup_time(use_cache=use_cache) for _ in range(_STARTUP_ROUNDS)
        )

    def test_startup_speedup_meets_threshold(self) -> None:
        uncached = self._best(use_cache=False)
        cached = self._best(use_cache=True)
        speedup = uncached / cached
        assert speedup >= _REQUIRED_SPEEDUP, (
            f"uvicorn startup speedup {speedup:.2f}x is below the required "
            f"{_REQUIRED_SPEEDUP}x "
            f"(cached={cached * 1000:.0f}ms uncached={uncached * 1000:.0f}ms)"
        )
