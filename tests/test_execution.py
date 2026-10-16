"""The loop-safe marker and its contract shared with fastapi-standalone-di."""

from functools import partial

from fastapi_cache_di import is_loop_safe, loop_safe
from fastapi_cache_di.execution import LOOP_SAFE_ATTR


class Plain:
    pass


@loop_safe
class Marked:
    pass


class UnmarkedChild(Marked):
    pass


def plain_fn() -> None:
    pass


@loop_safe
def marked_fn() -> None:
    pass


class _Holder:
    @loop_safe
    def marked_method(self) -> None:
        pass

    def plain_method(self) -> None:
        pass


def test_marker_attribute_is_the_contract_shared_with_fastapi_standalone_di() -> None:
    assert LOOP_SAFE_ATTR == "__fastapi_loop_safe__"


def test_loop_safe_returns_the_same_object() -> None:
    def fn() -> None:
        pass

    assert loop_safe(fn) is fn
    assert is_loop_safe(fn)


def test_marker_set_by_hand_is_recognised() -> None:
    class Foreign:
        __fastapi_loop_safe__ = True

    assert is_loop_safe(Foreign)


def test_unmarked_callables_are_not_loop_safe() -> None:
    assert not is_loop_safe(Plain)
    assert not is_loop_safe(plain_fn)
    assert not is_loop_safe(lambda: None)


def test_marked_callables_are_loop_safe() -> None:
    assert is_loop_safe(Marked)
    assert is_loop_safe(marked_fn)


def test_marker_is_not_inherited() -> None:
    assert not is_loop_safe(UnmarkedChild)


def test_partial_of_a_marked_function_is_loop_safe() -> None:
    assert is_loop_safe(partial(partial(marked_fn)))
    assert not is_loop_safe(partial(plain_fn))


def test_bound_method_follows_its_function() -> None:
    holder = _Holder()

    assert is_loop_safe(holder.marked_method)
    assert not is_loop_safe(holder.plain_method)


def test_object_without_namespace_is_not_loop_safe() -> None:
    assert not is_loop_safe(len)
