# Changelog

## [0.8.0](https://github.com/fderuiter/imednet-toolkit/compare/apache-airflow-providers-imednet-v0.7.0...apache-airflow-providers-imednet-v0.8.0) (2026-11-15)


### Features

* **jobs:** implement pydantic job lifecycle state machine with monotonic transition validation ([#1674](https://github.com/fderuiter/imednet-toolkit/issues/1674)) ([a8f171e](https://github.com/fderuiter/imednet-toolkit/commit/a8f171eeb48df2108c2ad3b26716f9b4792fa047))

## [0.7.0](https://github.com/fderuiter/imednet-toolkit/compare/apache-airflow-providers-imednet-v0.6.0...apache-airflow-providers-imednet-v0.7.0) (2026-09-10)


### Features

* Add automated dead-code prevention with Vulture ([3bbcb8c](https://github.com/fderuiter/imednet-toolkit/commit/3bbcb8c7739b27d7b890e83fd6123cb040f7ac7c))
* enforce documentation presence for all workspace packages ([#1431](https://github.com/fderuiter/imednet-toolkit/issues/1431)) ([9e45d79](https://github.com/fderuiter/imednet-toolkit/commit/9e45d796714e24724ff6a35c85e92cc6602cea32))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef8d1eb](https://github.com/fderuiter/imednet-toolkit/commit/ef8d1ebf948865d456ccd46b71b0439d5bd88b90))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef6fcea](https://github.com/fderuiter/imednet-toolkit/commit/ef6fceabd5acfa823321c54852a0dea64e61778d))
* implement platform shared db connection, unified sink template, and UI component gallery ([ff99calf](https://github.com/fderuiter/imednet-toolkit/commit/ff99caf7ffeda68aa8bd8f8bc494b1e3cb9eb7b5))
* support dynamic environment-specific routing and schema migration ([#1427](https://github.com/fderuiter/imednet-toolkit/issues/1427)) ([72180a3](https://github.com/fderuiter/imednet-toolkit/commit/72180a3d58d188b44f96e9e231eaa1ee9c06643a))
* synchronize unified documentation and resolve TODOs ([0a9c9f8](https://github.com/fderuiter/imednet-toolkit/commit/0a9c9f8d3eea0538f090961ef3e738c00c047576))


### Bug Fixes

* **airflow:** add missing type annotation for job in sensors.py ([0650818](https://github.com/fderuiter/imednet-toolkit/commit/06508181cfebcdd3b6b610fb3de68badb41370f9))
* bypass respx_mock guard in live test suite via path-based detection ([73d85de](https://github.com/fderuiter/imednet-toolkit/commit/73d85de20125d15e948151a15b4d2900bf1b00db))
* **providers-airflow:** resolve mypy errors and maintain test coverage ([a33693a](https://github.com/fderuiter/imednet-toolkit/commit/a33693af102f76aafb090616b0aa1f31943d9db4))
* **providers-airflow:** run ruff format on operators/export.py to fix CI failure ([d64e909](https://github.com/fderuiter/imednet-toolkit/commit/d64e909ead67b66daec758550e88a5182bea0370))
* **providers-airflow:** update Airflow 3 imports for Context and BaseHook ([c28af62](https://github.com/fderuiter/imednet-toolkit/commit/c28af62e6b759c294c17018540c711dc03e4c792))
* **providers-airflow:** use direct import for SinkConfig type hint ([0d5840e](https://github.com/fderuiter/imednet-toolkit/commit/0d5840e540a2070cbfe395a327c252e4af320581))
* **providers:** use correct spi import path for sink_base ([1020a0d](https://github.com/fderuiter/imednet-toolkit/commit/1020a0dd43f0f16cf98c27b839811d5253a32b53))
* reconcile cross-platform dependencies, linter rules, and verification gates ([91ccda7](https://github.com/fderuiter/imednet-toolkit/commit/91ccda7f6b87ce9ed555c19d9099a5de044b7ab9))
* support "SUCCESS" state in smoke tests and fix async unit mocks ([3050a31](https://github.com/fderuiter/imednet-toolkit/commit/3050a312361bcb8d478f43e9e987b5dff071150c))


### Documentation

* add comprehensive self-contained READMEs for Airflow provider, Sinks plugin, and Workflows plugin ([452066a](https://github.com/fderuiter/imednet-toolkit/commit/452066a973b1d74c2a71fbbdd4a854da27b0b96b))
* enforce strict docstring governance and fix sphinx warnings ([34ae046](https://github.com/fderuiter/imednet-toolkit/commit/34ae046911e03b978eb5c237508d5241bd4ab3d8))

## [0.6.0](https://github.com/fderuiter/imednet-python-sdk/compare/apache-airflow-providers-imednet-v0.5.2...apache-airflow-providers-imednet-v0.6.0) (2026-05-27)


### Features

* **airflow:** lower provider floor and isolate amazon dependency ([da1af1a](https://github.com/fderuiter/imednet-python-sdk/commit/da1af1a5df9fc299fb2bb1661f56baab76a12d6f))
* extend airflow hook with discovery-safe sdk helpers ([96a024e](https://github.com/fderuiter/imednet-python-sdk/commit/96a024e7761aa57247e30db47dab06985d43b2b7))
* harden airflow export operator mapping ([d5fcf9d](https://github.com/fderuiter/imednet-python-sdk/commit/d5fcf9d7b9cb7e9e123178e01eef381b284d820c))


### Bug Fixes

* **airflow:** require Airflow 3.2+ and harden provider compatibility ([9966b58](https://github.com/fderuiter/imednet-python-sdk/commit/9966b582afcecb6cd99cd6d92af797ba3b4929ef))


### Documentation

* **airflow:** clarify Context import fallback paths ([d3c6d43](https://github.com/fderuiter/imednet-python-sdk/commit/d3c6d43d4ebc5bed1d03e4d4acc460d7e140f9e2))
* clarify airflow export mapping guidance ([074c08d](https://github.com/fderuiter/imednet-python-sdk/commit/074c08d0b842ea59273e7272216bbc0b1ee2d956))

## [0.5.2](https://github.com/fderuiter/imednet-python-sdk/compare/apache-airflow-providers-imednet-v0.5.1...apache-airflow-providers-imednet-v0.5.2) (2026-05-13)


### Bug Fixes

* address PR review thread issues for plugin loading and docs refs ([2c841e0](https://github.com/fderuiter/imednet-python-sdk/commit/2c841e0288a206af737891904ee00c4b7fc666a4))
* require patched apache-airflow release ([549f82a](https://github.com/fderuiter/imednet-python-sdk/commit/549f82a68186e6417f2088e7c60c83b1aabf96f5))
* stabilize workspace dependency validation ([9b7799f](https://github.com/fderuiter/imednet-python-sdk/commit/9b7799f8c138b009cf92bed97f031bd5d29615da))
* tighten workflow init errors and airflow s3 fallback messaging ([b706b3c](https://github.com/fderuiter/imednet-python-sdk/commit/b706b3c04691566b5fd6c632a93eaea30307e419))
