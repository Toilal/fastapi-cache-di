# CHANGELOG

<!-- version list -->

## v0.4.0 (2026-10-20)

### Bug Fixes

- Build included routers' routes under the deps cache with warm_up
  ([#19](https://github.com/Toilal/fastapi-cache-di/pull/19),
  [`24e5c75`](https://github.com/Toilal/fastapi-cache-di/commit/24e5c7563f52ec4202baaa908255758817aedda8))

### Features

- Solve route dependencies from a plan compiled once per route
  ([#20](https://github.com/Toilal/fastapi-cache-di/pull/20),
  [`913674b`](https://github.com/Toilal/fastapi-cache-di/commit/913674b296a1ba2b177ec0d4dd9a4cc86906a017))


## v0.3.0 (2026-10-16)

### Bug Fixes

- Patch get_flat_dependant only where FastAPI still defines it
  ([#14](https://github.com/Toilal/fastapi-cache-di/pull/14),
  [`30fbb28`](https://github.com/Toilal/fastapi-cache-di/commit/30fbb2898aa08e1163b493fb3a7833f9cd4b6e12))

- Support FastAPI 0.141+ where get_flat_dependant no longer exists
  ([#14](https://github.com/Toilal/fastapi-cache-di/pull/14),
  [`30fbb28`](https://github.com/Toilal/fastapi-cache-di/commit/30fbb2898aa08e1163b493fb3a7833f9cd4b6e12))

### Features

- Call loop-safe dependencies on the event loop instead of a threadpool hop
  ([#16](https://github.com/Toilal/fastapi-cache-di/pull/16),
  [`a37d214`](https://github.com/Toilal/fastapi-cache-di/commit/a37d214e0c31012062d0edca3184aa49cbffb28a))

- Skip cached sub-trees when dependency_overrides is set too
  ([#15](https://github.com/Toilal/fastapi-cache-di/pull/15),
  [`bb8a2f7`](https://github.com/Toilal/fastapi-cache-di/commit/bb8a2f7d61764740a79b7ce0fe9b0d4bec016cb3))

- Skip re-walking the sub-tree of already-cached dependencies at request time
  ([#15](https://github.com/Toilal/fastapi-cache-di/pull/15),
  [`bb8a2f7`](https://github.com/Toilal/fastapi-cache-di/commit/bb8a2f7d61764740a79b7ce0fe9b0d4bec016cb3))


## v0.2.1 (2026-07-06)

### Bug Fixes

- Ship py.typed marker for PEP 561 type information
  ([#11](https://github.com/Toilal/fastapi-cache-di/pull/11),
  [`1a7eb6f`](https://github.com/Toilal/fastapi-cache-di/commit/1a7eb6f89c79b17910ad7e6642f2f229532cf98d))


## v0.2.0 (2026-07-05)

### Features

- Support Python 3.11 ([#10](https://github.com/Toilal/fastapi-cache-di/pull/10),
  [`d593d6f`](https://github.com/Toilal/fastapi-cache-di/commit/d593d6f55f56ebff80af0b4608066c8f426dcaad))


## v0.1.0 (2026-07-05)

- Initial Release

Changelog entries are generated automatically by
[python-semantic-release](https://python-semantic-release.readthedocs.io/) from
[Conventional Commits](https://www.conventionalcommits.org/) on release.
