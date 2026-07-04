"""Backing store for the FastAPI dependency-introspection cache.

:class:`DepsCache` holds the three levels of memoized data that
:mod:`fastapi_cache_di.patch` populates when it wraps FastAPI's introspection
functions:

- **signatures** — ``get_typed_signature`` results, keyed by ``id(call)``.
- **dependants** — ``get_dependant`` results, keyed by a composite tuple that
  captures every argument that affects the produced ``Dependant``.
- **flat_dependants** — ``get_flat_dependant`` results, keyed by
  ``(id(dependant), parent_oauth_scopes)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import inspect

    from fastapi.dependencies.models import Dependant

# (id(call), path_param_names, name, use_cache, extra-kwargs). The trailing
# element folds in the version-specific scope kwargs of ``get_dependant``.
DependantKey = tuple[Any, ...]
# (id(dependant), parent_oauth_scopes)
FlatDependantKey = tuple[int, tuple[str, ...]]


class DepsCache:
    """Unified cache for FastAPI dependency introspection results.

    Hit/miss counters live on the instance (not module globals), so a
    caller-owned cache still exposes its effectiveness after ``unpatch`` — read
    :attr:`sig_hits`, :attr:`flat_misses`, etc., once route loading is done.
    """

    __slots__ = (
        "_keepalive",
        "dep_hits",
        "dep_misses",
        "dependants",
        "flat_dependants",
        "flat_hits",
        "flat_misses",
        "sig_hits",
        "sig_misses",
        "signatures",
    )

    def __init__(self) -> None:
        self.signatures: dict[int, inspect.Signature] = {}
        self.dependants: dict[DependantKey, Dependant] = {}
        self.flat_dependants: dict[FlatDependantKey, Dependant] = {}
        self.sig_hits = 0
        self.sig_misses = 0
        self.dep_hits = 0
        self.dep_misses = 0
        self.flat_hits = 0
        self.flat_misses = 0
        # Strong refs to every object whose ``id()`` is used as a cache key.
        # Python recycles ``id()`` (memory addresses) after an object is GC'd,
        # so without this a short-lived callable (e.g. a lazy-route stub) could
        # be collected and a later function allocated at the same address, then
        # wrongly served the dead object's cached signature/dependant. Holding
        # a reference keeps the address reserved for the cache's lifetime.
        # Keyed by ``id()`` to dedup; values are the pinned objects (which may
        # be unhashable, e.g. ``Dependant``, so a set is not usable here).
        self._keepalive: dict[int, object] = {}

    def keep_alive(self, *objs: object) -> None:
        """Pin objects so their ``id()`` stays reserved while cached."""
        for obj in objs:
            self._keepalive[id(obj)] = obj

    def clear(self) -> None:
        """Drop all cached entries and reset the hit/miss counters."""
        self.signatures.clear()
        self.dependants.clear()
        self.flat_dependants.clear()
        self._keepalive.clear()
        self.sig_hits = 0
        self.sig_misses = 0
        self.dep_hits = 0
        self.dep_misses = 0
        self.flat_hits = 0
        self.flat_misses = 0
