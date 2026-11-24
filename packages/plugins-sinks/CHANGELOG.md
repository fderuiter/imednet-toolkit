# Changelog

## [1.1.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-plugins-sinks-v1.0.1...imednet-plugins-sinks-v1.1.0) (2026-11-23)


### Features

* centralize default PHI masking across enrichment and exports ([#1936](https://github.com/fderuiter/imednet-toolkit/issues/1936)) ([99a0bc0](https://github.com/fderuiter/imednet-toolkit/commit/99a0bc0aef9d389c48a53d1841ddb10f23516d33))
* embed CDISC SDTM transformation and standards readiness in core SPI ([#2002](https://github.com/fderuiter/imednet-toolkit/issues/2002)) ([1d7ca31](https://github.com/fderuiter/imednet-toolkit/commit/1d7ca3164a958194be9aae5e3e5746912de78112))
* enforce mode-safe plugin protocols and static types ([#2000](https://github.com/fderuiter/imednet-toolkit/issues/2000)) ([5c46bfa](https://github.com/fderuiter/imednet-toolkit/commit/5c46bfaf7f321e7026f5d6fbabfcec5acee2e954))
* merge tabular records with Snowflake schema evolution ([a596cc1](https://github.com/fderuiter/imednet-toolkit/commit/a596cc154cc426f5c3c51139139b281e0951c1ca))
* **spi:** introduce formal SPI sink_base and dynamic entry point discovery ([#1773](https://github.com/fderuiter/imednet-toolkit/issues/1773)) ([f5817de](https://github.com/fderuiter/imednet-toolkit/commit/f5817de89f43a6f0916cfb8604e1f55c7c2afca7))
* unify streaming batches and ledger checkpoints via SPI ([#1912](https://github.com/fderuiter/imednet-toolkit/issues/1912)) ([820d2b6](https://github.com/fderuiter/imednet-toolkit/commit/820d2b621ec78ff2f7b10d6ea9a62174a578d94e))


### Bug Fixes

* **airflow:** sync provider aliases and isolate dummy-Airflow test modules ([#2007](https://github.com/fderuiter/imednet-toolkit/issues/2007)) ([c4236be](https://github.com/fderuiter/imednet-toolkit/commit/c4236be941cb2d1281b7d3870378debee74e1b73))
* align sink showcases and enforce symbol-level showcase validation ([#1920](https://github.com/fderuiter/imednet-toolkit/issues/1920)) ([0536005](https://github.com/fderuiter/imednet-toolkit/commit/053600566fd550f3379edd35a3ce4d04125e2994))
* **docs:** fix examples and expand docs verification scripts ([#1945](https://github.com/fderuiter/imednet-toolkit/issues/1945)) ([78b3ad8](https://github.com/fderuiter/imednet-toolkit/commit/78b3ad859e67b930bbb396c12622914cca1d0892))
* **models:** infer contract types and synchronize Postman schemas ([#1889](https://github.com/fderuiter/imednet-toolkit/issues/1889)) ([64efe0f](https://github.com/fderuiter/imednet-toolkit/commit/64efe0f3bd62a19a86e99b56fabf7b6ead7a33a1))
* resolve Airflow destination connections and merge Snowflake batches ([c2a1b79](https://github.com/fderuiter/imednet-toolkit/commit/c2a1b7955b86c5c58e9339a80ffd90f3ddf94c07))
* **security:** mask PHI at mapper ingress via SPI ([#1911](https://github.com/fderuiter/imednet-toolkit/issues/1911)) ([432d8a5](https://github.com/fderuiter/imednet-toolkit/commit/432d8a5e3e7fe784f6143d7eae55206d770a7bd2))
* **security:** standardize PHI redaction on SensitivityRegistry ([#1923](https://github.com/fderuiter/imednet-toolkit/issues/1923)) ([cb52acc](https://github.com/fderuiter/imednet-toolkit/commit/cb52accc22789ee6f805bc8f8d6cc1f2c9379dad))
* **security:** validate SDTM sequence keys before building Neo4j Cypher ([#2028](https://github.com/fderuiter/imednet-toolkit/issues/2028)) ([758605b](https://github.com/fderuiter/imednet-toolkit/commit/758605b7ffb2c985600c2360717e402ec2a241c0))
* **sinks:** centralize SPI mapping and add direct Neo4j subject-record edges ([#1949](https://github.com/fderuiter/imednet-toolkit/issues/1949)) ([2342129](https://github.com/fderuiter/imednet-toolkit/commit/234212996a823c21c47e35adb9c5bf239de32648))


### Performance Improvements

* adopt lazy package barrels and deferred imports ([#2013](https://github.com/fderuiter/imednet-toolkit/issues/2013)) ([733cfe7](https://github.com/fderuiter/imednet-toolkit/commit/733cfe736f52423a02fe5577936e4012dad96884))
* **sinks:** streamline warehouse Arrow conversion to single-pass checks ([#1952](https://github.com/fderuiter/imednet-toolkit/issues/1952)) ([40197f6](https://github.com/fderuiter/imednet-toolkit/commit/40197f6daf6e7723d2430671bff3e85ae9259cc3))


### Documentation

* align guides, demos, and READMEs to current fixtures ([#2016](https://github.com/fderuiter/imednet-toolkit/issues/2016)) ([86c2aec](https://github.com/fderuiter/imednet-toolkit/commit/86c2aec01ba3cd2f1a640e23e39f53b3c019cc8b))

## [1.0.1](https://github.com/fderuiter/imednet-toolkit/compare/imednet-plugins-sinks-v1.0.0...imednet-plugins-sinks-v1.0.1) (2026-11-16)


### Bug Fixes

* **airflow:** restore export delegation and core 1.x installation ([#1709](https://github.com/fderuiter/imednet-toolkit/issues/1709)) ([1d17233](https://github.com/fderuiter/imednet-toolkit/commit/1d172334ae24553d69b8941d9fc0fa13a02d19d9))
* resolve explicit authentication without placeholder credentials ([#1749](https://github.com/fderuiter/imednet-toolkit/issues/1749)) ([154e593](https://github.com/fderuiter/imednet-toolkit/commit/154e593c141b96e1233f072e7d9f4a9ee5ceee0d))
* **retries:** enforce pagination ownership and configured sink attempts ([#1710](https://github.com/fderuiter/imednet-toolkit/issues/1710)) ([ac185b2](https://github.com/fderuiter/imednet-toolkit/commit/ac185b275ee9055e46f9e62f0650a1c720ae99a5))
* **security:** validate snowflake identifiers and sanitize export batch identifiers ([#1730](https://github.com/fderuiter/imednet-toolkit/issues/1730)) ([aeb8e08](https://github.com/fderuiter/imednet-toolkit/commit/aeb8e08b9ff2e9656f778f4381b074747bbf352d))


### Performance Improvements

* **sinks:** reuse centralized mapper per batch in document and graph sinks ([#1713](https://github.com/fderuiter/imednet-toolkit/issues/1713)) ([e115c6a](https://github.com/fderuiter/imednet-toolkit/commit/e115c6a64d3a6f42a6adacfb055760988b255f7f))

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
