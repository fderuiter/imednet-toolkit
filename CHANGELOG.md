# Changelog

This changelog tracks releases across the following workspace packages:
* `imednet` (`packages/core`)
* `imednet-plugins-sinks` (`packages/plugins-sinks`)
* `imednet-tui` (`packages/plugins-tui`)
* `imednet-workflows` (`packages/plugins-workflows`)
* `apache-airflow-providers-imednet` (`packages/providers-airflow`)

## Per-Package Changelogs

Active release notes (`> 0.6.0`) are maintained independently per workspace package by `release-please` and published on [GitHub Releases](https://github.com/fderuiter/imednet-toolkit/releases):

* [`packages/core/CHANGELOG.md`](packages/core/CHANGELOG.md) — `imednet`
* [`packages/plugins-sinks/CHANGELOG.md`](packages/plugins-sinks/CHANGELOG.md) — `imednet-plugins-sinks`
* [`packages/plugins-tui/CHANGELOG.md`](packages/plugins-tui/CHANGELOG.md) — `imednet-tui`
* [`packages/plugins-workflows/CHANGELOG.md`](packages/plugins-workflows/CHANGELOG.md) — `imednet-workflows`
* [`packages/providers-airflow/CHANGELOG.md`](packages/providers-airflow/CHANGELOG.md) — `apache-airflow-providers-imednet`

The entries below preserve historical pre-workspace release notes (`<= 0.6.0`).

## [0.6.0](https://github.com/fderuiter/imednet-toolkit/compare/v0.5.6...v0.6.0) (2026-05-12)


### Build & Tooling

* Adopted PEP 621 project metadata as the single source of truth in all `pyproject.toml` files using the `[project]` schema.
* Replaced Poetry with Hatchling (`hatchling.build`) and standard Python tools (`pip`, `uv`, `build`) for dependency management and building.
* Removed `[tool.poetry]` configurations and `poetry.lock` files across the monorepo workspace.
* Added automated semantic versioning and release PR generation via `release-please`.

### Features

* add resilient Pydantic base model with extra=ignore for all SDK models ([26b2aa6](https://github.com/fderuiter/imednet-toolkit/commit/26b2aa6876325026076e72dcd9ae68dd3199a0f8))


### Bug Fixes

* remove shared endpoint context dependency for study resolution ([44b94ee](https://github.com/fderuiter/imednet-toolkit/commit/44b94ee6f67b5ea440f55e65389cdb90d2a13577))
* rewrite ImednetBaseModel docstring to valid RST (no bullet list) ([e38a3d0](https://github.com/fderuiter/imednet-toolkit/commit/e38a3d06e8447fd3436d05699c03739286c1e1d0))
* wrap long docstring in test to satisfy ruff E501 ([0d66e6a](https://github.com/fderuiter/imednet-toolkit/commit/0d66e6af0aa74c810059174285d3bb764e7fd581))


### Documentation

* Replace dummy URLs in docs with grounded defaults ([7cad35c](https://github.com/fderuiter/imednet-toolkit/commit/7cad35caa900c736a35f37c9173475439dfd1ff0))

## [0.5.6](https://github.com/fderuiter/imednet-toolkit/compare/v0.5.5...v0.5.6) (2026-05-11)


### Bug Fixes

* address PR review-thread follow-up issues ([efdd101](https://github.com/fderuiter/imednet-toolkit/commit/efdd1012832f039119c5debaf4d55d07b9bbfe35))
* apply review feedback on respx mock and live-tests marker ([0346164](https://github.com/fderuiter/imednet-toolkit/commit/0346164a7f149ac076020c5a53d7cb7ffaf9fde9))
* make ImednetSDK.__aexit__ raise TypeError to fully poison async protocol ([7416247](https://github.com/fderuiter/imednet-toolkit/commit/7416247fba6c9803bb641e50c0de1c7e17174a08))
* poison async context manager protocol on ImednetSDK and add tests ([f6917a6](https://github.com/fderuiter/imednet-toolkit/commit/f6917a6ab953567d0d4f5fb061d564841dd59835))
* remove mutable study state from SDK client ([d4fa361](https://github.com/fderuiter/imednet-toolkit/commit/d4fa3612067dd837d894297af51e230e5f5c3753))
* upgrade urllib3 to 2.7.0 to resolve CVE-2026-44432 and CVE-2026-44431 ([d837e5a](https://github.com/fderuiter/imednet-toolkit/commit/d837e5abfeede6a203c65bae8bc84b6a49b54028))

## [0.5.5](https://github.com/fderuiter/imednet-toolkit/compare/v0.5.4...v0.5.5) (2026-05-08)


### Bug Fixes

* enforce idempotency check in DefaultRetryPolicy to prevent unsafe retries ([#898](https://github.com/fderuiter/imednet-toolkit/issues/898)) ([74c33cb](https://github.com/fderuiter/imednet-toolkit/commit/74c33cbb0b5d4630d8f7f2362550fc3cc4e6f15b))
* remove test-only client request wrappers ([#896](https://github.com/fderuiter/imednet-toolkit/issues/896)) ([7e5fa99](https://github.com/fderuiter/imednet-toolkit/commit/7e5fa99620389c9fd1c3c39a47d314e892b83b67))

## [0.5.4](https://github.com/fderuiter/imednet-toolkit/compare/v0.5.3...v0.5.4) (2026-05-08)


### Documentation

* fix docs warning failures from generated API toctrees ([#895](https://github.com/fderuiter/imednet-toolkit/issues/895)) ([78a5707](https://github.com/fderuiter/imednet-toolkit/commit/78a5707f6351971308b5377af248026b73ce6f89))
* replace static API reference RST files with sphinx-apidoc automation ([#889](https://github.com/fderuiter/imednet-toolkit/issues/889)) ([645918b](https://github.com/fderuiter/imednet-toolkit/commit/645918bf8c3bd622dc84b43764a11afdf5ebd866))

## [0.5.3](https://github.com/fderuiter/imednet-toolkit/compare/v0.5.2...v0.5.3) (2026-05-07)


### Bug Fixes

* address code review - improve close() error message and add __exit__ test coverage ([deb45c7](https://github.com/fderuiter/imednet-toolkit/commit/deb45c797e7e486b35cbed13fcb714c490186c7a))
* clarify close() error message, override close() in AsyncImednetSDK to raise TypeError, add test, fix black formatting ([91c5f58](https://github.com/fderuiter/imednet-toolkit/commit/91c5f58db1c6c318fc7ecec15d83c5e227774a18))
* enforce strict async/sync lifecycle separation in SDK (remove asyncio bridging anti-pattern) ([38c93a7](https://github.com/fderuiter/imednet-toolkit/commit/38c93a7950d0137a648123b37e46a7b7ebf0d9df))

## [0.5.2](https://github.com/fderuiter/imednet-toolkit/compare/v0.5.1...v0.5.2) (2026-05-07)


### Bug Fixes

* address PR review thread issues for plugin loading and docs refs ([2c841e0](https://github.com/fderuiter/imednet-toolkit/commit/2c841e0288a206af737891904ee00c4b7fc666a4))
* tighten workflow init errors and airflow s3 fallback messaging ([b706b3c](https://github.com/fderuiter/imednet-toolkit/commit/b706b3c04691566b5fd6c632a93eaea30307e419))


### Documentation

* fix sub-package path consistency in AGENTS.md ([00c4847](https://github.com/fderuiter/imednet-toolkit/commit/00c4847f19fdc39ebf51a498182aeb387d96fb5e))
* rewrite AGENTS.md per issue phases 1-5 ([b242550](https://github.com/fderuiter/imednet-toolkit/commit/b24255051bffa6aed2aa111928b575eae6ba10bb))

## [0.5.1](https://github.com/fderuiter/imednet-toolkit/compare/v0.5.0...v0.5.1) (2026-05-07)


### Bug Fixes

* apply review-thread metadata and release workflow corrections ([4ebd11b](https://github.com/fderuiter/imednet-toolkit/commit/4ebd11b7225e1d0e98f521bec496139330303a96))


### Documentation

* add detailed docstring and doctest examples to parse_bool ([81d868f](https://github.com/fderuiter/imednet-toolkit/commit/81d868f763f37a1ab0f36e56e7c97542fef5a4d4))
* add explicit release workflow guidance ([6fe54ff](https://github.com/fderuiter/imednet-toolkit/commit/6fe54ffedea2f6027d8ad1cd71b4a3075af16bc6))
* **ci:** finalize release-please and conventional commit guidance ([c8a8cf9](https://github.com/fderuiter/imednet-toolkit/commit/c8a8cf92f4ac1c0afa09bb662ea5a940b7039450))
* document IMEDNET_BASE_URL in Quick Start ([ca3c0bd](https://github.com/fderuiter/imednet-toolkit/commit/ca3c0bdda05a415ac51159086dcbb1aa0034dfcb))
