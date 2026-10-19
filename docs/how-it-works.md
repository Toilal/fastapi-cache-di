How it works
============

`patch_fastapi_deps_cache()` replaces up to three functions in
`fastapi.dependencies.utils` (plus the `fastapi.routing` and
`fastapi.openapi.utils` re-imports) with caching wrappers backed by a
[`DepsCache`](./api.md#depscache). `unpatch_fastapi_deps_cache()` restores the
originals.

## The cached functions

FastAPI introspects every route's dependency tree at startup with no caching.
Shared dependencies (an auth check, a DB handle, a role guard …) are
re-introspected from scratch for every route that uses them. The functions on
that hot path, and how each is keyed:

| Function | Cache key | Why |
|---|---|---|
| `get_typed_signature` | `id(call)` | avoid repeated signature parsing |
| `get_dependant` | `(id(call), path params, name, use_cache, scope kwargs)` | avoid redundant tree introspection |
| `get_flat_dependant` (FastAPI ≤ 0.140) | `(id(dependant), parent_oauth_scopes)` | the dominant startup cost on those versions |

On FastAPI ≤ 0.140, `get_flat_dependant` is the dominant cost: FastAPI calls it
once per route in `APIRoute.__init__` to flatten the dependency tree, recursing
through every sub-dependency without caching. Caching the recursive results
turns `O(routes × tree-depth)` into `O(routes + unique-deps)`.

FastAPI 0.141 removed `get_flat_dependant`: it now flattens the tree with
iterative walks that build no `Dependant`, so only `get_typed_signature` and
`get_dependant` are cached there.

## Where the patch is applied

Each function is patched in every module that holds its own reference:

- **`get_typed_signature`** — patched in `fastapi.dependencies.utils` only
  (that is the sole caller).
- **`get_dependant`** — patched in `fastapi.dependencies.utils` (for the
  recursive calls) and in `fastapi.routing` (which imports its own reference).
- **`get_flat_dependant`** — patched in `fastapi.dependencies.utils`
  (self-recursive calls), `fastapi.openapi.utils` and `fastapi.routing`, but
  only in the modules that actually define it; the patched modules are
  remembered and restored on unpatch. Nothing is patched from FastAPI 0.141,
  where the function no longer exists: no attribute is ever injected into a
  FastAPI module.

The `openapi.utils` patch is effectively a no-op: both call sites there pass
`skip_repeats=True`, which the wrapper short-circuits straight to the original
without caching. It is installed for symmetry and as a safety net.

## Copy-on-return semantics

Some `Dependant` list fields are mutated by FastAPI **after** introspection
returns (route-level parameterless dependencies inserted during routing, and on
FastAPI < 0.121 `security_requirements`). Handing back the shared cached object
would leak one route's mutations into every sibling that reuses the same cached
entry.

To stay safe, the wrappers shallow-copy the returned `Dependant` and give it
private copies of the mutated list *containers*:

- `get_flat_dependant` results are shallow-copied on the way out, with private
  copies of `path_params`, `query_params`, `header_params`, `cookie_params`,
  `body_params` and `dependencies`.
- `get_dependant` copies the mutated fields (`dependencies`,
  `security_requirements`) only for the **top-level** path-operation dependant
  (`name is None`), which FastAPI mutates in place. Named sub-dependants are
  never mutated and keep their cached identity — that identity is what makes the
  recursive sub-dependency sharing work.

Only the list *containers* are private; their elements (`ModelField` and
sub-`Dependant` objects) remain shared references. Appending to or reordering a
returned list is safe; mutating an element in place is not.

## Cache-key subtleties

- **`get_dependant`** folds in only the path *param names*, not the literal path
  string. Two routes with the same param names but different literals
  (`/a/{id}` vs `/b/{id}`) deliberately share one cached `Dependant` — this is
  what makes cross-route sharing effective. A consequence is that the shared
  object's `.path` reflects whichever route populated it first; FastAPI reads
  `.path` only for diagnostics, never for routing, so the staleness is cosmetic.
- The scope-related keyword arguments of `get_dependant` and `get_flat_dependant`
  changed across FastAPI versions. They are captured generically via `**kwargs`
  and folded into the key, so the wrappers stay compatible across the supported
  FastAPI range.
- Calls with `skip_repeats=True` (OpenAPI generation) bypass the cache since they
  depend on a mutable `visited` list, and are not a startup bottleneck anyway.

## `id()` recycling protection

The signature and dependant caches key on `id(call)` / `id(dependant)` — the
object's memory address. Python recycles `id()` after an object is garbage
collected, so a short-lived callable could be collected and a later object
allocated at the same address, then wrongly served the dead object's cached
entry. `DepsCache` holds a strong reference to every keyed object (via
`keep_alive`) so its address stays reserved for the cache's lifetime.

## Calling loop-safe dependencies inline

FastAPI awaits `fastapi.dependencies.utils.run_in_threadpool(call, **values)` for
every sub-dependency that is neither a coroutine nor a generator — the only call
site of that module global, from FastAPI 0.112.4 to 0.143.1.
`patch_fastapi_run_in_threadpool()` replaces the global: a callable that is
loop-safe (or any class, with `inline_classes=True`) is called inline, everything
else is delegated to the function the global held when patching.

- **Why the global, not `Dependant.call`** — rewriting `call` into an `async`
  factory would change the key FastAPI looks overrides up with and stores values
  under: an override registered on the original class would no longer be found,
  and shared sub-dependencies would be built twice once overrides are set.
  Patching the global leaves the `call`, the cache key and the override lookup
  untouched.
- **Overrides** — the `call` the replacement receives is the effective one, after
  `dependency_overrides`: an override is judged on its own marker.
- **Not affected** — sync generator dependencies go through
  `contextmanager_in_threadpool` and sync endpoints through `fastapi.routing`,
  so both keep their threadpool hop.
- **Guard** — a test fails if FastAPI stops awaiting sync dependencies through
  the module global.

## Skipping cached sub-trees at request time

`solve_dependencies` loops over a dependant's sub-dependencies, recurses into each
one, and only then checks whether the sub-dependency's value is already in the
request's `dependency_cache`. `patch_fastapi_solve_dependencies()` wraps it: when
the caller's sub-dependency is cached, the recursive call returns an empty
`SolvedDependency` straight away. The caller then hits its own cache check and
reuses the value, so no FastAPI code is copied.

- **Patch points** — `fastapi.dependencies.utils.solve_dependencies` (the
  recursion looks this module global up at every call) and
  `fastapi.routing.solve_dependencies` (imported by name; HTTP routes, WebSocket
  routes and route groups), which it shares with the resolution plan (see
  below). The root call never receives a cache, so it is never skipped.
- **Cache key** — it must be the exact key FastAPI stores the value under: a
  dataclass field before FastAPI 0.121, `Dependant.cache_key` as a
  `cached_property` up to 0.139, and `fastapi.dependencies.models._get_cache_key` from 0.140,
  called with the `_uses_scopes_cache` memo the recursion threads through.
- **The caller's sub-dependency** — when `dependency_overrides` is not empty,
  FastAPI rebuilds every sub-dependant, overridden or not, before recursing. The
  rebuilt dependant always has `use_cache=True` and drops its own `Security`
  scopes, so its key may not be the one the caller checks. The wrapper therefore
  tests the caller's own sub-dependant: before delegating, it publishes the
  dependant's sub-dependants in a `ContextVar`, and each recursive call takes the
  next one, since FastAPI recurses exactly once per sub-dependant, in order. A
  sub-dependant whose `name` or (overridden) `call` does not match what the
  recursive call received is never skipped. The `ContextVar` is per asyncio
  task, so concurrent requests never share it.
- **Guards** — tests fail if FastAPI stops resolving the recursion through the
  module global, changes the fields of `SolvedDependency`, keys its request cache
  differently, or stops recursing once per sub-dependant in order.

## Solving routes from a precompiled plan

`patch_fastapi_resolution_plan()` replaces the recursion itself.
`fastapi_cache_di.plan.compile_plan` flattens a route's dependency tree into
*frames* (a dependant whose own parameters are extracted after its
sub-dependencies) and *steps* (one per occurrence of a sub-dependency), in
FastAPI's evaluation order: depth first, sub-dependencies in the order of
`dependant.dependencies`. Each step carries, precomputed, its request cache key,
the kind of its callable (coroutine, sync, sync or async generator), its stack
scope, and each frame its non-empty parameter fields, special parameters
(`Request`, `WebSocket`, `HTTPConnection`, `Response`, `BackgroundTasks`,
`SecurityScopes`) and OAuth scopes. At request time, the plan runs the steps in
a single loop.

- **Patch point** — `fastapi.routing.solve_dependencies`, the root call. One
  dispatcher owns it while either the plan or the sub-tree skip is active: it
  runs the plan when the plan is active, and the current
  `fastapi.dependencies.utils.solve_dependencies` (the skip wrapper or FastAPI)
  otherwise. The first patch saves FastAPI's original, the last unpatch
  restores it, in any order. `Dependant.call` is never rewritten, so the
  override lookup and the cache keys stay FastAPI's.
- **FastAPI helpers** — the functions the plan calls (`request_params_to_args`,
  `request_body_to_args`, `run_in_threadpool`, the generator helper,
  `solve_dependencies`) are looked up on `fastapi.dependencies.utils` at call
  time, as FastAPI does, so `patch_fastapi_run_in_threadpool()` still applies.
- **Repeated occurrences** — a repeated occurrence of a `use_cache=True`
  dependency is a step that reads the request cache. Its value is missing only
  when its first occurrence failed validation; FastAPI then solves it again and
  reports its errors once more, so the step hands that sub-tree to
  `fastapi.dependencies.utils.solve_dependencies`, which duplicates the errors
  exactly like FastAPI.
- **Overrides** — the plan is compiled with `dependency_overrides` applied,
  rebuilding every sub-dependant with `get_dependant` like FastAPI, with the
  scope arguments of each version. The snapshot of the overrides (pairs compared
  by identity) is checked at every request, immediately when the dict is empty.
  The rebuilt sub-trees are not kept: frames and steps only keep the values they
  read, and a repeated occurrence keeps the `get_dependant` arguments to rebuild
  its dependant on the error path only.
- **Storage** — `Dependant` cannot carry the plan: from FastAPI 0.140 it is a
  slotted dataclass, without room for an extra attribute nor weak references.
  The plans are stored on the route's `dependency_overrides_provider` (the app),
  keyed by `id()` of the root dependant with a strong reference to it (the same
  protection as [`id()` recycling protection](#id-recycling-protection)), so
  they are freed with the app. A root call without a provider is solved by
  FastAPI.
- **Versions** — differences between FastAPI versions are confined to a few
  adapters chosen from the fields of `Dependant` and the functions FastAPI
  defines: the cache key, the OAuth scopes, the callable kind, the exit stacks
  (per scope from 0.121) and the `get_dependant` scope arguments.
- **Guards** — an equivalence test compares the plan with FastAPI on a
  reference tree (values, errors, request cache, call and exit order) on every
  version of the compatibility matrix, and a benchmark gate requires the plan to
  be at least twice as fast as the sub-tree skip on a tree of 100 dependencies
  and 211 edges.
