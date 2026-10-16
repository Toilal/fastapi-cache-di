"""Mark sync dependencies as safe to call directly on the event loop.

FastAPI calls every dependency that is neither a coroutine nor a generator
through the threadpool: it cannot tell whether its body blocks. For a class
whose ``__init__`` only stores its arguments, that hop costs far more than the
call itself. :func:`loop_safe` lets the author vouch for such a callable, so
that :func:`~fastapi_cache_di.patch.patch_fastapi_run_in_threadpool` calls it
inline instead.

The marker is the ``__fastapi_loop_safe__`` attribute, a contract shared with
``fastapi-standalone-di``: an object marked by either library is recognised by
both, without one depending on the other. Its name must therefore never change.
"""

import functools
from collections.abc import Callable
from typing import Any, TypeVar

LOOP_SAFE_ATTR = "__fastapi_loop_safe__"

T = TypeVar("T")


def loop_safe(obj: T) -> T:
    """Mark a sync function or a class as safe to call on the event loop.

    Returns *obj* itself, so ``Depends(obj)`` and ``dependency_overrides[obj]``
    keep working. A loop-safe callable must never block: no I/O, no lock, no
    heavy computation.
    """
    setattr(obj, LOOP_SAFE_ATTR, True)
    return obj


def is_loop_safe(call: Callable[..., Any]) -> bool:
    """True if *call* itself carries the loop-safe marker.

    The marker is read from the object's own namespace, never inherited: a
    subclass may redefine a blocking ``__init__`` and must be marked on its own.
    ``functools.partial`` wrappers are unwrapped down to the wrapped callable.
    """
    while isinstance(call, functools.partial):
        call = call.func
    namespace = getattr(call, "__dict__", None)
    return namespace is not None and namespace.get(LOOP_SAFE_ATTR) is True
