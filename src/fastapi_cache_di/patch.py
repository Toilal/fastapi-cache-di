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

Separately, :func:`patch_fastapi_solve_dependencies` cuts a **per-request**
cost: ``solve_dependencies`` walks the whole sub-tree of every sub-dependency
before checking whether its value is already in the request's
``dependency_cache``, so a dependency shared across a deep tree is re-solved at
every occurrence. The patch returns early for a sub-dependency whose value is
already cached; the caller then reuses that value through its own cache check.
Unlike the deps cache, it is meant to stay active for the app's lifetime, and it
is independent of it: either pair can be used without the other.
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
import fastapi.dependencies.models as _dep_models
import fastapi.dependencies.utils as _dep_utils
import fastapi.openapi.utils as _openapi_utils
import fastapi.routing as _routing
from fastapi.dependencies.models import Dependant
from fastapi.utils import get_path_param_names

from fastapi_cache_di.cache import DepsCache

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
# solve_dependencies: skip re-walking the sub-tree of cached dependencies
# ---------------------------------------------------------------------------

_original_solve_dependencies: Callable[..., Awaitable[Any]] | None = None
_original_routing_solve_dependencies: Callable[..., Awaitable[Any]] | None = None

# The key FastAPI stores a solved value under: a function from FastAPI 0.140,
# ``Dependant.cache_key`` before (a dataclass field, then a ``cached_property``).
_get_cache_key: Callable[..., Any] | None = getattr(_dep_models, "_get_cache_key", None)


def _request_cache_key(dependant: Dependant, uses_scopes_cache: Any) -> Any:
    if _get_cache_key is not None:
        return _get_cache_key(dependant=dependant, uses_scopes_cache=uses_scopes_cache)
    return dependant.cache_key


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
    routes and route groups. A sub-dependency whose value is already in the
    request's ``dependency_cache`` is no longer solved again: FastAPI reuses the
    cached value, as it already did after the walk. The values injected into
    endpoints are unchanged; a ``use_cache=False`` dependency nested under a
    cached one is no longer called during that discarded walk.

    Returns ``True`` on the first call, ``False`` if already patched. Raises
    :class:`RuntimeError` on a FastAPI that no longer exposes either function,
    rather than silently losing the optimization.
    """
    global _original_solve_dependencies, _original_routing_solve_dependencies

    missing = [
        module.__name__
        for module in (_dep_utils, _routing)
        if not hasattr(module, "solve_dependencies")
    ]
    if missing:
        raise RuntimeError(
            f"patch_fastapi_solve_dependencies: FastAPI {fastapi.__version__} no "
            f"longer defines solve_dependencies in {', '.join(missing)}; this "
            "version is not supported. Remove the patch or pin a supported "
            "FastAPI version."
        )

    if _original_solve_dependencies is not None:
        return False

    _original_solve_dependencies = _dep_utils.solve_dependencies
    _original_routing_solve_dependencies = _routing.solve_dependencies  # type: ignore[attr-defined]
    _dep_utils.solve_dependencies = _solve_dependencies_skipping_cached
    _routing.solve_dependencies = _solve_dependencies_skipping_cached  # type: ignore[attr-defined]
    return True


def unpatch_fastapi_solve_dependencies() -> bool:
    """Restore FastAPI's ``solve_dependencies`` in both modules.

    Returns ``True`` if unpatched, ``False`` if not currently patched.
    """
    global _original_solve_dependencies, _original_routing_solve_dependencies

    if _original_solve_dependencies is None:
        return False

    _dep_utils.solve_dependencies = _original_solve_dependencies  # type: ignore[assignment]
    _routing.solve_dependencies = _original_routing_solve_dependencies  # type: ignore[attr-defined,assignment]
    _original_solve_dependencies = None
    _original_routing_solve_dependencies = None
    return True
