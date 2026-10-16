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
  routes and route groups). The root call never receives a cache, so it is never
  skipped.
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
