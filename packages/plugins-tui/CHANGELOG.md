# Changelog

## [1.1.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-tui-v1.0.0...imednet-tui-v1.1.0) (2026-11-21)


### Features

* add CLI server filters and bounded fetches ([#1887](https://github.com/fderuiter/imednet-toolkit/issues/1887)) ([4a9f24b](https://github.com/fderuiter/imednet-toolkit/commit/4a9f24b2cdd37059e95ac229da878537506ec9d1))
* **cli:** add modular terminal interface plugin with dynamic discovery ([#1842](https://github.com/fderuiter/imednet-toolkit/issues/1842)) ([e4d5070](https://github.com/fderuiter/imednet-toolkit/commit/e4d50708f828befc900e0d4be35141d2dbd878e7))
* **cli:** expose shared CLI helpers in SPI ([#1998](https://github.com/fderuiter/imednet-toolkit/issues/1998)) ([b52e79b](https://github.com/fderuiter/imednet-toolkit/commit/b52e79ba6ac80e8562b5b77c930eda9be372cb6f))
* enforce mode-safe plugin protocols and static types ([#2000](https://github.com/fderuiter/imednet-toolkit/issues/2000)) ([5c46bfa](https://github.com/fderuiter/imednet-toolkit/commit/5c46bfaf7f321e7026f5d6fbabfcec5acee2e954))
* **tui:** add inline job table and per-study batch tracking ([#1919](https://github.com/fderuiter/imednet-toolkit/issues/1919)) ([67603dc](https://github.com/fderuiter/imednet-toolkit/commit/67603dc5cf6284238d2e7ca8137b5be916451fac))
* **tui:** bounded concurrent job polling with asyncio gather and semaphore ([#1874](https://github.com/fderuiter/imednet-toolkit/issues/1874)) ([0ced5cb](https://github.com/fderuiter/imednet-toolkit/commit/0ced5cbe6765616286b891b70f5028d30cf5825c))


### Bug Fixes

* **cli,tui:** unify TTY policy, contrast badges, and TUI layout ([#1994](https://github.com/fderuiter/imednet-toolkit/issues/1994)) ([a1afe02](https://github.com/fderuiter/imednet-toolkit/commit/a1afe024e6441143c33dae3ba1bee7a6c2d28a09))
* **tui:** add TTY stream guard and diagnostic for headless runs ([#1864](https://github.com/fderuiter/imednet-toolkit/issues/1864)) ([66ead99](https://github.com/fderuiter/imednet-toolkit/commit/66ead9915b81b1a3e4e6e19d8218b80fd404edb5))
* **tui:** fix selection events, sanitized IDs, controls, and SDK loading ([f413e15](https://github.com/fderuiter/imednet-toolkit/commit/f413e15a9cf21154340852ea9c06ed4f38f1015b))
* **tui:** harden job polling, resultUrl counts, and log redaction ([#1974](https://github.com/fderuiter/imednet-toolkit/issues/1974)) ([ab7f8d0](https://github.com/fderuiter/imednet-toolkit/commit/ab7f8d0d20b96c3dea8dc37f9646fe3999ba3d92))
* **tui:** harden literal text rendering, study switches, and Escape navigation ([#1953](https://github.com/fderuiter/imednet-toolkit/issues/1953)) ([1fded07](https://github.com/fderuiter/imednet-toolkit/commit/1fded071997dbc752caae4dea36729ee96d1971f))
* **tui:** stabilize bottom pane with responsive compact grid ([#1938](https://github.com/fderuiter/imednet-toolkit/issues/1938)) ([0f1acf2](https://github.com/fderuiter/imednet-toolkit/commit/0f1acf2c1c76ff96416e657011ed3810864a4bd3))

## 1.0.0 (2026-11-17)

### Features

- Initial release of `imednet-tui` modular terminal interface plugin.
- Dynamic CLI discovery via `imednet.cli_plugins` entry points.
- Interactive study, site, subject, and job exploration views with keyboard navigation.
- High contrast accessibility mode with `IMEDNET_HIGH_CONTRAST` support.
- Zero local credential storage in compliance with ADR 0005.
