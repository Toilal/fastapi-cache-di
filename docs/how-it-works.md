How it works
============

`patch_fastapi_deps_cache()` replaces three functions in
`fastapi.dependencies.utils` (plus the `fastapi.routing` and
`fastapi.openapi.utils` re-imports) with caching wrappers backed by a
[`DepsCache`](./api.md#depscache). `unpatch_fastapi_deps_cache()` restores the
originals.

## The three cached functions

FastAPI introspects every route's dependency tree at startup with no caching.
Shared dependencies (an auth check, a DB handle, a role guard …) are
re-introspected from scratch for every route that uses them. The three functions
on that hot path, and how each is keyed:

| Function | Cache key | Why |
|---|---|---|
| `get_typed_signature` | `id(call)` | avoid repeated signature parsing |
| `get_dependant` | `(id(call), path params, name, use_cache, scope kwargs)` | avoid redundant tree introspection |
| `get_flat_dependant` | `(id(dependant), parent_oauth_scopes)` | the dominant startup cost |

`get_flat_dependant` is the dominant cost: FastAPI calls it once per route in
`APIRoute.__init__` to flatten the dependency tree, recursing through every
sub-dependency without caching. Caching the recursive results turns
`O(routes × tree-depth)` into `O(routes + unique-deps)`.

## Where the patch is applied

Each function is patched in every module that holds its own reference:

- **`get_typed_signature`** — patched in `fastapi.dependencies.utils` only
  (that is the sole caller).
- **`get_dependant`** — patched in `fastapi.dependencies.utils` (for the
  recursive calls) and in `fastapi.routing` (which imports its own reference).
- **`get_flat_dependant`** — patched in `fastapi.dependencies.utils`
  (self-recursive calls), `fastapi.openapi.utils` (all versions), and
  `fastapi.routing`. `routing` only imports `get_flat_dependant` from FastAPI
  0.112.4 onwards, so it is patched there only if present (and remembered, to be
  restored on unpatch).

The `openapi.utils` patch is effectively a no-op on current FastAPI: both call
sites there pass `skip_repeats=True`, which the wrapper short-circuits straight
to the original without caching. It is installed for symmetry and as a safety
net.

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
