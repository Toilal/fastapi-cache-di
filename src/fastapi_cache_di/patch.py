"""Monkeypatch FastAPI's dependency introspection to cache expensive operations.

FastAPI calls ``get_typed_signature`` and ``get_dependant`` for every
``Depends()`` in every route handler at startup — with **no caching**.
Shared dependencies such as an auth check or a DB handle are re-introspected
tens of thousands of times.

This module caches up to three functions:

- ``get_typed_signature``: saves repeated signature parsing.
- ``get_dependant``: eliminates redundant recursive introspection of the
  dependency tree.
- ``get_flat_dependant`` (FastAPI <= 0.140 only, removed in 0.141): the
  **dominant startup cost** on those versions. FastAPI calls it once per route
  in ``APIRoute.__init__`` to flatten the dependency tree, and it recurses
  through every sub-dependency without caching. Shared sub-dependencies (auth,
  DB, roles ...) are re-traversed from scratch for every route. Caching the
  recursive results by ``(id(dependant), parent_oauth_scopes)`` turns
  O(routes * tree-depth) into O(routes + unique-deps). From 0.141, FastAPI
  flattens the tree with iterative walks that build no ``Dependant``, so only
  the first two functions are cached.

Call :func:`patch_fastapi_deps_cache` **once** before the app loads its routes.
Call :func:`unpatch_fastapi_deps_cache` after route loading to restore originals
and free memory — or use the :func:`fastapi_deps_cache` context manager.

.. warning::
   All patch state lives in module globals (originals, active cache, the modules
   whose ``get_flat_dependant`` was patched). Patching swaps FastAPI module attributes
   process-wide; it is neither thread-safe nor safe to interleave with route
   loading on other threads. Use ``patch``/``unpatch`` (and the context manager)
   from a single thread during startup only. Hit/miss counters live on the
   :class:`~fastapi_cache_di.cache.DepsCache` instead (so a caller-owned cache
   keeps them after unpatch); re-entrancy of the context manager is handled by
   :func:`patch_fastapi_deps_cache` returning ``False`` when already active.

Separately, :func:`patch_fastapi_run_in_threadpool` cuts a **per-request**
cost: FastAPI calls every sync, non-generator dependency (every class, every
plain ``def``) through ``run_in_threadpool``, a thread hop that costs far more
than a trivial ``__init__``. The patch replaces the module global
``fastapi.dependencies.utils.run_in_threadpool`` so that loop-safe callables
(see :mod:`fastapi_cache_di.execution`) are called inline on the event loop.
Unlike the deps cache, it is meant to stay active for the app's lifetime, and
it is independent of it: either pair can be used without the other.

:func:`patch_fastapi_solve_dependencies` cuts another per-request cost:
``solve_dependencies`` walks the whole sub-tree of every sub-dependency before
checking whether its value is already in the request's ``dependency_cache``, so a
dependency shared across a deep tree is re-solved at every occurrence. The patch
returns early for a sub-dependency whose value is already cached; the caller then
reuses that value through its own cache check. It is a lifetime patch too, and
independent of both other pairs.

:func:`patch_fastapi_resolution_plan` goes further: it compiles each route's
dependency tree once into a flat plan
(:mod:`fastapi_cache_di.plan`) and solves every request from it, without
walking the tree. It shares ``fastapi.routing.solve_dependencies`` with
:func:`patch_fastapi_solve_dependencies`, so either can be removed first.
"""

import copy
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import ModuleType
from typing import Any

import fastapi
import fastapi.dependencies.utils as _dep_utils
import fastapi.openapi.utils as _openapi_utils
import fastapi.routing as _routing
from fastapi.dependencies.models import Dependant
from fastapi.utils import get_path_param_names
from starlette.concurrency import run_in_threadpool

from fastapi_cache_di import plan as _plan
from fastapi_cache_di.cache import DepsCache
from fastapi_cache_di.execution import is_loop_safe
from fastapi_cache_di.plan import request_cache_key as _request_cache_key

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State: originals + active cache (hit/miss counters live on the DepsCache)
# ---------------------------------------------------------------------------

_original_get_typed_signature: Callable[..., inspect.Signature] | None = None
_original_get_dependant: Callable[..., Dependant] | None = None
_original_get_flat_dependant: Callable[..., Dependant] | None = None

_active_cache: DepsCache | None = None
_owns_cache = False
_patched_flat_modules: tuple[ModuleType, ...] = ()

# Every module that may hold its own ``get_flat_dependant`` reference: utils
# (self-recursive calls), openapi, and routing (from FastAPI 0.112.4). None of
# them has it from FastAPI 0.141 onwards.
_FLAT_DEPENDANT_MODULES = (_dep_utils, _openapi_utils, _routing)


# ---------------------------------------------------------------------------
# get_typed_signature cache
# ---------------------------------------------------------------------------


def _cached_get_typed_signature(call: Callable[..., Any]) -> inspect.Signature:
    assert _original_get_typed_signature is not None
    assert _active_cache is not None

    key = id(call)
    cached = _active_cache.signatures.get(key)
    if cached is not None:
        _active_cache.sig_hits += 1
        return cached

    _active_cache.sig_misses += 1
    result = _original_get_typed_signature(call)
    _active_cache.signatures[key] = result
    # Pin ``call`` so its address (the cache key) cannot be recycled by the GC
    # and later mis-served to a different callable allocated at the same id.
    _active_cache.keep_alive(call)
    return result


# ---------------------------------------------------------------------------
# get_dependant cache
# ---------------------------------------------------------------------------


def _sorted_stable(items: Any) -> list[Any]:
    """Sort ``items`` deterministically, tolerating non-orderable elements.

    Falls back to ordering by ``repr`` when the elements are mutually
    incomparable (e.g. a heterogeneous ``set``), so the caller never raises.
    """
    materialized = list(items)
    try:
        return sorted(materialized)
    except TypeError:
        return sorted(materialized, key=repr)


def _hashable(value: Any) -> Any:
    """Coerce a kwarg value into something usable inside a cache key.

    Today ``get_dependant``'s extra kwargs are scope-related (``str`` and
    ``list``), all already hashable once lists become tuples. This stays
    defensive against any future unhashable kwarg value: ``list``/``tuple`` are
    recursed element-wise, ``set``/``frozenset`` become a stably-sorted tuple,
    and ``dict`` a stably-sorted tuple of coerced items. Anything still
    unhashable falls back to its ``repr`` so key construction never raises.
    """
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return tuple(_sorted_stable(_hashable(v) for v in value))
    if isinstance(value, dict):
        return tuple(_sorted_stable((k, _hashable(v)) for k, v in value.items()))
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _cached_get_dependant(
    *,
    path: str,
    call: Callable[..., Any],
    name: str | None = None,
    use_cache: bool = True,
    **kwargs: Any,
) -> Dependant:
    # The scope-related keyword arguments of ``get_dependant`` changed across
    # FastAPI versions (``security_scopes`` -> ``own_oauth_scopes`` /
    # ``parent_oauth_scopes``, plus ``scope``). We capture them generically via
    # ``**kwargs`` so the wrapper stays compatible, and fold them into the key.
    assert _original_get_dependant is not None
    assert _active_cache is not None

    extra = tuple(sorted((k, _hashable(v)) for k, v in kwargs.items()))
    cache_key = (
        id(call),
        frozenset(get_path_param_names(path)),
        name,
        use_cache,
        extra,
    )

    # The top-level path-operation dependant (``name is None``) is mutated in
    # place by FastAPI: ``_build_dependant_with_parameterless_dependencies``
    # inserts each route's ``dependencies=[...]`` straight into
    # ``dependant.dependencies``. Handing back the shared cached object would
    # leak one route's dependencies into every sibling reusing the same
    # endpoint callable. Return a copy so that mutation targets a private list.
    # Named sub-dependants (``name`` set) are never mutated and must keep their
    # cached identity (relied on by the recursive sub-dependency sharing), so
    # they are returned as-is.
    #
    # The key folds in only the path *param names*, not the literal path string,
    # so two routes with the same param names but different literals (``/a/{id}``
    # vs ``/b/{id}``) deliberately share one cached ``Dependant`` — this is what
    # makes cross-route sharing effective. A consequence is that the shared
    # object's ``.path`` reflects whichever route populated it first. FastAPI
    # reads ``.path`` only for error/endpoint diagnostics, never for routing, so
    # the staleness is cosmetic (a wrong path in a log line, not a wrong route).
    cached = _active_cache.dependants.get(cache_key)
    if cached is not None:
        _active_cache.dep_hits += 1
        return _copy_lists(cached, _MUTATED_LIST_FIELDS) if name is None else cached

    _active_cache.dep_misses += 1
    result = _original_get_dependant(
        path=path, call=call, name=name, use_cache=use_cache, **kwargs
    )
    _active_cache.dependants[cache_key] = result
    # ``id(call)`` is part of ``cache_key``; pin it against GC address reuse.
    _active_cache.keep_alive(call)
    return _copy_lists(result, _MUTATED_LIST_FIELDS) if name is None else result


# ---------------------------------------------------------------------------
# get_flat_dependant cache
# ---------------------------------------------------------------------------


_FLAT_LIST_FIELDS = (
    "path_params",
    "query_params",
    "header_params",
    "cookie_params",
    "body_params",
    "dependencies",
)

# The only lists FastAPI mutates on a dependant after ``get_dependant`` returns:
# ``dependencies`` (route-level parameterless deps inserted in routing) and, on
# FastAPI < 0.121, ``security_requirements`` (appended in ``get_sub_dependant``).
_MUTATED_LIST_FIELDS = ("dependencies", "security_requirements")


def _copy_lists(d: Dependant, fields: tuple[str, ...]) -> Dependant:
    """Shallow-copy ``d`` then give it private copies of ``fields``.

    ``copy.copy`` shares every attribute with ``d``; re-copying the named list
    fields lets a caller mutate them without touching the cached instance, while
    sub-``Dependant`` identity is preserved. Fields absent on a given FastAPI
    version are skipped, so the same call is valid across versions and slot-safe.

    Only the list *containers* are private: their elements (``ModelField`` and
    sub-``Dependant`` objects) remain shared references. Appending to or
    reordering a returned list is safe; mutating an element in place is not — it
    would leak into every route sharing that cached element.
    """
    clone = copy.copy(d)
    for attr in fields:
        value = getattr(clone, attr, None)
        if isinstance(value, list):
            setattr(clone, attr, value.copy())
    return clone


def _cached_get_flat_dependant(
    dependant: Dependant,
    *,
    skip_repeats: bool = False,
    **kwargs: Any,
) -> Dependant:
    # ``visited`` and (in newer FastAPI) ``parent_oauth_scopes`` are captured
    # via ``**kwargs`` and passed straight through, keeping this wrapper valid
    # across FastAPI versions with different signatures.
    assert _original_get_flat_dependant is not None
    assert _active_cache is not None

    # Only cache when skip_repeats=False (the hot path from route construction).
    # skip_repeats=True (used by OpenAPI generation) depends on the mutable
    # ``visited`` list and is not a startup bottleneck.
    if skip_repeats:
        return _original_get_flat_dependant(
            dependant, skip_repeats=skip_repeats, **kwargs
        )

    parent_oauth_scopes = kwargs.get("parent_oauth_scopes")
    cache_key = (
        id(dependant),
        tuple(parent_oauth_scopes) if parent_oauth_scopes else (),
    )

    cached = _active_cache.flat_dependants.get(cache_key)
    if cached is not None:
        _active_cache.flat_hits += 1
        return _copy_lists(cached, _FLAT_LIST_FIELDS)

    _active_cache.flat_misses += 1
    result = _original_get_flat_dependant(dependant, skip_repeats=False, **kwargs)
    _active_cache.flat_dependants[cache_key] = result
    # ``id(dependant)`` is part of ``cache_key``; pin it against GC address reuse.
    _active_cache.keep_alive(dependant)
    return _copy_lists(result, _FLAT_LIST_FIELDS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def patch_fastapi_deps_cache(
    deps_cache: DepsCache | None = None,
) -> bool:
    """Install caching wrappers for ``get_typed_signature``, ``get_dependant``
    and, on FastAPI <= 0.140, ``get_flat_dependant``.

    Parameters
    ----------
    deps_cache:
        A :class:`DepsCache` instance to use as backing store. If ``None``
        (default), a new cache is created internally.

    Returns ``True`` on the first call, ``False`` if already patched.
    """
    global _original_get_typed_signature, _original_get_dependant
    global _original_get_flat_dependant, _active_cache, _owns_cache

    if _original_get_typed_signature is not None:
        if deps_cache is not None and deps_cache is not _active_cache:
            logger.warning(
                "patch_fastapi_deps_cache: caching is already active; the "
                "deps_cache argument was ignored and the existing cache is "
                "kept. Unpatch before patching with a different cache."
            )
        return False  # already patched

    _owns_cache = deps_cache is None
    _active_cache = deps_cache if deps_cache is not None else DepsCache()

    # Cache get_typed_signature (module-level is enough — only called from utils)
    _original_get_typed_signature = _dep_utils.get_typed_signature
    _dep_utils.get_typed_signature = _cached_get_typed_signature

    # Cache get_dependant — patch both the utils module (for recursive calls)
    # and the routing module (which imports its own reference).
    _original_get_dependant = _dep_utils.get_dependant
    _dep_utils.get_dependant = _cached_get_dependant
    _routing.get_dependant = _cached_get_dependant  # type: ignore[attr-defined]

    # Cache get_flat_dependant — patch every module that holds a reference to
    # it, and remember which ones (for unpatch). Nothing is patched from FastAPI
    # 0.141 onwards, where the function no longer exists: setting the attribute
    # anyway would inject a name FastAPI does not define.
    #
    # The openapi patch is effectively a no-op: both call sites
    # (``fastapi/openapi/utils.py``) pass ``skip_repeats=True``, which the
    # wrapper short-circuits straight to the original without caching. It is
    # installed for symmetry and as a safety net, so that any FastAPI version (or
    # third-party caller) reaching ``openapi.utils.get_flat_dependant`` with
    # ``skip_repeats=False`` still routes through the cache rather than diverging.
    global _patched_flat_modules
    if hasattr(_dep_utils, "get_flat_dependant"):
        _original_get_flat_dependant = _dep_utils.get_flat_dependant
        _patched_flat_modules = tuple(
            module
            for module in _FLAT_DEPENDANT_MODULES
            if hasattr(module, "get_flat_dependant")
        )
        for module in _patched_flat_modules:
            module.get_flat_dependant = _cached_get_flat_dependant  # type: ignore[attr-defined]

    return True


def unpatch_fastapi_deps_cache() -> bool:
    """Restore originals and free cache memory.

    Returns ``True`` if unpatched, ``False`` if not currently patched.
    """
    global _original_get_typed_signature, _original_get_dependant
    global _original_get_flat_dependant, _active_cache, _owns_cache
    global _patched_flat_modules

    if _original_get_typed_signature is None:
        return False

    assert _original_get_dependant is not None
    assert _active_cache is not None

    stats = [
        (
            "get_typed_signature",
            _active_cache.sig_hits,
            _active_cache.sig_misses,
            len(_active_cache.signatures),
        ),
        (
            "get_dependant",
            _active_cache.dep_hits,
            _active_cache.dep_misses,
            len(_active_cache.dependants),
        ),
    ]
    if _patched_flat_modules:
        stats.append(
            (
                "get_flat_dependant",
                _active_cache.flat_hits,
                _active_cache.flat_misses,
                len(_active_cache.flat_dependants),
            )
        )
    logger.info(
        "FastAPI deps cache: %s — %s.",
        ", ".join(
            f"{name} {hits} hits / {misses} misses ({entries} entries)"
            for name, hits, misses, entries in stats
        ),
        "cleared" if _owns_cache else "kept (caller-owned)",
    )

    _dep_utils.get_typed_signature = _original_get_typed_signature
    _dep_utils.get_dependant = _original_get_dependant
    _routing.get_dependant = _original_get_dependant  # type: ignore[attr-defined]
    for module in _patched_flat_modules:
        module.get_flat_dependant = _original_get_flat_dependant  # type: ignore[attr-defined]
    _patched_flat_modules = ()

    _original_get_typed_signature = None
    _original_get_dependant = None
    _original_get_flat_dependant = None

    # An internally-created cache is dropped with the module reference (GC). A
    # caller-owned cache is left populated (entries and counters) for post-hoc
    # inspection.
    if _owns_cache:
        _active_cache.clear()
    _active_cache = None
    _owns_cache = False

    return True


@contextmanager
def fastapi_deps_cache(
    *,
    deps_cache: DepsCache | bool = True,
) -> Iterator[None]:
    """Context manager that enables dependency caching for the block's duration.

    Parameters
    ----------
    deps_cache:
        - ``True`` (default): use a temporary cache scoped to this context.
        - ``False``: no-op, caching is disabled.
        - A :class:`DepsCache` instance: use (and populate) a shared cache.

    Usage::

        with fastapi_deps_cache():
            app.include_router(...)  # or however routes are loaded

        # Or with a shared cache:
        cache = DepsCache()
        with fastapi_deps_cache(deps_cache=cache):
            app.include_router(...)
    """
    if deps_cache is False:
        yield
        return
    dc = deps_cache if isinstance(deps_cache, DepsCache) else None
    # Only tear down what this context installed. patch_fastapi_deps_cache is a
    # no-op returning False when caching is already active (a nested context or
    # an earlier manual patch), so a nested/inner block must not unpatch and
    # destroy the still-wanted outer cache.
    patched = patch_fastapi_deps_cache(deps_cache=dc)
    try:
        yield
    finally:
        if patched:
            unpatch_fastapi_deps_cache()


# ---------------------------------------------------------------------------
# run_in_threadpool: call loop-safe dependencies inline
# ---------------------------------------------------------------------------

_original_run_in_threadpool: Callable[..., Awaitable[Any]] | None = None
_inline_classes = False


async def _run_in_threadpool_unless_loop_safe(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    # ``func`` is the effective call, after ``dependency_overrides``: an
    # override is judged on its own marker, never on the original's.
    if (_inline_classes and inspect.isclass(func)) or is_loop_safe(func):
        return func(*args, **kwargs)
    # A request already past the global lookup when unpatch runs still lands
    # here; Starlette's function is what FastAPI would have called anyway.
    original = _original_run_in_threadpool or run_in_threadpool
    return await original(func, *args, **kwargs)


def patch_fastapi_run_in_threadpool(*, inline_classes: bool = False) -> bool:
    """Call loop-safe sync dependencies directly on the event loop.

    Replaces ``fastapi.dependencies.utils.run_in_threadpool``, which FastAPI
    awaits for every sync, non-generator dependency at request time. A callable
    marked with :func:`~fastapi_cache_di.execution.loop_safe` is then called
    inline; everything else still goes through the threadpool. Sync generator
    dependencies and sync endpoints never go through this global and are not
    affected.

    Parameters
    ----------
    inline_classes:
        Treat every class as loop-safe, marked or not. Only enable it when no
        injected class does I/O, takes a lock or computes heavily in its
        ``__init__``: a blocking call inline stalls every in-flight request.

    Returns ``True`` on the first call, ``False`` if already patched. Raises
    :class:`RuntimeError` on a FastAPI that no longer calls dependencies through
    this global, rather than silently losing the optimization.
    """
    global _original_run_in_threadpool, _inline_classes

    if not hasattr(_dep_utils, "run_in_threadpool"):
        raise RuntimeError(
            f"patch_fastapi_run_in_threadpool: FastAPI {fastapi.__version__} no "
            "longer calls dependencies through "
            "fastapi.dependencies.utils.run_in_threadpool; this version is not "
            "supported. Remove the patch or pin a supported FastAPI version."
        )

    if _original_run_in_threadpool is not None:
        if inline_classes != _inline_classes:
            logger.warning(
                "patch_fastapi_run_in_threadpool: already active with "
                "inline_classes=%s; the new value was ignored. Unpatch before "
                "patching with a different value.",
                _inline_classes,
            )
        return False

    _original_run_in_threadpool = _dep_utils.run_in_threadpool
    _inline_classes = inline_classes
    _dep_utils.run_in_threadpool = _run_in_threadpool_unless_loop_safe
    return True


def unpatch_fastapi_run_in_threadpool() -> bool:
    """Restore FastAPI's ``run_in_threadpool`` for dependencies.

    Returns ``True`` if unpatched, ``False`` if not currently patched.
    """
    global _original_run_in_threadpool, _inline_classes

    if _original_run_in_threadpool is None:
        return False

    _dep_utils.run_in_threadpool = _original_run_in_threadpool  # type: ignore[attr-defined,assignment]
    _original_run_in_threadpool = None
    _inline_classes = False
    return True


# ---------------------------------------------------------------------------
# solve_dependencies: skip re-walking the sub-tree of cached dependencies
# ---------------------------------------------------------------------------

_original_solve_dependencies: Callable[..., Awaitable[Any]] | None = None


# The sub-dependants of the dependant being solved, not yet handed to the
# recursion. FastAPI calls ``solve_dependencies`` exactly once per
# sub-dependant, in the order of ``dependant.dependencies``, so each recursive
# call takes the next one.
_pending_sub_dependants: ContextVar[Iterator[Dependant] | None] = ContextVar(
    "fastapi_cache_di_pending_sub_dependants", default=None
)


def _caller_sub_dependant(
    received: Dependant, overrides: Mapping[Any, Any]
) -> Dependant | None:
    """The sub-dependant the caller checks its cache with, when it can be matched.

    With overrides set, FastAPI hands every recursive call a ``Dependant``
    rebuilt from that sub-dependant: the copy always has ``use_cache=True`` and
    drops its own ``Security`` scopes, so only the original carries the key the
    caller looks up.
    """
    pending = _pending_sub_dependants.get()
    if pending is None:
        return None
    original = next(pending, None)
    if original is None or original.name != received.name:
        return None
    if overrides.get(original.call, original.call) is not received.call:
        return None
    return original


async def _solve_dependencies_skipping_cached(**kwargs: Any) -> Any:
    received: Dependant = kwargs["dependant"]
    cache = kwargs.get("dependency_cache")
    # Only recursive calls get a cache: a root call must not consume the
    # pending sub-dependants of a solve it happens to run inside.
    if cache is not None:
        provider = kwargs.get("dependency_overrides_provider")
        overrides = getattr(provider, "dependency_overrides", None) or {}
        original = _caller_sub_dependant(received, overrides)
        if (
            original is not None
            and original.use_cache
            and _request_cache_key(original, kwargs.get("_uses_scopes_cache")) in cache
        ):
            return _dep_utils.SolvedDependency(
                values={},
                errors=[],
                background_tasks=kwargs.get("background_tasks"),
                response=kwargs["response"],
                dependency_cache=cache,
            )
    # A request already past the global lookup when unpatch runs still lands
    # here; the global then holds FastAPI's function again.
    solve = _original_solve_dependencies or _dep_utils.solve_dependencies
    token = _pending_sub_dependants.set(iter(received.dependencies))
    try:
        return await solve(**kwargs)
    finally:
        _pending_sub_dependants.reset(token)


def patch_fastapi_solve_dependencies() -> bool:
    """Skip re-walking the sub-tree of dependencies already solved for a request.

    Replaces ``solve_dependencies`` in ``fastapi.dependencies.utils``, whose
    recursion looks this module global up at every call, and in
    ``fastapi.routing``, which imports it by name for HTTP routes, WebSocket
    routes and route groups (see :func:`_route_solve_dependencies`). A
    sub-dependency whose value is already in the request's ``dependency_cache``
    is no longer solved again: FastAPI reuses the cached value, as it already
    did after the walk. The values injected into endpoints are unchanged; a
    ``use_cache=False`` dependency nested under a cached one is no longer
    called during that discarded walk.

    Returns ``True`` on the first call, ``False`` if already patched. Raises
    :class:`RuntimeError` on a FastAPI that no longer exposes either function,
    rather than silently losing the optimization.
    """
    global _original_solve_dependencies

    _require_solve_dependencies("patch_fastapi_solve_dependencies")

    if _original_solve_dependencies is not None:
        return False

    _original_solve_dependencies = _dep_utils.solve_dependencies
    _dep_utils.solve_dependencies = _solve_dependencies_skipping_cached
    _claim_routing_solve_dependencies()
    return True


def unpatch_fastapi_solve_dependencies() -> bool:
    """Restore FastAPI's ``solve_dependencies`` in both modules.

    ``fastapi.routing`` keeps the patched function while
    :func:`patch_fastapi_resolution_plan` is active.

    Returns ``True`` if unpatched, ``False`` if not currently patched.
    """
    global _original_solve_dependencies

    if _original_solve_dependencies is None:
        return False

    _dep_utils.solve_dependencies = _original_solve_dependencies  # type: ignore[assignment]
    _original_solve_dependencies = None
    _release_routing_solve_dependencies()
    return True


def _require_solve_dependencies(caller: str, *names: str) -> None:
    missing = [
        f"{module.__name__}.solve_dependencies"
        for module in (_dep_utils, _routing)
        if not hasattr(module, "solve_dependencies")
    ]
    missing += [f"{_dep_utils.__name__}.{name}" for name in names]
    if missing:
        raise RuntimeError(
            f"{caller}: FastAPI {fastapi.__version__} no longer defines "
            f"{', '.join(missing)}; this version is not supported. Remove the "
            "patch or pin a supported FastAPI version."
        )


# ---------------------------------------------------------------------------
# fastapi.routing.solve_dependencies: one function for both patches
# ---------------------------------------------------------------------------

_original_routing_solve_dependencies: Callable[..., Awaitable[Any]] | None = None
_resolution_plan_active = False


async def _route_solve_dependencies(**kwargs: Any) -> Any:
    """The root call of every route while either per-request patch is active.

    Owning ``fastapi.routing.solve_dependencies`` jointly lets each patch be
    removed in any order without removing the other one.
    """
    if _resolution_plan_active:
        return await _plan.solve_dependencies(**kwargs)
    return await _dep_utils.solve_dependencies(**kwargs)


def _claim_routing_solve_dependencies() -> None:
    global _original_routing_solve_dependencies

    if _original_routing_solve_dependencies is None:
        _original_routing_solve_dependencies = _routing.solve_dependencies  # type: ignore[attr-defined]
        _routing.solve_dependencies = _route_solve_dependencies  # type: ignore[attr-defined]


def _release_routing_solve_dependencies() -> None:
    global _original_routing_solve_dependencies

    if _resolution_plan_active or _original_solve_dependencies is not None:
        return
    if _original_routing_solve_dependencies is not None:
        _routing.solve_dependencies = _original_routing_solve_dependencies  # type: ignore[attr-defined,assignment]
        _original_routing_solve_dependencies = None


# ---------------------------------------------------------------------------
# Resolution plan: solve each route from a plan compiled once
# ---------------------------------------------------------------------------


def patch_fastapi_resolution_plan() -> bool:
    """Solve each route's dependencies from a plan compiled on its first request.

    Replaces ``solve_dependencies`` in ``fastapi.routing``, the root call of
    HTTP routes, WebSocket routes and route groups. The first request of a
    route compiles its dependency tree into a flat plan (see
    :mod:`fastapi_cache_di.plan`); every request then runs that plan instead of
    walking the tree recursively. The plan is compiled again when
    ``dependency_overrides`` change. Values, errors, the request cache and the
    exit order of ``yield`` dependencies are FastAPI's; like
    :func:`patch_fastapi_solve_dependencies`, a ``use_cache=False`` dependency
    nested under an already solved dependency is not called again.

    Independent of the other pairs. Returns ``True`` on the first call,
    ``False`` if already patched. Raises :class:`RuntimeError` on a FastAPI
    missing a function the plan calls.
    """
    global _resolution_plan_active

    _require_solve_dependencies(
        "patch_fastapi_resolution_plan", *_plan.missing_internals()
    )

    if _resolution_plan_active:
        return False

    _resolution_plan_active = True
    _claim_routing_solve_dependencies()
    return True


def unpatch_fastapi_resolution_plan() -> bool:
    """Solve routes recursively again, as FastAPI does.

    Returns ``True`` if unpatched, ``False`` if not currently patched.
    """
    global _resolution_plan_active

    if not _resolution_plan_active:
        return False

    _resolution_plan_active = False
    _release_routing_solve_dependencies()
    return True
