# Changelog

## [0.9.1](https://github.com/fderuiter/imednet-toolkit/compare/imednet-v0.9.0...imednet-v0.9.1) (2026-09-18)


### Bug Fixes

* **ci:** reconcile accessibility workflow permissions, vpat path, and cross-environment mypy rules ([0e9b0f4](https://github.com/fderuiter/imednet-toolkit/commit/0e9b0f4c186c1714e69788141b9488fb3b489033))

## [0.9.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-v0.8.0...imednet-v0.9.0) (2026-09-10)


### Features

* Add automated dead-code prevention with Vulture ([3bbcb8c](https://github.com/fderuiter/imednet-toolkit/commit/3bbcb8c7739b27d7b890e83fd6123cb040f7ac7c))
* add live test charter with pass/skip/fail contract and update discovery/conftest/smoke semantics ([641f542](https://github.com/fderuiter/imednet-toolkit/commit/641f542c6f1d327d103a88e1a7fdbbfcef9b5132))
* cli end-to-end failure-path and output UX coverage ([811e190](https://github.com/fderuiter/imednet-toolkit/commit/811e1901453e894461d25f7688cfb0f037a57d4e))
* cli end-to-end failure-path and output UX coverage ([ce8888a](https://github.com/fderuiter/imednet-toolkit/commit/ce8888ad264946a0a7a16c1bb4a60d2675be73dc))
* **core:** implement granular entrypoint registry architecture for workflows ([da4b60d](https://github.com/fderuiter/imednet-toolkit/commit/da4b60d8568254b316284d9c89c89cf860a64f41))
* **core:** support IMEDNET_TIMEOUT and IMEDNET_STRICT_MODE settings ([49fd8bd](https://github.com/fderuiter/imednet-toolkit/commit/49fd8bd805172eef61a4e650ee1f7afe520151fb))
* decouple database sinks into unified plugins package ([47850f2](https://github.com/fderuiter/imednet-toolkit/commit/47850f20d696ea3840dcc3aaa00585a9ceef98e4))
* define live test suite contract and implement charter semantics ([642ac79](https://github.com/fderuiter/imednet-toolkit/commit/642ac79e37185a6de21472bf674f2f356d4f1531))
* enforce boundary isolation and align downstream utility logic ([dd39824](https://github.com/fderuiter/imednet-toolkit/commit/dd398240a87fa9f2e4a79e7f42febd170eed9d9d))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef8d1eb](https://github.com/fderuiter/imednet-toolkit/commit/ef8d1ebf948865d456ccd46b71b0439d5bd88b90))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef6fcea](https://github.com/fderuiter/imednet-toolkit/commit/ef6fceabd5acfa823321c54852a0dea64e61778d))
* enrich JobStatus object with auto-parsed results and metadata ([84d6083](https://github.com/fderuiter/imednet-toolkit/commit/84d6083b6f645b43bf5760da4711ea2981af93b0))
* Fully Dynamic OpenAPI Contract Resolution for automated drift validation ([#1419](https://github.com/fderuiter/imednet-toolkit/issues/1419)) ([9e1f6a7](https://github.com/fderuiter/imednet-toolkit/commit/9e1f6a7cc4f89e278628a5f46c859ddfbe10e13d))
* implement compliance-aware PHI masking for data exports ([dc19d50](https://github.com/fderuiter/imednet-toolkit/commit/dc19d503b797a3b375afd7ff6d6f35b22463b2a3))
* implement Enterprise Managed Portal with SSO and multi-tenancy ([72bd4d3](https://github.com/fderuiter/imednet-toolkit/commit/72bd4d32d7f095920551ec561821fa03229a61d3))
* implement hybrid config-driven compliance file mapping ([#1420](https://github.com/fderuiter/imednet-toolkit/issues/1420)) ([6b0e01b](https://github.com/fderuiter/imednet-toolkit/commit/6b0e01be21b32d54e5ffb6c89c4e13fe50b9a9ac))
* implement platform shared db connection, unified sink template, and UI component gallery ([ff99calf](https://github.com/fderuiter/imednet-toolkit/commit/ff99caf7ffeda68aa8bd8f8bc494b1e3cb9eb7b5))
* Implement unified API contract model for model generation ([a50e1b2](https://github.com/fderuiter/imednet-toolkit/commit/a50e1b23976489b73d251b72402e3913d9b32858))
* implement unified execution middleware and centralize pagination loop ([cb6a16e](https://github.com/fderuiter/imednet-toolkit/commit/cb6a16e05092ba59cc0768793da0608f5789cc64))
* **integrations:** implement unified functional facade for data export ([14435a6](https://github.com/fderuiter/imednet-toolkit/commit/14435a62c628972e915dfaa9b61f071b070b27b5))
* migrate legacy CSV and SQL exports to Unified Tabular Sink Engine ([786e0f2](https://github.com/fderuiter/imednet-toolkit/commit/786e0f2d552ae2f016cc4560031211240064969b))
* migrate sinks to centralized mapper with enrichment engine ([bdaeadb](https://github.com/fderuiter/imednet-toolkit/commit/bdaeadb6dce7ce48fda3dc41e775bd51c2c2416e))
* schedule daily smoke tests and standardize pipeline secrets ([#1470](https://github.com/fderuiter/imednet-toolkit/issues/1470)) ([e47dc1c](https://github.com/fderuiter/imednet-toolkit/commit/e47dc1c67631a4301a3973c77948a14f7f03b89f))
* **sdk:** dynamically synthesize unified sync/async endpoint definitions ([6ab3631](https://github.com/fderuiter/imednet-toolkit/commit/6ab3631fcef1326a3cbcbc6a013441a52bcbf2bb))
* unified verification model to eliminate log parsing in CI ([#1412](https://github.com/fderuiter/imednet-toolkit/issues/1412)) ([390423a](https://github.com/fderuiter/imednet-toolkit/commit/390423ae37d7e81df838f4c5a43ee6aeec77b259))
* unify execution protocol for list operations using UniversalExecutor ([56e6ea3](https://github.com/fderuiter/imednet-toolkit/commit/56e6ea3bb97faa877c6a4f8f42be4de41e035ca6))


### Bug Fixes

* 🔒 address upstream API drift for clinical studies and dynamic models ([1dbb29d](https://github.com/fderuiter/imednet-toolkit/commit/1dbb29d5de4ab8c420d8bb56cfa349d4ad5d75f4))
* address upstream API drift for clinical studies and dynamic models ([713c59c](https://github.com/fderuiter/imednet-toolkit/commit/713c59ca9dcd099304cc1922ed1904ca633e83de))
* **airflow:** add missing type annotation for job in sensors.py ([0650818](https://github.com/fderuiter/imednet-toolkit/commit/06508181cfebcdd3b6b610fb3de68badb41370f9))
* avoid importing streamlit app during CLI setup ([f7a51bd](https://github.com/fderuiter/imednet-toolkit/commit/f7a51bd0a2bfccf85d39a3c155df77461db1b50b))
* Avoid importing the Streamlit dashboard during CLI initialization ([a521e7b](https://github.com/fderuiter/imednet-toolkit/commit/a521e7b27995669c9f6364f5a7b4fb63197c35fd))
* broaden site/subject eligibility in discovery and add diagnostics ([af17fae](https://github.com/fderuiter/imednet-toolkit/commit/af17fae43251f69ad81812b6705177f72195ecb3))
* broaden site/subject eligibility in discovery and add diagnostics ([1a4e590](https://github.com/fderuiter/imednet-toolkit/commit/1a4e5902ff5e4eabb512b63829c0e4884ebe0ab3))
* bypass respx_mock guard in live test suite via path-based detection ([73d85de](https://github.com/fderuiter/imednet-toolkit/commit/73d85de20125d15e948151a15b4d2900bf1b00db))
* **cli:** add fallback commands for uninstalled plugins ([55b3e73](https://github.com/fderuiter/imednet-toolkit/commit/55b3e73f1a45af17ddfec52fd95c207cc224fdda))
* **core:** resolve linting, formatting, and typing CI failures ([2a8bd0f](https://github.com/fderuiter/imednet-toolkit/commit/2a8bd0f79d38650796981a1fb7d8a6b14f844b7a))
* **core:** resolve ruff formatting error in users model ([d2355ff](https://github.com/fderuiter/imednet-toolkit/commit/d2355ffa621739ed3b8f038b652601f32ac6fd83))
* **core:** resolve ruff formatting issues in paginator.py ([0646aa8](https://github.com/fderuiter/imednet-toolkit/commit/0646aa8ba44362aba3d9fe32a6affe6b6435a219))
* **core:** resolve ruff formatting issues in sdk_convenience.py ([0affad9](https://github.com/fderuiter/imednet-toolkit/commit/0affad92da8fa9a75324753b285db5f7c55318fe))
* **docs:** remove DataFrame from __all__ to fix Sphinx autodoc error ([dc3622b](https://github.com/fderuiter/imednet-toolkit/commit/dc3622bfeaf516e006bdb622a6e0c5a07541aae4))
* **docs:** resolve sphinx autodoc errors with pandas.DataFrame and fix RET503 ([4b4bb0c](https://github.com/fderuiter/imednet-toolkit/commit/4b4bb0cff42e8996af44879e6ad4ca316d43a620))
* **export:** synchronize registry operations with re-entrant lock ([400f28a](https://github.com/fderuiter/imednet-toolkit/commit/400f28ad2e47e1348d6ee90b6f0bce304775fac4))
* handle empty strings in parse_bool to resolve API drift ([159e550](https://github.com/fderuiter/imednet-toolkit/commit/159e55073f0f6c94e3311f30164e30712ffc8784))
* monorepo F401 and F841 automated cleanup ([049ce38](https://github.com/fderuiter/imednet-toolkit/commit/049ce38e492c29dc6f4d4dfd64fdc7476cb89914))
* reconcile cross-platform dependencies, linter rules, and verification gates ([91ccda7](https://github.com/fderuiter/imednet-toolkit/commit/91ccda7f6b87ce9ed555c19d9099a5de044b7ab9))
* remove stale strict mypy ignores ([3a1bd21](https://github.com/fderuiter/imednet-toolkit/commit/3a1bd21ca07f5dcf47ec6e39ecaef9e6f1446e8c))
* remove subjectKey from RegisterSubjectRequest to fix subject registration ([a5d8b34](https://github.com/fderuiter/imednet-toolkit/commit/a5d8b34cf58ba784687071dea07c7012e6ed5003))
* remove unreachable RuntimeError statement in HTTP executor ([63debf4](https://github.com/fderuiter/imednet-toolkit/commit/63debf4bffc0de6667b91603e522de489b27b115))
* resolve core mypy and formatting issues and fix obsolete test failures ([1e792f3](https://github.com/fderuiter/imednet-toolkit/commit/1e792f3b784c1f270a71799eb60e2a62c6e02d97))
* resolve iterator regressions in CLI and workflows ([be6c52c](https://github.com/fderuiter/imednet-toolkit/commit/be6c52c49fed2663f20c45df03e79994bb30bd60))
* resolve mypy type-checking errors in core package ([2172480](https://github.com/fderuiter/imednet-toolkit/commit/2172480facbe4781b9d5469da3c2486152959c0e))
* resolve NoneType AttributeErrors in live integration tests ([db27fbd](https://github.com/fderuiter/imednet-toolkit/commit/db27fbddfd05406ac61d8737694e2a17fcd37b2b))
* resolve Postman schema path dynamically for ModelEngine ([a4f188f](https://github.com/fderuiter/imednet-toolkit/commit/a4f188f65518574846cf64a734009fa223cc8f01))
* resolve ruff formatting issues in `packages/core` ([5273e46](https://github.com/fderuiter/imednet-toolkit/commit/5273e46b567c8f3ca66d3ae1597f82dcd2aee890))
* resolve ruff formatting, missing docstrings, and pin numpy to fix mypy failures in CI ([9166e51](https://github.com/fderuiter/imednet-toolkit/commit/9166e51751898090949849c2dae56a6e78b623f9))
* satisfy dashboard mypy narrowing ([e1cebc6](https://github.com/fderuiter/imednet-toolkit/commit/e1cebc6a7dc84dac2e0b02b0c75b82e159d8281c))
* suppress semgrep warning for whitelisted dynamic import ([3b34a3e](https://github.com/fderuiter/imednet-toolkit/commit/3b34a3e4e09845e9d6df83ed334ae930c2b2fe65))
* update discovery logic to handle API drift in smoke tests ([48d3c1f](https://github.com/fderuiter/imednet-toolkit/commit/48d3c1fea7a4bad79c983ea72e9cd38394fd0f74))
* use case-insensitive equality for status checks in discovery (not substring) ([d3b352d](https://github.com/fderuiter/imednet-toolkit/commit/d3b352d416724656e3f2b44bd30b0f253bd7bad8))


### Documentation

* 🖊️ Scribe: Comprehensive update of docstring placeholders ([4e13f4b](https://github.com/fderuiter/imednet-toolkit/commit/4e13f4b28b6343e1eb402c255081f24e2212d491))
* 🖊️ Scribe: Comprehensive update of docstring placeholders and fix CI ([ef5e6dd](https://github.com/fderuiter/imednet-toolkit/commit/ef5e6dd59e073e1a4996f38a4382ff1482ab9e13))
* 🖊️ Scribe: fix placeholder docstrings in core endpoints and models ([f7465da](https://github.com/fderuiter/imednet-toolkit/commit/f7465da64dcf6e0a1cebf4659e1947353603b2d8))
* 🖊️ Scribe: fix todo docstrings in core package ([4af4f68](https://github.com/fderuiter/imednet-toolkit/commit/4af4f68384e403ab1723152be7f26e396a6dea5e))
* 🖊️ Scribe: replace placeholder docstrings with comprehensive documentation ([f89cae5](https://github.com/fderuiter/imednet-toolkit/commit/f89cae5458da75bf23382afa06024bf37305bd4f))
* enforce strict docstring governance and fix sphinx warnings ([34ae046](https://github.com/fderuiter/imednet-toolkit/commit/34ae046911e03b978eb5c237508d5241bd4ab3d8))
* fix documentation build errors and native callouts ([#1432](https://github.com/fderuiter/imednet-toolkit/issues/1432)) ([77238da](https://github.com/fderuiter/imednet-toolkit/commit/77238da2a888f7419dc6d468c5e0cdc5988c2429))
* Update CLI documentation to use sphinx-argparse instead of sphinx-click ([9b400c9](https://github.com/fderuiter/imednet-toolkit/commit/9b400c98f2253c09d814d045393e4a89a7326094))

## [0.8.0](https://github.com/fderuiter/imednet-python-sdk/compare/imednet-v0.7.0...imednet-v0.8.0) (2026-05-27)


### Features

* add atomic staged parquet partition writes ([01276aa](https://github.com/fderuiter/imednet-python-sdk/commit/01276aa3afe8a5e980d86c0c3e4e175167b54f09))
* add pyarrow dataset partitioned parquet engine ([4c8a630](https://github.com/fderuiter/imednet-python-sdk/commit/4c8a630ffbcd9d71e369239346e7488b83d2234a))
* add standards profiles and readiness validation ([33356c7](https://github.com/fderuiter/imednet-python-sdk/commit/33356c713b57d4a3091bc093eb14c4f16681fee5))
* add triage schemas, store, and review workbench scaffolding ([45d937b](https://github.com/fderuiter/imednet-python-sdk/commit/45d937b35d340d7ff38d6514fa8eec282e64ef1b))
* **cli:** add `imednet export duckdb` subcommand with filter passthrough and dependency guard ([#1028](https://github.com/fderuiter/imednet-python-sdk/issues/1028)) ([1fee464](https://github.com/fderuiter/imednet-python-sdk/commit/1fee464a9379809056d585c202328919d107fb8d))
* **cli:** register `imednet dashboard` with optional-plugin fallback and launcher ([#1030](https://github.com/fderuiter/imednet-python-sdk/issues/1030)) ([2d868f3](https://github.com/fderuiter/imednet-python-sdk/commit/2d868f38111deb90bd3ada0b95abbcee16cc06fb))
* **core:** add arrow table serialization utility ([73d8cfe](https://github.com/fderuiter/imednet-python-sdk/commit/73d8cfe25d66202b17739a3928a44fbc39adc519))
* **core:** add duckdb and other export dependencies as optional extras ([e2fc72b](https://github.com/fderuiter/imednet-python-sdk/commit/e2fc72ba8d16d19d7c509074916c4b63c4f92eb6))
* **core:** add export helpers and CLI commands for mongodb neo4j and snowflake ([018f880](https://github.com/fderuiter/imednet-python-sdk/commit/018f88087f4cd79afca08f6dba1ca37dbfcfdaab))
* **core:** add Hive-partitioned Parquet export helpers for concurrent analytical extraction ([#1023](https://github.com/fderuiter/imednet-python-sdk/issues/1023)) ([09cf8e5](https://github.com/fderuiter/imednet-python-sdk/commit/09cf8e5416bc6042a56802be801e2ffe2535d0a1))
* **core:** add native DuckDB export APIs for full-study and per-form integrations ([#1024](https://github.com/fderuiter/imednet-python-sdk/issues/1024)) ([3c2c5da](https://github.com/fderuiter/imednet-python-sdk/commit/3c2c5daf4656a1ff7729130e38115dc4684e98bf))
* **core:** enrich mongodb export envelopes with canonical metadata ([2b740bd](https://github.com/fderuiter/imednet-python-sdk/commit/2b740bd1e17e4073606f76b9daf87440ec984ab6))
* **core:** export MultiStudyOrchestrator and orchestration types from top-level `imednet` API ([#1043](https://github.com/fderuiter/imednet-python-sdk/issues/1043)) ([e142a0e](https://github.com/fderuiter/imednet-python-sdk/commit/e142a0e4a112b469553ab832676e7f8c9cbd3732))
* **core:** export sink architecture for Neo4j, MongoDB, and Snowflake ([#1109](https://github.com/fderuiter/imednet-python-sdk/issues/1109)) ([919a004](https://github.com/fderuiter/imednet-python-sdk/commit/919a004e7503dfdc31b226f43f6a6771a4f74830))
* **core:** harden parquet commit failure cleanup ([776c4bf](https://github.com/fderuiter/imednet-python-sdk/commit/776c4bf2417bbce09212ab3420210376d957c948))
* **errors:** introduce orchestration-specific error types in `imednet.errors` ([#1035](https://github.com/fderuiter/imednet-python-sdk/issues/1035)) ([158e483](https://github.com/fderuiter/imednet-python-sdk/commit/158e483f600a4cb2ef11910bbd8aa0ce807c88b5))
* finalize pyarrow partitioned storage engine integration ([e2e661d](https://github.com/fderuiter/imednet-python-sdk/commit/e2e661d0875310bd39d3d0842ac03190c092cece))
* **models:** add study configuration schemas for reporting ([b2098f3](https://github.com/fderuiter/imednet-python-sdk/commit/b2098f380fd8511a25f7136a54dc4e53d5656bd2))
* **orchestration:** add `studyKey` log context alias to multi-study worker telemetry ([#1073](https://github.com/fderuiter/imednet-python-sdk/issues/1073)) ([9a652b9](https://github.com/fderuiter/imednet-python-sdk/commit/9a652b9fedfacabe54a0e5b0d68448c3cc6ea1b2))
* **orchestration:** add concurrent `execute_pipeline` with per-study isolation and normalized results ([#1038](https://github.com/fderuiter/imednet-python-sdk/issues/1038)) ([d1a6b3f](https://github.com/fderuiter/imednet-python-sdk/commit/d1a6b3f47569b8b962a5d698f9a9517e79457f07))
* **orchestration:** add StudyContextLogAdapter for per-study log enrichment ([#1036](https://github.com/fderuiter/imednet-python-sdk/issues/1036)) ([9d27585](https://github.com/fderuiter/imednet-python-sdk/commit/9d275859e0ea4e4ed74eaf85d463e25b978d1bc7))
* **orchestration:** add typed worker protocol and normalized result schema for orchestrator contracts ([#1031](https://github.com/fderuiter/imednet-python-sdk/issues/1031)) ([fd7cdde](https://github.com/fderuiter/imednet-python-sdk/commit/fd7cddeb0b3f1f60a7e7a71ec1dfe233f12538f1))
* **orchestration:** implement MultiStudyOrchestrator initialization and active-study filter resolution ([#1037](https://github.com/fderuiter/imednet-python-sdk/issues/1037)) ([b7d4660](https://github.com/fderuiter/imednet-python-sdk/commit/b7d46606c146933788e30d7f1dec58a05dcccb80))
* **orchestration:** propagate study_context() into ThreadPoolExecutor worker threads ([#1041](https://github.com/fderuiter/imednet-python-sdk/issues/1041)) ([d1df7be](https://github.com/fderuiter/imednet-python-sdk/commit/d1df7be70d17c40638770debdab7d9bf478c8887))
* **orchestration:** scaffold `imednet.orchestration` package namespace and public surface ([#1026](https://github.com/fderuiter/imednet-python-sdk/issues/1026)) ([73bdd32](https://github.com/fderuiter/imednet-python-sdk/commit/73bdd321f57d540443381445dbdd08824a15610b))
* **performance:** add sync worker, chunked workflows, and paginated guardrails ([fb76e71](https://github.com/fderuiter/imednet-python-sdk/commit/fb76e71fa2851dda11e8910b55533abe99291616))
* stream chunked workflow exports ([2feacec](https://github.com/fderuiter/imednet-python-sdk/commit/2feacec8f4921e6fefb9bb70051293686413897d))
* validate reporting profile names ([14422c3](https://github.com/fderuiter/imednet-python-sdk/commit/14422c343b3c8349fdea370d24329ee1d323bd92))


### Bug Fixes

* align snowflake import guidance ([20ecd6a](https://github.com/fderuiter/imednet-python-sdk/commit/20ecd6ae2ef6e1c6aed6c2a9509366b9644173ab))
* **analytics:** exercise DuckDB and Hive-Parquet paths in the default CI/dev environment ([#1034](https://github.com/fderuiter/imednet-python-sdk/issues/1034)) ([17c7fa8](https://github.com/fderuiter/imednet-python-sdk/commit/17c7fa8b7d279105a8336c00b2dc1f666b47a18a))
* **core:** address export CLI and sink review follow-ups ([4ac2d95](https://github.com/fderuiter/imednet-python-sdk/commit/4ac2d958326b2f9f6f4920e5c902ed1941bed7b1))
* **parquet:** enable union-by-name hive parquet query ([829cf66](https://github.com/fderuiter/imednet-python-sdk/commit/829cf66b0b29c17cda60535e706538f7a9fe7484))
* validate hive partition keys before writing parquet ([1e1d2a5](https://github.com/fderuiter/imednet-python-sdk/commit/1e1d2a54ff05021add48768acb43b1766b80d801))


### Documentation

* clarify chunked workflow streaming APIs ([fe70965](https://github.com/fderuiter/imednet-python-sdk/commit/fe709657f08a62942e20ba1d05b3b08935418f84))
* **orchestration:** Sphinx API reference, module docstrings, and multi-study pipeline example ([#1047](https://github.com/fderuiter/imednet-python-sdk/issues/1047)) ([b3a61de](https://github.com/fderuiter/imednet-python-sdk/commit/b3a61dee38a34b1cff4fbc70059fdf9c9f0c3c1b))

## [0.7.0](https://github.com/fderuiter/imednet-python-sdk/compare/imednet-v0.6.2...imednet-v0.7.0) (2026-05-21)


### Features

* **plugins:** add PluginProtocol, PluginLoadError, plugin authoring docs and tests ([19a977b](https://github.com/fderuiter/imednet-python-sdk/commit/19a977b825ba80e0d5f418ce4a143c687782083f))


### Bug Fixes

* fully suppress httpx/httpcore logs during request execution ([d8f264e](https://github.com/fderuiter/imednet-python-sdk/commit/d8f264ed00b55c7c77abb2b0e26c40b74bd7d130))
* harden credential redaction in auth, errors, transport, and CLI tests ([fddefc0](https://github.com/fderuiter/imednet-python-sdk/commit/fddefc0401ba17967abcc4f7b46c3f90555f9bfa))
* **http:** normalize encoded path segments safely ([e6e7489](https://github.com/fderuiter/imednet-python-sdk/commit/e6e748951ec60f1c4d349ed6bc7a00035d040767))
* improve redaction matching and simplify log suppression ([000c46f](https://github.com/fderuiter/imednet-python-sdk/commit/000c46fd2cef1ce54d8173e8769e937a6af93be5))
* **plugins:** remove redundant pass in PluginLoadError body ([d3f20e9](https://github.com/fderuiter/imednet-python-sdk/commit/d3f20e91649b73572abb699464f612996c71fb7c))
* refine paginator cursor state semantics ([8d205e7](https://github.com/fderuiter/imednet-python-sdk/commit/8d205e7ebd8f69c032249d475a6e38c39fbb2fce))
* tighten paginator cursor validation message formatting ([257a1b6](https://github.com/fderuiter/imednet-python-sdk/commit/257a1b6d741a6e0b60a689a7b20116039fc4e54a))


### Documentation

* ✍️ Scribe: fix broken optional dependency installation commands ([20ae098](https://github.com/fderuiter/imednet-python-sdk/commit/20ae09880daf9ca7430f34d5a3a9baa70071ef6d))

## [0.6.2](https://github.com/fderuiter/imednet-python-sdk/compare/imednet-v0.6.1...imednet-v0.6.2) (2026-05-19)


### Bug Fixes

* remove stateful instance caching from endpoints for thread-safety ([1b8acb4](https://github.com/fderuiter/imednet-python-sdk/commit/1b8acb4e0244090f3034dc3d8057f191d1d0cdb4))

## [0.6.1](https://github.com/fderuiter/imednet-python-sdk/compare/imednet-v0.6.0...imednet-v0.6.1) (2026-05-13)


### Bug Fixes

* resolve workspace package build metadata ([9f3be6c](https://github.com/fderuiter/imednet-python-sdk/commit/9f3be6c642944a09cdb151152ec8d6788dd34d3b))
* stabilize workspace dependency validation ([9b7799f](https://github.com/fderuiter/imednet-python-sdk/commit/9b7799f8c138b009cf92bed97f031bd5d29615da))
* use package-local core readme for builds ([d672c5f](https://github.com/fderuiter/imednet-python-sdk/commit/d672c5fe6afd85a04ffa0b8814115685778c5e92))
