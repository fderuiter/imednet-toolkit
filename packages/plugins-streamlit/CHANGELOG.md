# Changelog

## [0.3.1](https://github.com/fderuiter/imednet-toolkit/compare/imednet-streamlit-v0.3.0...imednet-streamlit-v0.3.1) (2026-09-18)


### Bug Fixes

* **ci:** reconcile accessibility workflow permissions, vpat path, and cross-environment mypy rules ([0e9b0f4](https://github.com/fderuiter/imednet-toolkit/commit/0e9b0f4c186c1714e69788141b9488fb3b489033))
* **streamlit:** modernize type annotations and update a11y audit report ([c108ba3](https://github.com/fderuiter/imednet-toolkit/commit/c108ba31b684be415ed9a5d3d9c9cedaf70feca9))

## [0.3.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-streamlit-v0.2.0...imednet-streamlit-v0.3.0) (2026-09-10)


### Features

* **a11y:** automate CI accessibility checks and fix contrast violations ([790ae0a](https://github.com/fderuiter/imednet-toolkit/commit/790ae0a5aecf04fb94886a68e0eea4cee5a05db7))
* enforce boundary isolation and align downstream utility logic ([dd39824](https://github.com/fderuiter/imednet-toolkit/commit/dd398240a87fa9f2e4a79e7f42febd170eed9d9d))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef8d1eb](https://github.com/fderuiter/imednet-toolkit/commit/ef8d1ebf948865d456ccd46b71b0439d5bd88b90))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef6fcea](https://github.com/fderuiter/imednet-toolkit/commit/ef6fceabd5acfa823321c54852a0dea64e61778d))
* implement Enterprise Managed Portal with SSO and multi-tenancy ([72bd4d3](https://github.com/fderuiter/imednet-toolkit/commit/72bd4d32d7f095920551ec561821fa03229a61d3))
* implement hybrid config-driven compliance file mapping ([#1420](https://github.com/fderuiter/imednet-toolkit/issues/1420)) ([6b0e01b](https://github.com/fderuiter/imednet-toolkit/commit/6b0e01be21b32d54e5ffb6c89c4e13fe50b9a9ac))
* implement platform shared db connection, unified sink template, and UI component gallery ([ff99calf](https://github.com/fderuiter/imednet-toolkit/commit/ff99caf7ffeda68aa8bd8f8bc494b1e3cb9eb7b5))
* **streamlit:** add browser-level end-to-end dashboard coverage ([5ec4015](https://github.com/fderuiter/imednet-toolkit/commit/5ec4015772af072fa7478c3da5b6c3e6acdc6934))
* **streamlit:** add browser-level end-to-end dashboard coverage ([53ebc33](https://github.com/fderuiter/imednet-toolkit/commit/53ebc3377de8e6ab46f8f5f296d425ba39b5457d))
* **streamlit:** add browser-level end-to-end dashboard coverage ([5ece725](https://github.com/fderuiter/imednet-toolkit/commit/5ece7259e5cc2e95ee44f2d2ec19167192b0cf42))
* **streamlit:** add multi-user session-isolation and cache-expiry tests ([ff71fd1](https://github.com/fderuiter/imednet-toolkit/commit/ff71fd17537ea5d4a37abd7702a96da865df4f19))
* **streamlit:** implement dynamic environment url for admin portal ([2793c8d](https://github.com/fderuiter/imednet-toolkit/commit/2793c8d500ef42a5aad9a6871cdee9ad4f1913cb))
* support dynamic environment-specific routing and schema migration ([#1427](https://github.com/fderuiter/imednet-toolkit/issues/1427)) ([72180a3](https://github.com/fderuiter/imednet-toolkit/commit/72180a3d58d188b44f96e9e231eaa1ee9c06643a))


### Bug Fixes

* bypass respx_mock guard in live test suite via path-based detection ([73d85de](https://github.com/fderuiter/imednet-toolkit/commit/73d85de20125d15e948151a15b4d2900bf1b00db))
* resolve ruff formatting, missing docstrings, and pin numpy to fix mypy failures in CI ([9166e51](https://github.com/fderuiter/imednet-toolkit/commit/9166e51751898090949849c2dae56a6e78b623f9))
* **streamlit:** avoid reassigning widget-owned setup wizard form keys ([89207d4](https://github.com/fderuiter/imednet-toolkit/commit/89207d4eaac9de82c3268c355c2be6aca977a6da))
* **streamlit:** fix mypy type hint errors in render_accessible_chart ([21e785f](https://github.com/fderuiter/imednet-toolkit/commit/21e785f440ad4c1eba86d0f12fd4e7ffc26881f5))
* **streamlit:** resolve formatting and unit test regressions in CI ([ec6241d](https://github.com/fderuiter/imednet-toolkit/commit/ec6241d79ee0a098e70def2ca96a51c4dd8acb5e))
* **streamlit:** use standard version dependencies to resolve mypy CI failure ([15bf22d](https://github.com/fderuiter/imednet-toolkit/commit/15bf22ddad9d4b58f7899d0aa975d29325d27bef))


### Documentation

* enforce strict docstring governance and fix sphinx warnings ([34ae046](https://github.com/fderuiter/imednet-toolkit/commit/34ae046911e03b978eb5c237508d5241bd4ab3d8))

## [0.2.0](https://github.com/fderuiter/imednet-python-sdk/compare/imednet-streamlit-v0.1.0...imednet-streamlit-v0.2.0) (2026-05-27)


### Features

* add triage schemas, store, and review workbench scaffolding ([45d937b](https://github.com/fderuiter/imednet-python-sdk/commit/45d937b35d340d7ff38d6514fa8eec282e64ef1b))
* **governance:** add config version control, publisher wizard, and data lineage modules ([c6bce78](https://github.com/fderuiter/imednet-python-sdk/commit/c6bce78f231959db37cf996fe7026b25bcdde99a))
* Implement credential management and SDK factory module ([#1039](https://github.com/fderuiter/imednet-python-sdk/issues/1039)) ([80bc94e](https://github.com/fderuiter/imednet-python-sdk/commit/80bc94e3011ae8b4b78ed4fde0a0d5b646e6d8d9))
* Implement Streamlit data completeness page and site performance follow-ups ([#1049](https://github.com/fderuiter/imednet-python-sdk/issues/1049)) ([8f1e657](https://github.com/fderuiter/imednet-python-sdk/commit/8f1e657c2526fd5442dadef9d78ee03e785403c9))
* **performance:** add sync worker, chunked workflows, and paginated guardrails ([fb76e71](https://github.com/fderuiter/imednet-python-sdk/commit/fb76e71fa2851dda11e8910b55533abe99291616))
* **plugin:** scaffold `imednet-streamlit` workspace package ([#1025](https://github.com/fderuiter/imednet-python-sdk/issues/1025)) ([5f2e008](https://github.com/fderuiter/imednet-python-sdk/commit/5f2e008bef6cc633ba7752804691561245893863))
* **streamlit:** add 5-step Setup Wizard for no-code study mapping and configuration export ([#1121](https://github.com/fderuiter/imednet-python-sdk/issues/1121)) ([a9ab4cd](https://github.com/fderuiter/imednet-python-sdk/commit/a9ab4cdf5997dd91d5dc5aeac65a65301d777df5))
* **streamlit:** add interactive triage drawer actions and AppTest coverage ([ed75338](https://github.com/fderuiter/imednet-python-sdk/commit/ed753380723695a8c6cfa28c9159f8b67efda1bb))
* **streamlit:** add pagination guards, truncation, and tests ([1a2d124](https://github.com/fderuiter/imednet-python-sdk/commit/1a2d124166d5f7cfa08b5dd3275880ce5359c5a7))
* **streamlit:** add reporting dashboard runtime page and tests ([8a33787](https://github.com/fderuiter/imednet-python-sdk/commit/8a3378709d969db0d22199d302139c245de0fd9c))
* **streamlit:** add reusable lineage component and recursive redaction ([4b38911](https://github.com/fderuiter/imednet-python-sdk/commit/4b389110777935b4265fbc367253a97c838a78e5))
* **streamlit:** add shared components library for metrics, charts, tables, and exports ([#1042](https://github.com/fderuiter/imednet-python-sdk/issues/1042)) ([96230a5](https://github.com/fderuiter/imednet-python-sdk/commit/96230a5cbaf5fdb1d06df9bd6ded31e2433c2e08))
* **streamlit:** complete imednet-streamlit dashboard plugin documentation ([#1085](https://github.com/fderuiter/imednet-python-sdk/issues/1085)) ([f027b59](https://github.com/fderuiter/imednet-python-sdk/commit/f027b598e67c8ca7381b6fa7002394f74b054df2))
* **streamlit:** implement Query Status Overview dashboard page ([#1045](https://github.com/fderuiter/imednet-python-sdk/issues/1045)) ([68b0591](https://github.com/fderuiter/imednet-python-sdk/commit/68b0591ef1b0bcdfadbb50b29f724b98f89ced22))
* **streamlit:** implement Site Performance & Metrics dashboard page ([#1048](https://github.com/fderuiter/imednet-python-sdk/issues/1048)) ([bbb5a2a](https://github.com/fderuiter/imednet-python-sdk/commit/bbb5a2af68602da48e94882c052e1101c48c55d8))
* **streamlit:** implement Subject Enrollment Overview dashboard page ([#1046](https://github.com/fderuiter/imednet-python-sdk/issues/1046)) ([f88a30d](https://github.com/fderuiter/imednet-python-sdk/commit/f88a30dc6762e869824605dfa04c528f75660864))
* **streamlit:** scaffold auth-gated multi-page dashboard entrypoint ([#1040](https://github.com/fderuiter/imednet-python-sdk/issues/1040)) ([b912cb6](https://github.com/fderuiter/imednet-python-sdk/commit/b912cb62ea8dde33861a5fda667579ce1e2c4904))


### Bug Fixes

* **governance:** replace old-style generics and improve exception messages in governance modules ([9a346fe](https://github.com/fderuiter/imednet-python-sdk/commit/9a346fecaaf5603da72165631d83af17713ed293))
* **streamlit:** add AE serious target and subject key auto-mapping defaults ([da6918c](https://github.com/fderuiter/imednet-python-sdk/commit/da6918cc8c102c916a965af731e46a260c24c049))
* **streamlit:** align setup wizard mappings with canonical AE/PD/DD fields ([7290b22](https://github.com/fderuiter/imednet-python-sdk/commit/7290b22bae2ce26b4e0d905175e5606a41ea79dc))
* **streamlit:** harden subject key default selection in setup wizard ([eb87a67](https://github.com/fderuiter/imednet-python-sdk/commit/eb87a67c4ec3bbd36bf9ee5661112b9175d11504))


### Documentation

* **streamlit:** add dashboard guide and strengthen component docs ([0cec6fb](https://github.com/fderuiter/imednet-python-sdk/commit/0cec6fbf35b65a1e8d1c8f1da21b161b5d1e905e))
* **streamlit:** tighten plugin guide and auth docstring coverage ([91a1cd6](https://github.com/fderuiter/imednet-python-sdk/commit/91a1cd631ce1b9cba2003ff8c9d20e403b18b2de))
