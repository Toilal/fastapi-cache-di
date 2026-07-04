"""Monkeypatch FastAPI's dependency introspection to cache expensive operations.

FastAPI calls ``get_typed_signature`` and ``get_dependant`` for every
``Depends()`` in every route handler at startup — with **no caching**.
Shared dependencies such as an auth check or a DB handle are re-introspected
tens of thousands of times.

This module caches three functions:

- ``get_typed_signature``: saves repeated signature parsing.
- ``get_dependant``: eliminates redundant recursive introspection of the
  dependency tree.
- ``get_flat_dependant``: the **dominant startup cost**. FastAPI calls it once
  per route in ``APIRoute.__init__`` to flatten the dependency tree, and it
  recurses through every sub-dependency without caching. Shared sub-dependencies
  (auth, DB, roles ...) are re-traversed from scratch for every route. Caching
  the recursive results by ``(id(dependant), parent_oauth_scopes)`` turns
  O(routes * tree-depth) into O(routes + unique-deps).

Call :func:`patch_fastapi_deps_cache` **once** before the app loads its routes.
Call :func:`unpatch_fastapi_deps_cache` after route loading to restore originals
and free memory — or use the :func:`fastapi_deps_cache` context manager.

.. warning::
   All patch state lives in module globals (originals, active cache, whether the
   routing flat patch was applied). Patching swaps FastAPI module attributes
   process-wide; it is neither thread-safe nor safe to interleave with route
   loading on other threads. Use ``patch``/``unpatch`` (and the context manager)
   from a single thread during startup only. Hit/miss counters live on the
   :class:`~fastapi_cache_di.cache.DepsCache` instead (so a caller-owned cache
   keeps them after unpatch); re-entrancy of the context manager is handled by
   :func:`patch_fastapi_deps_cache` returning ``False`` when already active.
"""

import copy
import inspect
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

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
_patched_routing_flat = False


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
    and ``get_flat_dependant``.

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

    # Cache get_flat_dependant — patch the modules that import it: utils
    # (self-recursive calls), openapi (all versions), and routing. ``routing``
    # only imports ``get_flat_dependant`` from FastAPI 0.112.4 onwards, so patch
    # it there only if present, and remember whether we did (for unpatch).
    #
    # The openapi patch is effectively a no-op on current FastAPI: both call
    # sites (``fastapi/openapi/utils.py``) pass ``skip_repeats=True``, which the
    # wrapper short-circuits straight to the original without caching. It is
    # installed for symmetry and as a safety net, so that any FastAPI version (or
    # third-party caller) reaching ``openapi.utils.get_flat_dependant`` with
    # ``skip_repeats=False`` still routes through the cache rather than diverging.
    global _patched_routing_flat
    _original_get_flat_dependant = _dep_utils.get_flat_dependant
    _dep_utils.get_flat_dependant = _cached_get_flat_dependant
    _openapi_utils.get_flat_dependant = _cached_get_flat_dependant  # type: ignore[attr-defined]
    if hasattr(_routing, "get_flat_dependant"):
        _routing.get_flat_dependant = _cached_get_flat_dependant
        _patched_routing_flat = True

    return True


def unpatch_fastapi_deps_cache() -> bool:
    """Restore originals and free cache memory.

    Returns ``True`` if unpatched, ``False`` if not currently patched.
    """
    global _original_get_typed_signature, _original_get_dependant
    global _original_get_flat_dependant, _active_cache, _owns_cache

    if _original_get_typed_signature is None:
        return False

    assert _original_get_dependant is not None
    assert _original_get_flat_dependant is not None
    assert _active_cache is not None

    logger.info(
        "FastAPI deps cache: "
        "get_typed_signature %d hits / %d misses (%d entries), "
        "get_dependant %d hits / %d misses (%d entries), "
        "get_flat_dependant %d hits / %d misses (%d entries) — %s.",
        _active_cache.sig_hits,
        _active_cache.sig_misses,
        len(_active_cache.signatures),
        _active_cache.dep_hits,
        _active_cache.dep_misses,
        len(_active_cache.dependants),
        _active_cache.flat_hits,
        _active_cache.flat_misses,
        len(_active_cache.flat_dependants),
        "cleared" if _owns_cache else "kept (caller-owned)",
    )

    global _patched_routing_flat
    _dep_utils.get_typed_signature = _original_get_typed_signature
    _dep_utils.get_dependant = _original_get_dependant
    _routing.get_dependant = _original_get_dependant  # type: ignore[attr-defined]
    _dep_utils.get_flat_dependant = _original_get_flat_dependant
    _openapi_utils.get_flat_dependant = _original_get_flat_dependant  # type: ignore[attr-defined]
    if _patched_routing_flat:
        _routing.get_flat_dependant = _original_get_flat_dependant  # type: ignore[attr-defined]
        _patched_routing_flat = False

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
