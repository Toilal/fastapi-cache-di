"""Solve a route's dependencies from a plan compiled once per route.

At every request, ``solve_dependencies`` walks the route's dependency tree
recursively and recomputes, for each node, work that only depends on the route
and on ``dependency_overrides``: its cache key, the kind of its callable, the
request parameters it has or not. :func:`compile_plan` does that work once and
flattens the tree into steps, in FastAPI's evaluation order (depth first,
sub-dependencies in the order of ``dependant.dependencies``).
:func:`solve_dependencies` then runs the steps in a loop, with the same values,
errors, request cache and ``yield`` exits as FastAPI.

A repeated occurrence of a ``use_cache=True`` dependency is a step that reads
the request cache. Its value is missing only when its first occurrence failed
validation; FastAPI then solves it again and reports its errors once more, so
the step hands that sub-tree to FastAPI's own ``solve_dependencies``.

The plans of a route are stored on its ``dependency_overrides_provider`` (the
app), so they live as long as the app: one for requests without overrides, one
for the last state of ``dependency_overrides`` seen.
"""

import dataclasses
import functools
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

import fastapi.dependencies.models as _dep_models
import fastapi.dependencies.utils as _dep_utils
from fastapi.background import BackgroundTasks
from fastapi.dependencies.models import Dependant
from fastapi.security import SecurityScopes
from starlette.background import BackgroundTasks as StarletteBackgroundTasks
from starlette.requests import Request
from starlette.responses import Response
from starlette.websockets import WebSocket

# Version markers, from the fields of ``Dependant``: dependency scopes (with a
# request and a function exit stack) arrive in FastAPI 0.121, and
# ``security_scopes`` splits into own/parent OAuth scopes in 0.123.
_DEPENDANT_FIELDS = frozenset(field.name for field in dataclasses.fields(Dependant))
_SCOPED_STACKS = "scope" in _DEPENDANT_FIELDS
_SPLIT_OAUTH_SCOPES = "own_oauth_scopes" in _DEPENDANT_FIELDS

# Functions from FastAPI 0.140; ``Dependant`` properties before.
_get_cache_key: Callable[..., Any] | None = getattr(_dep_models, "_get_cache_key", None)
_get_oauth_scopes: Callable[..., list[str]] | None = getattr(
    _dep_utils, "_get_oauth_scopes", None
)

_COROUTINE = 0
_SYNC = 1
_GENERATOR = 2

# Keyword arguments of the root call made by FastAPI's route handlers. A call
# with anything else, or with a request cache to fill, goes to FastAPI.
_ROOT_KWARGS = frozenset(
    {
        "request",
        "dependant",
        "body",
        "background_tasks",
        "response",
        "dependency_overrides_provider",
        "async_exit_stack",
        "embed_body_fields",
    }
)

_REQUEST_PARAM_SOURCES = (
    ("path_params", "path_params"),
    ("query_params", "query_params"),
    ("header_params", "headers"),
    ("cookie_params", "cookies"),
)

_PLANS_ATTR = "_fastapi_cache_di_plans"


def request_cache_key(dependant: Dependant, uses_scopes_cache: Any) -> Any:
    """The key FastAPI stores the value of ``dependant`` under in the request cache.

    A function from FastAPI 0.140, ``Dependant.cache_key`` before (a dataclass
    field, then a ``cached_property``).
    """
    if _get_cache_key is not None:
        return _get_cache_key(dependant=dependant, uses_scopes_cache=uses_scopes_cache)
    return dependant.cache_key  # type: ignore[attr-defined,unused-ignore]


def _oauth_scopes(dependant: Dependant) -> list[str]:
    if _get_oauth_scopes is not None:
        return _get_oauth_scopes(dependant=dependant)
    if _SPLIT_OAUTH_SCOPES:
        scopes: list[str] = dependant.oauth_scopes  # type: ignore[attr-defined,unused-ignore]
        return scopes
    legacy: list[str] = dependant.security_scopes  # type: ignore[attr-defined,unused-ignore]
    return legacy


def _call_kind(dependant: Dependant) -> int:
    call = dependant.call
    is_gen = getattr(_dep_utils, "_is_gen_callable", None)
    if is_gen is not None:
        if is_gen(call) or _dep_utils._is_async_gen_callable(call):  # type: ignore[attr-defined,unused-ignore]
            return _GENERATOR
        if _dep_utils._is_coroutine_callable(call):  # type: ignore[attr-defined,unused-ignore]
            return _COROUTINE
        return _SYNC
    if hasattr(_dep_utils, "is_gen_callable"):
        if _dep_utils.is_gen_callable(call) or _dep_utils.is_async_gen_callable(call):  # type: ignore[attr-defined,unused-ignore]
            return _GENERATOR
        if _dep_utils.is_coroutine_callable(call):  # type: ignore[attr-defined,unused-ignore]
            return _COROUTINE
        return _SYNC
    if dependant.is_gen_callable or dependant.is_async_gen_callable:  # type: ignore[attr-defined,unused-ignore]
        return _GENERATOR
    if dependant.is_coroutine_callable:  # type: ignore[attr-defined,unused-ignore]
        return _COROUTINE
    return _SYNC


def _overridden_kwargs(sub: Dependant, overrides: Mapping[Any, Any]) -> dict[str, Any]:
    """The ``get_dependant`` arguments FastAPI rebuilds ``sub`` with under overrides.

    FastAPI rebuilds every sub-dependant, overridden or not, as soon as one
    override exists.
    """
    assert sub.call is not None
    kwargs: dict[str, Any] = {
        "path": sub.path,
        "call": overrides.get(sub.call, sub.call),
        "name": sub.name,
    }
    if _SPLIT_OAUTH_SCOPES:
        kwargs["parent_oauth_scopes"] = _oauth_scopes(sub)
    else:
        kwargs["security_scopes"] = sub.security_scopes  # type: ignore[attr-defined,unused-ignore]
    if _SCOPED_STACKS:
        kwargs["scope"] = sub.scope
    return kwargs


def _get_dependant(**kwargs: Any) -> Dependant:
    return _dep_utils.get_dependant(**kwargs)


def missing_internals() -> list[str]:
    """The FastAPI functions the plan calls that this FastAPI does not define."""
    generator_helper = "_solve_generator" if _SCOPED_STACKS else "solve_generator"
    names = (
        "SolvedDependency",
        "get_dependant",
        "request_params_to_args",
        "request_body_to_args",
        "run_in_threadpool",
        generator_helper,
    )
    return [name for name in names if not hasattr(_dep_utils, name)]


@dataclasses.dataclass(slots=True, frozen=True)
class _Frame:
    """The own parameters of a dependant, extracted once its sub-dependencies ran.

    Only what is read at request time is kept: under overrides, the dependant
    is a copy rebuilt by ``get_dependant`` together with its whole sub-tree.
    """

    request_params: tuple[tuple[Sequence[Any], str], ...]
    body_params: list[Any]
    http_connection_param_name: str | None
    request_param_name: str | None
    websocket_param_name: str | None
    background_tasks_param_name: str | None
    response_param_name: str | None
    security_scopes_param_name: str | None
    security_scopes: tuple[str, ...]
    has_special_params: bool
    has_own_params: bool


@dataclasses.dataclass(slots=True, frozen=True)
class _Step:
    """One occurrence of a sub-dependency, in FastAPI's evaluation order.

    ``frame`` is ``None`` for a repeated occurrence of a ``use_cache=True``
    dependency, whose sub-tree is not part of the plan again. ``dependant`` is
    what FastAPI's helpers receive (the generator helper, and FastAPI's own
    ``solve_dependencies`` for a repeated occurrence missing from the cache); a
    route's own dependant, or one rebuilt on demand by ``rebuild`` under
    overrides.
    """

    parent: int
    frame: int | None
    name: str | None
    use_cache: bool
    function_scope: bool
    key: Any
    kind: int
    call: Callable[..., Any]
    dependant: Dependant | None
    rebuild: Callable[[], Dependant] | None


@dataclasses.dataclass(slots=True, frozen=True)
class Plan:
    frames: tuple[_Frame, ...]
    steps: tuple[_Step, ...]


def _frame(dependant: Dependant) -> _Frame:
    request_params = tuple(
        (fields, source)
        for attr, source in _REQUEST_PARAM_SOURCES
        if (fields := getattr(dependant, attr))
    )
    has_special_params = any(
        (
            dependant.http_connection_param_name,
            dependant.request_param_name,
            dependant.websocket_param_name,
            dependant.background_tasks_param_name,
            dependant.response_param_name,
            dependant.security_scopes_param_name,
        )
    )
    return _Frame(
        request_params=request_params,
        body_params=dependant.body_params,
        http_connection_param_name=dependant.http_connection_param_name,
        request_param_name=dependant.request_param_name,
        websocket_param_name=dependant.websocket_param_name,
        background_tasks_param_name=dependant.background_tasks_param_name,
        response_param_name=dependant.response_param_name,
        security_scopes_param_name=dependant.security_scopes_param_name,
        security_scopes=(
            # ``None`` before FastAPI 0.123 when no parent sets scopes;
            # ``SecurityScopes`` turns it into an empty list.
            tuple(_oauth_scopes(dependant) or ())
            if dependant.security_scopes_param_name
            else ()
        ),
        has_special_params=has_special_params,
        has_own_params=(
            bool(request_params) or bool(dependant.body_params) or has_special_params
        ),
    )


def _route_dependants(root: Dependant) -> dict[int, Dependant]:
    """Every dependant of the route's own tree, by ``id()`` of its call."""
    found: dict[int, Dependant] = {}
    pending = list(root.dependencies)
    seen: set[int] = set()
    while pending:
        dependant = pending.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        found.setdefault(id(dependant.call), dependant)
        pending.extend(dependant.dependencies)
    return found


def compile_plan(root: Dependant, overrides: Mapping[Any, Any]) -> Plan:
    """Flatten the dependency tree of ``root`` into steps, overrides applied."""
    frames = [_frame(root)]
    steps: list[_Step] = []
    solved_keys: set[Any] = set()
    uses_scopes_cache: dict[Any, Any] = {}
    route_dependants = _route_dependants(root) if overrides else {}
    generator_dependants: dict[int, Dependant] = {}

    def generator_dependant(call: Callable[..., Any]) -> Dependant:
        # The generator helper only reads the call: reuse the route's own
        # dependant for it, or a bare one shared by every step of that call.
        found = route_dependants.get(id(call)) or generator_dependants.get(id(call))
        if found is None or found.call is not call:
            found = generator_dependants[id(call)] = Dependant(call=call)
        return found

    def visit(dependant: Dependant, index: int) -> None:
        for sub in dependant.dependencies:
            if overrides:
                rebuild_kwargs = _overridden_kwargs(sub, overrides)
                used = _get_dependant(**rebuild_kwargs)
            else:
                used = sub
            key = request_cache_key(sub, uses_scopes_cache)
            kind = _call_kind(used)
            assert used.call is not None
            frame: int | None = None
            if not (sub.use_cache and key in solved_keys):
                frame = len(frames)
                frames.append(_frame(used))
                visit(used, frame)
                solved_keys.add(key)
            step_dependant: Dependant | None = used
            rebuild: Callable[[], Dependant] | None = None
            if overrides:
                step_dependant = None
                if frame is None:
                    rebuild = functools.partial(_get_dependant, **rebuild_kwargs)
                elif kind == _GENERATOR:
                    step_dependant = generator_dependant(used.call)
            steps.append(
                _Step(
                    parent=index,
                    frame=frame,
                    name=sub.name,
                    use_cache=sub.use_cache,
                    function_scope=getattr(sub, "scope", None) == "function",
                    key=key,
                    kind=kind,
                    call=used.call,
                    dependant=step_dependant,
                    rebuild=rebuild,
                )
            )

    visit(root, 0)
    return Plan(frames=tuple(frames), steps=tuple(steps))


class _RoutePlans:
    __slots__ = ("overridden", "overrides", "plain", "root")

    def __init__(self, root: Dependant) -> None:
        self.root = root
        self.plain: Plan | None = None
        self.overridden: Plan | None = None
        self.overrides: tuple[tuple[Any, Any], ...] = ()


def _same_overrides(
    left: tuple[tuple[Any, Any], ...], right: tuple[tuple[Any, Any], ...]
) -> bool:
    return len(left) == len(right) and all(
        lk is rk and lv is rv for (lk, lv), (rk, rv) in zip(left, right, strict=True)
    )


def _plan_for(root: Dependant, provider: Any) -> Plan | None:
    """The plan of ``root`` for the current overrides, compiled on first use.

    ``None`` when there is no provider (the app) to store it on.
    """
    try:
        namespace = vars(provider)
    except TypeError:
        return None
    plans: dict[int, _RoutePlans] | None = namespace.get(_PLANS_ATTR)
    if plans is None:
        plans = namespace[_PLANS_ATTR] = {}
    route = plans.get(id(root))
    if route is None or route.root is not root:
        route = plans[id(root)] = _RoutePlans(root)

    overrides = getattr(provider, "dependency_overrides", None)
    if not overrides:
        if route.plain is None:
            route.plain = compile_plan(root, {})
        return route.plain
    current = tuple(overrides.items())
    if route.overridden is None or not _same_overrides(route.overrides, current):
        route.overridden = compile_plan(root, overrides)
        route.overrides = current
    return route.overridden


async def _solve_own_params(
    frame: _Frame,
    values: dict[str, Any],
    errors: list[Any],
    *,
    request: Request | WebSocket,
    body: Any,
    background_tasks: StarletteBackgroundTasks | None,
    response: Response,
    embed_body_fields: bool,
) -> StarletteBackgroundTasks | None:
    for fields, source in frame.request_params:
        param_values, param_errors = _dep_utils.request_params_to_args(
            fields, getattr(request, source)
        )
        values.update(param_values)
        errors.extend(param_errors)
    if frame.body_params:
        body_values, body_errors = await _dep_utils.request_body_to_args(
            body_fields=frame.body_params,
            received_body=body,
            embed_body_fields=embed_body_fields,
        )
        values.update(body_values)
        errors.extend(body_errors)
    if not frame.has_special_params:
        return background_tasks
    if frame.http_connection_param_name:
        values[frame.http_connection_param_name] = request
    if frame.request_param_name and isinstance(request, Request):
        values[frame.request_param_name] = request
    elif frame.websocket_param_name and isinstance(request, WebSocket):
        values[frame.websocket_param_name] = request
    if frame.background_tasks_param_name:
        if background_tasks is None:
            background_tasks = BackgroundTasks()
        values[frame.background_tasks_param_name] = background_tasks
    if frame.response_param_name:
        values[frame.response_param_name] = response
    if frame.security_scopes_param_name:
        values[frame.security_scopes_param_name] = SecurityScopes(
            scopes=list(frame.security_scopes)
        )
    return background_tasks


async def run_plan(
    plan: Plan,
    *,
    request: Request | WebSocket,
    body: Any = None,
    background_tasks: StarletteBackgroundTasks | None = None,
    response: Response | None = None,
    dependency_overrides_provider: Any = None,
    async_exit_stack: AsyncExitStack,
    embed_body_fields: bool,
) -> Any:
    """Solve the dependencies of the plan's root for one request.

    Returns a ``SolvedDependency`` equal to the one FastAPI's
    ``solve_dependencies`` returns for the same root call.
    """
    request_astack = function_astack = async_exit_stack
    if _SCOPED_STACKS:
        inner_astack = request.scope.get("fastapi_inner_astack")
        assert isinstance(inner_astack, AsyncExitStack), (
            "fastapi_inner_astack not found in request scope"
        )
        function_scope_astack = request.scope.get("fastapi_function_astack")
        assert isinstance(function_scope_astack, AsyncExitStack), (
            "fastapi_function_astack not found in request scope"
        )
        request_astack, function_astack = inner_astack, function_scope_astack
    if response is None:
        response = Response()
        del response.headers["content-length"]
        response.status_code = None  # type: ignore[assignment]
    cache: dict[Any, Any] = {}
    values: list[dict[str, Any]] = [{} for _ in plan.frames]
    errors: list[list[Any]] = [[] for _ in plan.frames]
    frames = plan.frames

    for step in plan.steps:
        parent = step.parent
        dependant = step.dependant
        if step.frame is None:
            if step.key in cache:
                if step.name is not None:
                    values[parent][step.name] = cache[step.key]
                continue
            if step.rebuild is not None:
                dependant = step.rebuild()
            assert dependant is not None
            solved = await _dep_utils.solve_dependencies(
                request=request,
                dependant=dependant,
                body=body,
                background_tasks=background_tasks,
                response=response,
                dependency_overrides_provider=dependency_overrides_provider,
                dependency_cache=cache,
                async_exit_stack=async_exit_stack,
                embed_body_fields=embed_body_fields,
            )
            background_tasks = solved.background_tasks
            # FastAPI 0.112.4 replaces an empty cache with a new dict.
            if solved.dependency_cache is not cache:
                cache.update(solved.dependency_cache)
            if solved.errors:
                errors[parent].extend(solved.errors)
                continue
            sub_values = solved.values
        else:
            frame = frames[step.frame]
            sub_values = values[step.frame]
            sub_errors = errors[step.frame]
            if frame.has_own_params:
                background_tasks = await _solve_own_params(
                    frame,
                    sub_values,
                    sub_errors,
                    request=request,
                    body=body,
                    background_tasks=background_tasks,
                    response=response,
                    embed_body_fields=embed_body_fields,
                )
            if sub_errors:
                errors[parent].extend(sub_errors)
                continue

        key = step.key
        if step.use_cache and key in cache:
            solved_value = cache[key]
        elif step.kind == _GENERATOR:
            if _SCOPED_STACKS:
                assert dependant is not None
                stack = function_astack if step.function_scope else request_astack
                solved_value = await _dep_utils._solve_generator(  # type: ignore[attr-defined,unused-ignore]
                    dependant=dependant, stack=stack, sub_values=sub_values
                )
            else:
                solved_value = await _dep_utils.solve_generator(  # type: ignore[attr-defined,unused-ignore]
                    call=step.call, stack=async_exit_stack, sub_values=sub_values
                )
        elif step.kind == _COROUTINE:
            solved_value = await step.call(**sub_values)
        else:
            solved_value = await _dep_utils.run_in_threadpool(  # type: ignore[attr-defined]
                step.call, **sub_values
            )
        if step.name is not None:
            values[parent][step.name] = solved_value
        if key not in cache:
            cache[key] = solved_value

    root = frames[0]
    if root.has_own_params:
        background_tasks = await _solve_own_params(
            root,
            values[0],
            errors[0],
            request=request,
            body=body,
            background_tasks=background_tasks,
            response=response,
            embed_body_fields=embed_body_fields,
        )
    return _dep_utils.SolvedDependency(
        values=values[0],
        errors=errors[0],
        background_tasks=background_tasks,
        response=response,
        dependency_cache=cache,
    )


async def solve_dependencies(**kwargs: Any) -> Any:
    """Drop-in for FastAPI's root ``solve_dependencies`` call, solved from a plan.

    Any call a plan does not cover is passed on to the module global
    ``fastapi.dependencies.utils.solve_dependencies``.
    """
    if kwargs.keys() <= _ROOT_KWARGS:
        plan = _plan_for(
            kwargs["dependant"], kwargs.get("dependency_overrides_provider")
        )
        if plan is not None:
            del kwargs["dependant"]
            return await run_plan(plan, **kwargs)
    return await _dep_utils.solve_dependencies(**kwargs)
