# Changelog

## [1.2.0](https://github.com/fderuiter/imednet-toolkit/compare/apache-airflow-providers-imednet-v1.1.0...apache-airflow-providers-imednet-v1.2.0) (2026-11-23)


### Features

* **airflow:** add provider metadata, deferrable trigger, and SDK parity ([#2004](https://github.com/fderuiter/imednet-toolkit/issues/2004)) ([2763e22](https://github.com/fderuiter/imednet-toolkit/commit/2763e22cfe08226230345de354731620d22909cf))
* **airflow:** align Airflow 3 provider UI metadata, template fields, and SQL exports ([#1999](https://github.com/fderuiter/imednet-toolkit/issues/1999)) ([fb5b4c0](https://github.com/fderuiter/imednet-toolkit/commit/fb5b4c0dcf68200855f79f00e0bf75ef07e3eb65))
* centralize default PHI masking across enrichment and exports ([#1936](https://github.com/fderuiter/imednet-toolkit/issues/1936)) ([99a0bc0](https://github.com/fderuiter/imednet-toolkit/commit/99a0bc0aef9d389c48a53d1841ddb10f23516d33))
* dynamic sdk sinks resolution with architectural import guard ([#1865](https://github.com/fderuiter/imednet-toolkit/issues/1865)) ([50ef9e7](https://github.com/fderuiter/imednet-toolkit/commit/50ef9e728bde341e322b58a25adaea3dec5283dc))
* embed CDISC SDTM transformation and standards readiness in core SPI ([#2002](https://github.com/fderuiter/imednet-toolkit/issues/2002)) ([1d7ca31](https://github.com/fderuiter/imednet-toolkit/commit/1d7ca3164a958194be9aae5e3e5746912de78112))
* enforce mode-safe plugin protocols and static types ([#2000](https://github.com/fderuiter/imednet-toolkit/issues/2000)) ([5c46bfa](https://github.com/fderuiter/imednet-toolkit/commit/5c46bfaf7f321e7026f5d6fbabfcec5acee2e954))
* merge tabular records with Snowflake schema evolution ([a596cc1](https://github.com/fderuiter/imednet-toolkit/commit/a596cc154cc426f5c3c51139139b281e0951c1ca))
* unify streaming batches and ledger checkpoints via SPI ([#1912](https://github.com/fderuiter/imednet-toolkit/issues/1912)) ([820d2b6](https://github.com/fderuiter/imednet-toolkit/commit/820d2b621ec78ff2f7b10d6ea9a62174a578d94e))


### Bug Fixes

* **airflow:** isolate sensor summaries and credential guards ([#2053](https://github.com/fderuiter/imednet-toolkit/issues/2053)) ([9ddbb06](https://github.com/fderuiter/imednet-toolkit/commit/9ddbb06782ffbceeeb1375dc5847323590d3f000))
* **airflow:** repair Airflow 3 provider discovery, XCom checkpoints, and DuckDB dispatch ([#1970](https://github.com/fderuiter/imednet-toolkit/issues/1970)) ([e778b17](https://github.com/fderuiter/imednet-toolkit/commit/e778b17e91025a11e0aeb11d8eee8d558cb09700))
* **airflow:** restore get_conn method on ImednetHook ([#1826](https://github.com/fderuiter/imednet-toolkit/issues/1826)) ([263678f](https://github.com/fderuiter/imednet-toolkit/commit/263678f4332f56ab966b33b2b1f872fe3abd8ec2))
* **airflow:** sync provider aliases and isolate dummy-Airflow test modules ([#2007](https://github.com/fderuiter/imednet-toolkit/issues/2007)) ([c4236be](https://github.com/fderuiter/imednet-toolkit/commit/c4236be941cb2d1281b7d3870378debee74e1b73))
* **airflow:** use Airflow Variable checkpoints and verify Job resultUrl ([#1995](https://github.com/fderuiter/imednet-toolkit/issues/1995)) ([ce2e65c](https://github.com/fderuiter/imednet-toolkit/commit/ce2e65c14ce46ad999d80498d4f78d9d0fcbc7d2))
* **core,airflow:** guard optional pandas import and stabilize Airflow 3.4 CI suites ([#2005](https://github.com/fderuiter/imednet-toolkit/issues/2005)) ([b0e99ff](https://github.com/fderuiter/imednet-toolkit/commit/b0e99ff852d5e19c3d88b5d312a0a4cb39da344f))
* **docs:** fix examples and expand docs verification scripts ([#1945](https://github.com/fderuiter/imednet-toolkit/issues/1945)) ([78b3ad8](https://github.com/fderuiter/imednet-toolkit/commit/78b3ad859e67b930bbb396c12622914cca1d0892))
* pass resolved Config and scope HTTP client lifecycles ([#1929](https://github.com/fderuiter/imednet-toolkit/issues/1929)) ([ff6775b](https://github.com/fderuiter/imednet-toolkit/commit/ff6775b444dac188d128533afa0e5cdc87c48f14))
* resolve Airflow destination connections and merge Snowflake batches ([c2a1b79](https://github.com/fderuiter/imednet-toolkit/commit/c2a1b7955b86c5c58e9339a80ffd90f3ddf94c07))
* **security:** standardize PHI redaction on SensitivityRegistry ([#1923](https://github.com/fderuiter/imednet-toolkit/issues/1923)) ([cb52acc](https://github.com/fderuiter/imednet-toolkit/commit/cb52accc22789ee6f805bc8f8d6cc1f2c9379dad))
* **security:** validate and encode API path segments strictly ([#1968](https://github.com/fderuiter/imednet-toolkit/issues/1968)) ([78e605b](https://github.com/fderuiter/imednet-toolkit/commit/78e605bea40845d6a3130aafe432091b58357b49))
* unify opt-in quality gates and optional variable readiness scoring ([#2046](https://github.com/fderuiter/imednet-toolkit/issues/2046)) ([f2c54b8](https://github.com/fderuiter/imednet-toolkit/commit/f2c54b89084f84d6e482ed7eaa2795e529336131))


### Performance Improvements

* **export:** stream study records once across per-form exports ([#1996](https://github.com/fderuiter/imednet-toolkit/issues/1996)) ([3fec197](https://github.com/fderuiter/imednet-toolkit/commit/3fec1974fecc1f27f55c220aefc744910cbe2d8a))


### Documentation

* align guides, demos, and READMEs to current fixtures ([#2016](https://github.com/fderuiter/imednet-toolkit/issues/2016)) ([86c2aec](https://github.com/fderuiter/imednet-toolkit/commit/86c2aec01ba3cd2f1a640e23e39f53b3c019cc8b))

## [1.1.0](https://github.com/fderuiter/imednet-toolkit/compare/apache-airflow-providers-imednet-v1.0.0...apache-airflow-providers-imednet-v1.1.0) (2026-11-16)


### Features

* **core:** implement built-in simulation transport and in-memory fixture state ([#1735](https://github.com/fderuiter/imednet-toolkit/issues/1735)) ([193ea00](https://github.com/fderuiter/imednet-toolkit/commit/193ea008c01545b3c562e66f3bef6c5f3580d7b5))


### Bug Fixes

* **airflow:** restore export delegation and core 1.x installation ([#1709](https://github.com/fderuiter/imednet-toolkit/issues/1709)) ([1d17233](https://github.com/fderuiter/imednet-toolkit/commit/1d172334ae24553d69b8941d9fc0fa13a02d19d9))
* **airflow:** wrap DuckDB export delegation in provider ([#1741](https://github.com/fderuiter/imednet-toolkit/issues/1741)) ([d0cd1b7](https://github.com/fderuiter/imednet-toolkit/commit/d0cd1b727ad5151d12170bdebe991268f49f8dd3))
* **providers-airflow:** guard lazy provider imports and forward subprocess env ([#1719](https://github.com/fderuiter/imednet-toolkit/issues/1719)) ([6842ca4](https://github.com/fderuiter/imednet-toolkit/commit/6842ca475d224d0e4f69fccc0b9d792371995363))
* resolve explicit authentication without placeholder credentials ([#1749](https://github.com/fderuiter/imednet-toolkit/issues/1749)) ([154e593](https://github.com/fderuiter/imednet-toolkit/commit/154e593c141b96e1233f072e7d9f4a9ee5ceee0d))
* **retries:** enforce pagination ownership and configured sink attempts ([#1710](https://github.com/fderuiter/imednet-toolkit/issues/1710)) ([ac185b2](https://github.com/fderuiter/imednet-toolkit/commit/ac185b275ee9055e46f9e62f0650a1c720ae99a5))

## [1.0.0](https://github.com/fderuiter/imednet-toolkit/compare/apache-airflow-providers-imednet-v0.8.0...apache-airflow-providers-imednet-v1.0.0) (2026-11-15)


### ⚠ BREAKING CHANGES

* retire scheduled smoke tests and harden validation ([#1702](https://github.com/fderuiter/imednet-toolkit/issues/1702))

### Bug Fixes

* retire scheduled smoke tests and harden validation ([#1702](https://github.com/fderuiter/imednet-toolkit/issues/1702)) ([a46cce8](https://github.com/fderuiter/imednet-toolkit/commit/a46cce8759edd1fe4035cdf3075a441d8fe1b8a6))

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
