import inspect
from collections.abc import Iterator
from typing import Annotated

import fastapi.dependencies.utils as _dep_utils
import fastapi.openapi.utils as _openapi_utils
import fastapi.routing as _routing
import pytest
from fastapi import Depends
from fastapi.dependencies.models import Dependant

from fastapi_cache_di import (
    DepsCache,
    fastapi_deps_cache,
    patch_fastapi_deps_cache,
    unpatch_fastapi_deps_cache,
)
from fastapi_cache_di import patch as patch_module


@pytest.fixture(autouse=True)
def _ensure_unpatched() -> Iterator[None]:
    """Ensure the deps cache is unpatched before and after each test."""
    unpatch_fastapi_deps_cache()
    yield
    unpatch_fastapi_deps_cache()


def _dummy_dep() -> str:
    return "dep"


def _dummy_endpoint(dep: Annotated[str, Depends(_dummy_dep)]) -> str:
    return dep


class TestPatchUnpatch:
    def test_patch_returns_true_on_first_call(self) -> None:
        try:
            assert patch_fastapi_deps_cache() is True
        finally:
            unpatch_fastapi_deps_cache()

    def test_patch_returns_false_when_already_patched(self) -> None:
        try:
            patch_fastapi_deps_cache()
            assert patch_fastapi_deps_cache() is False
        finally:
            unpatch_fastapi_deps_cache()

    def test_unpatch_returns_true_when_patched(self) -> None:
        patch_fastapi_deps_cache()
        assert unpatch_fastapi_deps_cache() is True

    def test_unpatch_returns_false_when_not_patched(self) -> None:
        assert unpatch_fastapi_deps_cache() is False

    def test_unpatch_restores_originals(self) -> None:
        original_sig = _dep_utils.get_typed_signature
        original_dep = _dep_utils.get_dependant
        original_routing_dep = _routing.get_dependant
        original_flat = _dep_utils.get_flat_dependant
        original_routing_flat = _routing.get_flat_dependant
        original_openapi_flat = _openapi_utils.get_flat_dependant

        patch_fastapi_deps_cache()

        assert _dep_utils.get_typed_signature is not original_sig
        assert _dep_utils.get_dependant is not original_dep
        assert _routing.get_dependant is not original_routing_dep
        assert _dep_utils.get_flat_dependant is not original_flat
        assert _routing.get_flat_dependant is not original_routing_flat
        assert _openapi_utils.get_flat_dependant is not original_openapi_flat

        unpatch_fastapi_deps_cache()

        assert _dep_utils.get_typed_signature is original_sig
        assert _dep_utils.get_dependant is original_dep
        assert _routing.get_dependant is original_routing_dep
        assert _dep_utils.get_flat_dependant is original_flat
        assert _routing.get_flat_dependant is original_routing_flat
        assert _openapi_utils.get_flat_dependant is original_openapi_flat

    def test_unpatch_clears_caches(self) -> None:
        patch_fastapi_deps_cache()
        cache = patch_module._active_cache
        assert cache is not None
        # Populate caches
        _dep_utils.get_typed_signature(_dummy_dep)
        _dep_utils.get_dependant(path="/test", call=_dummy_dep)
        assert len(cache.signatures) > 0
        assert len(cache.dependants) > 0

        unpatch_fastapi_deps_cache()

        assert patch_module._active_cache is None


class TestContextManager:
    def test_patches_and_unpatches(self) -> None:
        original_dep = _dep_utils.get_dependant

        with fastapi_deps_cache():
            assert _dep_utils.get_dependant is not original_dep

        assert _dep_utils.get_dependant is original_dep

    def test_unpatches_on_exception(self) -> None:
        original_dep = _dep_utils.get_dependant

        try:
            with fastapi_deps_cache():
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        assert _dep_utils.get_dependant is original_dep

    def test_caching_works_inside_context(self) -> None:
        with fastapi_deps_cache():
            a = _dep_utils.get_dependant(path="/test", call=_dummy_dep, name="dep")
            b = _dep_utils.get_dependant(path="/test", call=_dummy_dep, name="dep")
            assert a is b


class TestGetTypedSignatureCache:
    def test_returns_correct_signature(self) -> None:
        patch_fastapi_deps_cache()
        try:
            result = _dep_utils.get_typed_signature(_dummy_dep)
            assert isinstance(result, inspect.Signature)
        finally:
            unpatch_fastapi_deps_cache()

    def test_same_callable_returns_same_object(self) -> None:
        patch_fastapi_deps_cache()
        try:
            a = _dep_utils.get_typed_signature(_dummy_dep)
            b = _dep_utils.get_typed_signature(_dummy_dep)
            assert a is b
        finally:
            unpatch_fastapi_deps_cache()

    def test_different_callables_return_different_objects(self) -> None:
        patch_fastapi_deps_cache()
        try:
            a = _dep_utils.get_typed_signature(_dummy_dep)
            b = _dep_utils.get_typed_signature(_dummy_endpoint)
            assert a is not b
        finally:
            unpatch_fastapi_deps_cache()


class TestGetDependantCache:
    def test_returns_dependant(self) -> None:
        patch_fastapi_deps_cache()
        try:
            result = _dep_utils.get_dependant(path="/test", call=_dummy_dep)
            assert isinstance(result, Dependant)
        finally:
            unpatch_fastapi_deps_cache()

    def test_same_args_returns_same_object(self) -> None:
        patch_fastapi_deps_cache()
        try:
            a = _dep_utils.get_dependant(path="/test", call=_dummy_dep, name="dep")
            b = _dep_utils.get_dependant(path="/test", call=_dummy_dep, name="dep")
            assert a is b
        finally:
            unpatch_fastapi_deps_cache()

    def test_different_paths_same_param_names_share_cache(self) -> None:
        patch_fastapi_deps_cache()
        try:
            a = _dep_utils.get_dependant(
                path="/a/{group_id}/test", call=_dummy_dep, name="dep"
            )
            b = _dep_utils.get_dependant(
                path="/b/{group_id}/test", call=_dummy_dep, name="dep"
            )
            # Same path param names → same cache entry
            assert a is b
        finally:
            unpatch_fastapi_deps_cache()

    def test_different_path_param_names_separate_cache(self) -> None:
        patch_fastapi_deps_cache()
        try:
            a = _dep_utils.get_dependant(
                path="/{group_id}/test", call=_dummy_dep, name="dep"
            )
            b = _dep_utils.get_dependant(path="/test", call=_dummy_dep, name="dep")
            assert a is not b
        finally:
            unpatch_fastapi_deps_cache()

    def test_different_names_separate_cache(self) -> None:
        patch_fastapi_deps_cache()
        try:
            a = _dep_utils.get_dependant(path="/test", call=_dummy_dep, name="db")
            b = _dep_utils.get_dependant(path="/test", call=_dummy_dep, name="client")
            assert a is not b
        finally:
            unpatch_fastapi_deps_cache()

    def test_sub_dependencies_are_cached(self) -> None:
        """The recursive get_dependant calls for sub-deps hit the cache."""
        patch_fastapi_deps_cache()
        try:
            # First call: endpoint with Depends(_dummy_dep) — builds tree
            a = _dep_utils.get_dependant(path="/test", call=_dummy_endpoint)
            assert len(a.dependencies) == 1
            sub_a = a.dependencies[0]

            # Second call with different endpoint but same dep
            def _other_endpoint(dep: Annotated[str, Depends(_dummy_dep)]) -> str:
                return dep

            b = _dep_utils.get_dependant(path="/test", call=_other_endpoint)
            assert len(b.dependencies) == 1
            sub_b = b.dependencies[0]

            # Sub-dependency should be the same cached object
            assert sub_a is sub_b
        finally:
            unpatch_fastapi_deps_cache()

    def test_routing_module_also_patched(self) -> None:
        """get_dependant in fastapi.routing is also patched."""
        patch_fastapi_deps_cache()
        try:
            a = _routing.get_dependant(path="/test", call=_dummy_dep, name="dep")
            b = _dep_utils.get_dependant(path="/test", call=_dummy_dep, name="dep")
            assert a is b
        finally:
            unpatch_fastapi_deps_cache()


class TestGetFlatDependantCache:
    def test_returns_dependant(self) -> None:
        patch_fastapi_deps_cache()
        try:
            dep = _dep_utils.get_dependant(path="/test", call=_dummy_endpoint)
            result = _dep_utils.get_flat_dependant(dep)
            assert isinstance(result, Dependant)
        finally:
            unpatch_fastapi_deps_cache()

    def test_same_dependant_returns_equal_but_distinct_objects(self) -> None:
        """Cache returns a copy (not the same object) to prevent mutation issues."""
        patch_fastapi_deps_cache()
        try:
            dep = _dep_utils.get_dependant(path="/test", call=_dummy_endpoint)
            a = _dep_utils.get_flat_dependant(dep)
            b = _dep_utils.get_flat_dependant(dep)
            # Different objects (copies)
            assert a is not b
            # But same content
            assert a.call is b.call
            assert len(a.path_params) == len(b.path_params)
        finally:
            unpatch_fastapi_deps_cache()

    def test_cache_prevents_redundant_traversal(self) -> None:
        """Shared sub-dependencies are flattened once, then served from cache."""
        patch_fastapi_deps_cache()
        try:
            dep1 = _dep_utils.get_dependant(path="/a", call=_dummy_endpoint)
            dep2 = _dep_utils.get_dependant(path="/b", call=_dummy_endpoint)
            _dep_utils.get_flat_dependant(dep1)

            hits_before = patch_module._flat_hits

            _dep_utils.get_flat_dependant(dep2)

            # The shared sub-dependency _dummy_dep should be a cache hit
            assert patch_module._flat_hits > hits_before
        finally:
            unpatch_fastapi_deps_cache()

    def test_skip_repeats_bypasses_cache(self) -> None:
        """skip_repeats=True calls go straight through without caching."""
        patch_fastapi_deps_cache()
        try:
            dep = _dep_utils.get_dependant(path="/test", call=_dummy_endpoint)
            result = _dep_utils.get_flat_dependant(dep, skip_repeats=True)
            assert isinstance(result, Dependant)
            cache = patch_module._active_cache
            assert cache is not None
            assert len(cache.flat_dependants) == 0
        finally:
            unpatch_fastapi_deps_cache()

    def test_all_modules_patched(self) -> None:
        """get_flat_dependant is patched in utils, routing, and openapi modules."""
        original = _dep_utils.get_flat_dependant
        patch_fastapi_deps_cache()
        try:
            assert _dep_utils.get_flat_dependant is not original
            assert _routing.get_flat_dependant is _dep_utils.get_flat_dependant
            assert _openapi_utils.get_flat_dependant is _dep_utils.get_flat_dependant
        finally:
            unpatch_fastapi_deps_cache()


class TestKeepAliveGuardsAgainstIdReuse:
    """The caches key on ``id(call)`` / ``id(dependant)``. Python recycles
    ``id()`` after GC, so cached entries must pin their keyed objects to keep
    the address reserved — otherwise a later object allocated at the same
    address is wrongly served the dead object's signature/dependant.
    """

    def test_cached_signature_callable_is_pinned(self) -> None:
        cache = DepsCache()
        patch_fastapi_deps_cache(cache)
        try:

            def ephemeral_dep() -> str:
                return "x"

            _dep_utils.get_typed_signature(ephemeral_dep)
            assert id(ephemeral_dep) in cache._keepalive
        finally:
            unpatch_fastapi_deps_cache()

    def test_cached_dependant_callable_is_pinned(self) -> None:
        cache = DepsCache()
        patch_fastapi_deps_cache(cache)
        try:

            def ephemeral_endpoint(dep: Annotated[str, Depends(_dummy_dep)]) -> str:
                return dep

            _dep_utils.get_dependant(path="/x", call=ephemeral_endpoint)
            assert id(ephemeral_endpoint) in cache._keepalive
        finally:
            unpatch_fastapi_deps_cache()
