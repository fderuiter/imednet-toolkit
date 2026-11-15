# Changelog

## [1.0.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-plugins-sinks-v0.2.0...imednet-plugins-sinks-v1.0.0) (2026-11-15)


### ⚠ BREAKING CHANGES

* retire scheduled smoke tests and harden validation ([#1702](https://github.com/fderuiter/imednet-toolkit/issues/1702))

### Bug Fixes

* retire scheduled smoke tests and harden validation ([#1702](https://github.com/fderuiter/imednet-toolkit/issues/1702)) ([a46cce8](https://github.com/fderuiter/imednet-toolkit/commit/a46cce8759edd1fe4035cdf3075a441d8fe1b8a6))

## [0.2.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-plugins-sinks-v0.1.0...imednet-plugins-sinks-v0.2.0) (2026-09-10)


### Features

* decouple database sinks into unified plugins package ([47850f2](https://github.com/fderuiter/imednet-toolkit/commit/47850f20d696ea3840dcc3aaa00585a9ceef98e4))
* enforce documentation presence for all workspace packages ([#1431](https://github.com/fderuiter/imednet-toolkit/issues/1431)) ([9e45d79](https://github.com/fderuiter/imednet-toolkit/commit/9e45d796714e24724ff6a35c85e92cc6602cea32))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef8d1eb](https://github.com/fderuiter/imednet-toolkit/commit/ef8d1ebf948865d456ccd46b71b0439d5bd88b90))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef6fcea](https://github.com/fderuiter/imednet-toolkit/commit/ef6fceabd5acfa823321c54852a0dea64e61778d))
* implement platform shared db connection, unified sink template, and UI component gallery ([ff99calf](https://github.com/fderuiter/imednet-toolkit/commit/ff99caf7ffeda68aa8bd8f8bc494b1e3cb9eb7b5))
* implement protocol-validated direct SDK registration ([63ea455](https://github.com/fderuiter/imednet-toolkit/commit/63ea455b59405c77e24ee15d31a2affdf4fdd236))
* migrate sinks to centralized mapper with enrichment engine ([bdaeadb](https://github.com/fderuiter/imednet-toolkit/commit/bdaeadb6dce7ce48fda3dc41e775bd51c2c2416e))
* support dynamic environment-specific routing and schema migration ([#1427](https://github.com/fderuiter/imednet-toolkit/issues/1427)) ([72180a3](https://github.com/fderuiter/imednet-toolkit/commit/72180a3d58d188b44f96e9e231eaa1ee9c06643a))


### Bug Fixes

* address upstream API drift for clinical studies and dynamic models ([c9c15e2](https://github.com/fderuiter/imednet-toolkit/commit/c9c15e264255f46793fb1e178c982d9737e043a6))
* bypass respx_mock guard in live test suite via path-based detection ([73d85de](https://github.com/fderuiter/imednet-toolkit/commit/73d85de20125d15e948151a15b4d2900bf1b00db))
* **ci:** resolve Quality & Security failures for plugins-sinks ([6ddcc48](https://github.com/fderuiter/imednet-toolkit/commit/6ddcc486438982cc6b8b2f3616b774ffbdc02cf4))
* **docs:** update export destination code snippets for typed configuration ([0e0934b](https://github.com/fderuiter/imednet-toolkit/commit/0e0934b480c919db3ebf9d70e80fea335844dcc6))
* monorepo F401 and F841 automated cleanup ([049ce38](https://github.com/fderuiter/imednet-toolkit/commit/049ce38e492c29dc6f4d4dfd64fdc7476cb89914))
* resolve ruff formatting, missing docstrings, and pin numpy to fix mypy failures in CI ([9166e51](https://github.com/fderuiter/imednet-toolkit/commit/9166e51751898090949849c2dae56a6e78b623f9))
* **sinks:** Update auth type hint default to valid tuple in Neo4j config ([fc44105](https://github.com/fderuiter/imednet-toolkit/commit/fc44105a02b523eb438950cc1102713d1079f4e7))


### Documentation

* add comprehensive self-contained READMEs for Airflow provider, Sinks plugin, and Workflows plugin ([452066a](https://github.com/fderuiter/imednet-toolkit/commit/452066a973b1d74c2a71fbbdd4a854da27b0b96b))
* enforce strict docstring governance and fix sphinx warnings ([34ae046](https://github.com/fderuiter/imednet-toolkit/commit/34ae046911e03b978eb5c237508d5241bd4ab3d8))
* integrate Sinks package and fix workflow guides ([dda2490](https://github.com/fderuiter/imednet-toolkit/commit/dda249030b73741879db6687057d97ac73ccffe7))
* mirror physical package structure in API reference ([8e5d2db](https://github.com/fderuiter/imednet-toolkit/commit/8e5d2db33c8765185de787a26bdf8466d5f2a880))
* unified markdown-to-docs SSOT ([e7e84fc](https://github.com/fderuiter/imednet-toolkit/commit/e7e84fc5261c72014e93508d4083b7ff129d7b23))

## Changelog

All notable changes to this project will be documented in this file.
