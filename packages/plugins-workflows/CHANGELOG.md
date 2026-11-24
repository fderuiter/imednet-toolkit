# Changelog

## [1.2.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-workflows-v1.1.0...imednet-workflows-v1.2.0) (2026-11-23)


### Features

* add symmetric CSV upsert and schema-aware CLI dry-run ([#2026](https://github.com/fderuiter/imednet-toolkit/issues/2026)) ([c8bb983](https://github.com/fderuiter/imednet-toolkit/commit/c8bb983c0cee63ecaf5e9f2202c712491a0dd351))
* add tabular record import and CLI write commands ([#1978](https://github.com/fderuiter/imednet-toolkit/issues/1978)) ([358fbb9](https://github.com/fderuiter/imednet-toolkit/commit/358fbb9191035135056cd9513bdeaa281cb4ffe0))
* **airflow:** align Airflow 3 provider UI metadata, template fields, and SQL exports ([#1999](https://github.com/fderuiter/imednet-toolkit/issues/1999)) ([fb5b4c0](https://github.com/fderuiter/imednet-toolkit/commit/fb5b4c0dcf68200855f79f00e0bf75ef07e3eb65))
* centralize batch coordination and job error diagnostics ([#1909](https://github.com/fderuiter/imednet-toolkit/issues/1909)) ([20232c6](https://github.com/fderuiter/imednet-toolkit/commit/20232c6729f185cf7941cf9585ba1bd9ec089dcb))
* centralize default PHI masking across enrichment and exports ([#1936](https://github.com/fderuiter/imednet-toolkit/issues/1936)) ([99a0bc0](https://github.com/fderuiter/imednet-toolkit/commit/99a0bc0aef9d389c48a53d1841ddb10f23516d33))
* **cli:** expose shared CLI helpers in SPI ([#1998](https://github.com/fderuiter/imednet-toolkit/issues/1998)) ([b52e79b](https://github.com/fderuiter/imednet-toolkit/commit/b52e79ba6ac80e8562b5b77c930eda9be372cb6f))
* **cli:** implement live ANSI terminal progress rendering with poller integration ([#1852](https://github.com/fderuiter/imednet-toolkit/issues/1852)) ([9ea62ef](https://github.com/fderuiter/imednet-toolkit/commit/9ea62ef13339f034e983d8af5b7ba1e1b2048408))
* embed CDISC SDTM transformation and standards readiness in core SPI ([#2002](https://github.com/fderuiter/imednet-toolkit/issues/2002)) ([1d7ca31](https://github.com/fderuiter/imednet-toolkit/commit/1d7ca3164a958194be9aae5e3e5746912de78112))
* embed lifecycle invariants in domain models and stores ([#1915](https://github.com/fderuiter/imednet-toolkit/issues/1915)) ([2154ff4](https://github.com/fderuiter/imednet-toolkit/commit/2154ff4479fcee63d70d55407694af3e1d7e3ab9))
* enforce mode-safe plugin protocols and static types ([#2000](https://github.com/fderuiter/imednet-toolkit/issues/2000)) ([5c46bfa](https://github.com/fderuiter/imednet-toolkit/commit/5c46bfaf7f321e7026f5d6fbabfcec5acee2e954))
* merge tabular records with Snowflake schema evolution ([a596cc1](https://github.com/fderuiter/imednet-toolkit/commit/a596cc154cc426f5c3c51139139b281e0951c1ca))
* unify DataDictionary validation with XML rule evaluator ([#1913](https://github.com/fderuiter/imednet-toolkit/issues/1913)) ([2595f8e](https://github.com/fderuiter/imednet-toolkit/commit/2595f8e43ccf70f03dbe94ae3200143cdbfefb19))
* unify streaming batches and ledger checkpoints via SPI ([#1912](https://github.com/fderuiter/imednet-toolkit/issues/1912)) ([820d2b6](https://github.com/fderuiter/imednet-toolkit/commit/820d2b621ec78ff2f7b10d6ea9a62174a578d94e))
* **workflows:** add interactive console wizards and parameter validation ([#1843](https://github.com/fderuiter/imednet-toolkit/issues/1843)) ([c78001a](https://github.com/fderuiter/imednet-toolkit/commit/c78001afa5bd8f120fbe8605fab2d01c7f60f915))
* **workflows:** add SQLite sync epoch state machine and SyncWorker lifecycle ([#1959](https://github.com/fderuiter/imednet-toolkit/issues/1959)) ([f1b42b8](https://github.com/fderuiter/imednet-toolkit/commit/f1b42b8f6e6b8dc374fe7e9d5057e803b65e2b6a))
* **workflows:** add vital signs (VS) SDTM domain specification ([#1796](https://github.com/fderuiter/imednet-toolkit/issues/1796)) ([8e5389c](https://github.com/fderuiter/imednet-toolkit/commit/8e5389c8758ba7378e2446abaf8bf2333d2a8192))
* **workflows:** bridge QueryManagementWorkflow to TriageStore with schema v2 ([#1977](https://github.com/fderuiter/imednet-toolkit/issues/1977)) ([62e5694](https://github.com/fderuiter/imednet-toolkit/commit/62e56947ab9a78b04d0c4381479a957543b9de88))
* **workflows:** promote SPI write helpers for stateful UAT runs ([#2048](https://github.com/fderuiter/imednet-toolkit/issues/2048)) ([bf7c020](https://github.com/fderuiter/imednet-toolkit/commit/bf7c020f3d3efec061d5adfbd89b1fec9b01c4e3))
* **workflows:** register missing workflow entrypoints and add contract parity test ([#1775](https://github.com/fderuiter/imednet-toolkit/issues/1775)) ([562ca4f](https://github.com/fderuiter/imednet-toolkit/commit/562ca4f5462e18635e0438526246e71fb9c8f5dd))
* **workflows:** support optional flags in subject-data CLI ([#1820](https://github.com/fderuiter/imednet-toolkit/issues/1820)) ([62cc9c3](https://github.com/fderuiter/imednet-toolkit/commit/62cc9c39995b71e85ca3c09ba5638b24d4b4a4f4))


### Bug Fixes

* **airflow:** repair Airflow 3 provider discovery, XCom checkpoints, and DuckDB dispatch ([#1970](https://github.com/fderuiter/imednet-toolkit/issues/1970)) ([e778b17](https://github.com/fderuiter/imednet-toolkit/commit/e778b17e91025a11e0aeb11d8eee8d558cb09700))
* **airflow:** use Airflow Variable checkpoints and verify Job resultUrl ([#1995](https://github.com/fderuiter/imednet-toolkit/issues/1995)) ([ce2e65c](https://github.com/fderuiter/imednet-toolkit/commit/ce2e65c14ce46ad999d80498d4f78d9d0fcbc7d2))
* align filters, exports, and DuckDB revision schemas ([478f0fb](https://github.com/fderuiter/imednet-toolkit/commit/478f0fb44d34ffba811cfe816bc2865848106582))
* **cli:** thread endpoint filter allowlists through CLI preflight ([bee7d00](https://github.com/fderuiter/imednet-toolkit/commit/bee7d00fcc205c4acfdcdd256a3d0eb2a391d859))
* **cli:** unify CLI contracts in core SPI ([#1960](https://github.com/fderuiter/imednet-toolkit/issues/1960)) ([7f0c9fa](https://github.com/fderuiter/imednet-toolkit/commit/7f0c9fa742facb4c72c5beeae2ef9adc90434d82))
* **cli:** unify CLI output and format contracts via SPI ([#1987](https://github.com/fderuiter/imednet-toolkit/issues/1987)) ([2caa9c6](https://github.com/fderuiter/imednet-toolkit/commit/2caa9c6c14a61b73739bba133fdb222e1b217b97))
* **cli:** unify terminal policy with opt-out interactive mode ([#1888](https://github.com/fderuiter/imednet-toolkit/issues/1888)) ([066801e](https://github.com/fderuiter/imednet-toolkit/commit/066801e349af5b96b543b7e261aad7b06c9d879e))
* **core:** align dynamic stubs and endpoint contracts ([#1967](https://github.com/fderuiter/imednet-toolkit/issues/1967)) ([429b69c](https://github.com/fderuiter/imednet-toolkit/commit/429b69c68b23896e18962b83cea88c9d649aa56a))
* **core:** restrict evaluator method calls to safe allowlist ([#1806](https://github.com/fderuiter/imednet-toolkit/issues/1806)) ([980cb31](https://github.com/fderuiter/imednet-toolkit/commit/980cb315882a9c5df17429beb09e8524f3e339fd))
* **docs:** fix examples and expand docs verification scripts ([#1945](https://github.com/fderuiter/imednet-toolkit/issues/1945)) ([78b3ad8](https://github.com/fderuiter/imednet-toolkit/commit/78b3ad859e67b930bbb396c12622914cca1d0892))
* enforce endpoint filter registries and list-only contracts ([#1893](https://github.com/fderuiter/imednet-toolkit/issues/1893)) ([ed927c0](https://github.com/fderuiter/imednet-toolkit/commit/ed927c0f67f9983cc1ba38fb0b0b5df95358ea87))
* **export:** reconcile incremental exports directly in destination files ([#1964](https://github.com/fderuiter/imednet-toolkit/issues/1964)) ([f8a7758](https://github.com/fderuiter/imednet-toolkit/commit/f8a775886c2c94910ac9853afc0f1266b318ac18))
* **models:** infer contract types and synchronize Postman schemas ([#1889](https://github.com/fderuiter/imednet-toolkit/issues/1889)) ([64efe0f](https://github.com/fderuiter/imednet-toolkit/commit/64efe0f3bd62a19a86e99b56fabf7b6ead7a33a1))
* normalize pagination bounds and filters in endpoint strategies ([#1918](https://github.com/fderuiter/imednet-toolkit/issues/1918)) ([add6048](https://github.com/fderuiter/imednet-toolkit/commit/add6048a46fb53011dc1b97c23328d961765d851))
* preserve uncommitted watermarks, recover stale leases, and chain sanitized batch causes ([#1955](https://github.com/fderuiter/imednet-toolkit/issues/1955)) ([cc69622](https://github.com/fderuiter/imednet-toolkit/commit/cc696223e5e0e3e4196f4c76f7ee0ce8f967c523))
* scope HTTP auth, isolate circuit breakers, and make job pollers cooperative ([#1947](https://github.com/fderuiter/imednet-toolkit/issues/1947)) ([e15e872](https://github.com/fderuiter/imednet-toolkit/commit/e15e8724825bae66d8a03bc0f2522e3bf1e9ffde))
* **security:** mask PHI at mapper ingress via SPI ([#1911](https://github.com/fderuiter/imednet-toolkit/issues/1911)) ([432d8a5](https://github.com/fderuiter/imednet-toolkit/commit/432d8a5e3e7fe784f6143d7eae55206d770a7bd2))
* **security:** standardize PHI redaction on SensitivityRegistry ([#1923](https://github.com/fderuiter/imednet-toolkit/issues/1923)) ([cb52acc](https://github.com/fderuiter/imednet-toolkit/commit/cb52accc22789ee6f805bc8f8d6cc1f2c9379dad))
* **security:** validate and encode API path segments strictly ([#1968](https://github.com/fderuiter/imednet-toolkit/issues/1968)) ([78e605b](https://github.com/fderuiter/imednet-toolkit/commit/78e605bea40845d6a3130aafe432091b58357b49))
* unify governance context and audit checks ([#2044](https://github.com/fderuiter/imednet-toolkit/issues/2044)) ([bcec554](https://github.com/fderuiter/imednet-toolkit/commit/bcec5541232e4e7be669b38fc3e3ce462ae42712))
* **workflows:** align UAT workflows and edit-check verdicts ([#2049](https://github.com/fderuiter/imednet-toolkit/issues/2049)) ([fad9bb0](https://github.com/fderuiter/imednet-toolkit/commit/fad9bb05f17a33fede10dd97380fcf3d383ae4fb))
* **workflows:** deduplicate model aliases and normalize consumed keys in SDTM transformation ([#1821](https://github.com/fderuiter/imednet-toolkit/issues/1821)) ([ca73a42](https://github.com/fderuiter/imednet-toolkit/commit/ca73a42b7a55152394d8ed6e7e821a35675f522b))
* **workflows:** enforce server-side role validation in commit_config ([#1802](https://github.com/fderuiter/imednet-toolkit/issues/1802)) ([71257d0](https://github.com/fderuiter/imednet-toolkit/commit/71257d057b6f4fbb5ef4a01a942931a65b071dfa))
* **workflows:** isolate UAT run state and poll verification jobs concurrently ([#1942](https://github.com/fderuiter/imednet-toolkit/issues/1942)) ([804aae4](https://github.com/fderuiter/imednet-toolkit/commit/804aae4293bd396f02c6a8970f5693a7038f5b8b))
* **workflows:** set default state ledger path to user home directory ([#1814](https://github.com/fderuiter/imednet-toolkit/issues/1814)) ([71c090f](https://github.com/fderuiter/imednet-toolkit/commit/71c090f2f972582a11271ad8b2e588b612eb3a0b))
* **workflows:** stream sorted SDTM batches with ISO PHI redaction ([#1979](https://github.com/fderuiter/imednet-toolkit/issues/1979)) ([c52d849](https://github.com/fderuiter/imednet-toolkit/commit/c52d849e3ea1b0e9193457e8ed69f95ad2c7a05f))
* **workflows:** validate study context with validate_partition_key in ConfigVersionStore ([#1789](https://github.com/fderuiter/imednet-toolkit/issues/1789)) ([3b9009d](https://github.com/fderuiter/imednet-toolkit/commit/3b9009d46ef05ab3418c1dea656342563256e12e))


### Performance Improvements

* adopt lazy package barrels and deferred imports ([#2013](https://github.com/fderuiter/imednet-toolkit/issues/2013)) ([733cfe7](https://github.com/fderuiter/imednet-toolkit/commit/733cfe736f52423a02fe5577936e4012dad96884))
* **workflows:** bulk insert revisions via columnar DuckDB staging ([#1948](https://github.com/fderuiter/imednet-toolkit/issues/1948)) ([ef1e7ee](https://github.com/fderuiter/imednet-toolkit/commit/ef1e7eeb1333d18d56340a46cc236b27bf0d902a))
* **workflows:** deduplicate form model synthesis in hierarchy mapping ([#1785](https://github.com/fderuiter/imednet-toolkit/issues/1785)) ([b848c85](https://github.com/fderuiter/imednet-toolkit/commit/b848c85d2d037f41422cbefe3c17c8137cfbae08))
* **workflows:** narrow SQLite lock scope to write transactions ([#1875](https://github.com/fderuiter/imednet-toolkit/issues/1875)) ([3a9833f](https://github.com/fderuiter/imednet-toolkit/commit/3a9833fa454a3e498d3864c9f312e40e145bc6d2))
* **workflows:** precompute model aliases and dispatch date parsing ([#1876](https://github.com/fderuiter/imednet-toolkit/issues/1876)) ([6f7ffba](https://github.com/fderuiter/imednet-toolkit/commit/6f7ffba3053b0012c29345190ff1c7db55ae0a5e))


### Documentation

* align guides, demos, and READMEs to current fixtures ([#2016](https://github.com/fderuiter/imednet-toolkit/issues/2016)) ([86c2aec](https://github.com/fderuiter/imednet-toolkit/commit/86c2aec01ba3cd2f1a640e23e39f53b3c019cc8b))

## [1.1.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-workflows-v1.0.0...imednet-workflows-v1.1.0) (2026-11-16)


### Features

* integrate typed job polling and CLI JSON contracts ([#1750](https://github.com/fderuiter/imednet-toolkit/issues/1750)) ([72e8957](https://github.com/fderuiter/imednet-toolkit/commit/72e8957959159467c22875757526ac0d232e5a38))
* native CDISC ODM XML serialization and SDTM domain mapper ([#1745](https://github.com/fderuiter/imednet-toolkit/issues/1745)) ([426723d](https://github.com/fderuiter/imednet-toolkit/commit/426723d8bb1b5c95843fb81960ad3ccdafc95147))
* **standards:** implement CDASH and SDTM deterministic normalization engine ([#1744](https://github.com/fderuiter/imednet-toolkit/issues/1744)) ([c1ba240](https://github.com/fderuiter/imednet-toolkit/commit/c1ba240565c08f7fb04563fc95b896e729990018))
* **uat:** implement in-engine state machine and monotonic job updates ([#1715](https://github.com/fderuiter/imednet-toolkit/issues/1715)) ([78855ab](https://github.com/fderuiter/imednet-toolkit/commit/78855abd570e2da4c40b9bb96be63e148a215a40))


### Bug Fixes

* **airflow:** restore export delegation and core 1.x installation ([#1709](https://github.com/fderuiter/imednet-toolkit/issues/1709)) ([1d17233](https://github.com/fderuiter/imednet-toolkit/commit/1d172334ae24553d69b8941d9fc0fa13a02d19d9))
* isolate test paths and default DuckDB fixtures to in-memory mode ([#1739](https://github.com/fderuiter/imednet-toolkit/issues/1739)) ([e320d06](https://github.com/fderuiter/imednet-toolkit/commit/e320d06e9fc0684f8d4ca7e9c8c9e9611be55b69))
* resolve explicit authentication without placeholder credentials ([#1749](https://github.com/fderuiter/imednet-toolkit/issues/1749)) ([154e593](https://github.com/fderuiter/imednet-toolkit/commit/154e593c141b96e1233f072e7d9f4a9ee5ceee0d))
* **retries:** enforce pagination ownership and configured sink attempts ([#1710](https://github.com/fderuiter/imednet-toolkit/issues/1710)) ([ac185b2](https://github.com/fderuiter/imednet-toolkit/commit/ac185b275ee9055e46f9e62f0650a1c720ae99a5))


### Performance Improvements

* **workflows:** precompute normalized terminology lookups before validation loops ([#1726](https://github.com/fderuiter/imednet-toolkit/issues/1726)) ([933406b](https://github.com/fderuiter/imednet-toolkit/commit/933406b23bd49ade70f4661ce913aa76c63014ce))
* **workflows:** use set lookups for record criteria extraction ([#1720](https://github.com/fderuiter/imednet-toolkit/issues/1720)) ([21cbcf5](https://github.com/fderuiter/imednet-toolkit/commit/21cbcf5ed073fff8c0929459e236d42121aaebf1))

## [1.0.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-workflows-v0.8.0...imednet-workflows-v1.0.0) (2026-11-15)


### ⚠ BREAKING CHANGES

* retire scheduled smoke tests and harden validation ([#1702](https://github.com/fderuiter/imednet-toolkit/issues/1702))

### Bug Fixes

* retire scheduled smoke tests and harden validation ([#1702](https://github.com/fderuiter/imednet-toolkit/issues/1702)) ([a46cce8](https://github.com/fderuiter/imednet-toolkit/commit/a46cce8759edd1fe4035cdf3075a441d8fe1b8a6))
* **workflows:** convert duckdb_centralizer to lazy cached property ([#1705](https://github.com/fderuiter/imednet-toolkit/issues/1705)) ([9342249](https://github.com/fderuiter/imednet-toolkit/commit/93422498212f48006816c3f58db41f92ce14fbf4))

## [0.8.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-workflows-v0.7.0...imednet-workflows-v0.8.0) (2026-11-15)


### Features

* query lifecycle state machine with formal discrepancy status enum ([#1673](https://github.com/fderuiter/imednet-toolkit/issues/1673)) ([9176fe3](https://github.com/fderuiter/imednet-toolkit/commit/9176fe363ba7d3425f9d6996e69b6896e0faf7ef))
* validate deterministic status transitions in TriageStore ([#1655](https://github.com/fderuiter/imednet-toolkit/issues/1655)) ([3afdb60](https://github.com/fderuiter/imednet-toolkit/commit/3afdb60a3f13d34d793cd6b3f1661c99dca5245b))
* **workflows:** concurrent batch job polling for bulk record submission ([#1651](https://github.com/fderuiter/imednet-toolkit/issues/1651)) ([ac5411a](https://github.com/fderuiter/imednet-toolkit/commit/ac5411add039fc77a9714cf5eeec84d5a098663c))
* **workflows:** implement stream execution state machine with atomic transaction guards ([#1659](https://github.com/fderuiter/imednet-toolkit/issues/1659)) ([f379d13](https://github.com/fderuiter/imednet-toolkit/commit/f379d13eb492682ab27db12dcf9c9e59c9a8cba1))


### Bug Fixes

* **deps:** relax filelock constraint and upgrade vulnerable runtime packages ([#1672](https://github.com/fderuiter/imednet-toolkit/issues/1672)) ([efc39d3](https://github.com/fderuiter/imednet-toolkit/commit/efc39d3cd17d06af71268657c9b5862003fc014f))
* **export:** validate SQL identifiers, directory paths, and study keys in DuckDB queries ([#1657](https://github.com/fderuiter/imednet-toolkit/issues/1657)) ([39eaa07](https://github.com/fderuiter/imednet-toolkit/commit/39eaa076b4a09e41f862f4c1b670c47c9e6888fa))
* **security:** replace eval with restricted AST expression evaluation engine ([#1671](https://github.com/fderuiter/imednet-toolkit/issues/1671)) ([dccd8be](https://github.com/fderuiter/imednet-toolkit/commit/dccd8be5f64cdde01b7dc201903f1a3c611e548f))


### Performance Improvements

* stream active record identifiers with lightweight projection ([#1663](https://github.com/fderuiter/imednet-toolkit/issues/1663)) ([e655612](https://github.com/fderuiter/imednet-toolkit/commit/e655612040ae94f4454325a5ba1a5cfb28827bab))
* **workflows:** eliminate redundant serialization and cache compiled logic ([#1656](https://github.com/fderuiter/imednet-toolkit/issues/1656)) ([6f4d7e8](https://github.com/fderuiter/imednet-toolkit/commit/6f4d7e878613349e50304c680d0201a6d1995118))
* **workflows:** switch SyncWorker to sync_records and sql count ([#1653](https://github.com/fderuiter/imednet-toolkit/issues/1653)) ([387602a](https://github.com/fderuiter/imednet-toolkit/commit/387602a61abae9f2cfe816a404f8f4aa660712e0))


### Documentation

* align domain vocabulary across interfaces and models ([#1666](https://github.com/fderuiter/imednet-toolkit/issues/1666)) ([712409b](https://github.com/fderuiter/imednet-toolkit/commit/712409b7de232cdd710d4073bab4dd8276a77e9b))

## [0.7.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-workflows-v0.6.0...imednet-workflows-v0.7.0) (2026-09-10)


### Features

* 🛠️ SyntheticRecordGenerator — Variable-type-aware test payload generation ([6ccf96b](https://github.com/fderuiter/imednet-toolkit/commit/6ccf96b6c09598ed9e5d4cc828918b925c240c43))
* add StudySchemaInspector workflow and snapshot model ([3dfde3b](https://github.com/fderuiter/imednet-toolkit/commit/3dfde3b306e4c006348f8d7bcc4481d855a4c729))
* add UAT specification models and tests ([5c1add5](https://github.com/fderuiter/imednet-toolkit/commit/5c1add56fde62f5775570b4cde8f95cf25e1e76b))
* address PR feedback for UATWorkflow orchestrator ([432d8b8](https://github.com/fderuiter/imednet-toolkit/commit/432d8b8a06c1328fcbc2e69979037504ebf62057))
* BulkRecordSubmissionWorkflow implementation and CI fix ([aae4179](https://github.com/fderuiter/imednet-toolkit/commit/aae41797e37246bf5d18927272d21c8b466417e5))
* BulkRecordSubmissionWorkflow implementation with linting fixes ([0fc5333](https://github.com/fderuiter/imednet-toolkit/commit/0fc533345d4a38cf6244af48a5aa223493481d86))
* BulkRecordSubmissionWorkflow implementation with mypy fix ([33b793b](https://github.com/fderuiter/imednet-toolkit/commit/33b793b52dfea469195f9b9394345ccdbcc51d7e))
* **core:** implement granular entrypoint registry architecture for workflows ([da4b60d](https://github.com/fderuiter/imednet-toolkit/commit/da4b60d8568254b316284d9c89c89cf860a64f41))
* enforce boundary isolation and align downstream utility logic ([dd39824](https://github.com/fderuiter/imednet-toolkit/commit/dd398240a87fa9f2e4a79e7f42febd170eed9d9d))
* enforce documentation presence for all workspace packages ([#1431](https://github.com/fderuiter/imednet-toolkit/issues/1431)) ([9e45d79](https://github.com/fderuiter/imednet-toolkit/commit/9e45d796714e24724ff6a35c85e92cc6602cea32))
* Enhanced JobPoller with progress callbacks and concurrent polling ([5706387](https://github.com/fderuiter/imednet-toolkit/commit/5706387bcef313fdbfe5b5f5ba08f139448ca30c))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef8d1eb](https://github.com/fderuiter/imednet-toolkit/commit/ef8d1ebf948865d456ccd46b71b0439d5bd88b90))
* Enhanced JobPoller with progress callbacks and concurrent polling ([ef6fcea](https://github.com/fderuiter/imednet-toolkit/commit/ef6fceabd5acfa823321c54852a0dea64e61778d))
* enrich JobStatus object with auto-parsed results and metadata ([84d6083](https://github.com/fderuiter/imednet-toolkit/commit/84d6083b6f645b43bf5760da4711ea2981af93b0))
* implement Enterprise Managed Portal with SSO and multi-tenancy ([72bd4d3](https://github.com/fderuiter/imednet-toolkit/commit/72bd4d32d7f095920551ec561821fa03229a61d3))
* implement platform shared db connection, unified sink template, and UI component gallery ([ff99calf](https://github.com/fderuiter/imednet-toolkit/commit/ff99caf7ffeda68aa8bd8f8bc494b1e3cb9eb7b5))
* implement pluggable state provider architecture with airflow xcom backend ([8ac81f9](https://github.com/fderuiter/imednet-toolkit/commit/8ac81f98756204a2ed0b8e74840fdac5d8d63b28))
* implement protocol-validated direct SDK registration ([63ea455](https://github.com/fderuiter/imednet-toolkit/commit/63ea455b59405c77e24ee15d31a2affdf4fdd236))
* schedule daily smoke tests and standardize pipeline secrets ([#1470](https://github.com/fderuiter/imednet-toolkit/issues/1470)) ([e47dc1c](https://github.com/fderuiter/imednet-toolkit/commit/e47dc1c67631a4301a3973c77948a14f7f03b89f))
* UATWorkflow — End-to-end UAT orchestrator composing inspect, generate, execute, and monitor phases ([8ce8fd5](https://github.com/fderuiter/imednet-toolkit/commit/8ce8fd5a86e5c6ecdae974fd4e883d423642b8ed))
* UATWorkflow — End-to-end UAT orchestrator composing inspect, generate, finalize, and monitor phases ([34ec232](https://github.com/fderuiter/imednet-toolkit/commit/34ec2326d58ca29d1db4e2cb452315dc5b3ddfa3))


### Bug Fixes

* bypass respx_mock guard in live test suite via path-based detection ([73d85de](https://github.com/fderuiter/imednet-toolkit/commit/73d85de20125d15e948151a15b4d2900bf1b00db))
* **ci:** resolve Semgrep false positives in duckdb_centralizer.py ([52e2355](https://github.com/fderuiter/imednet-toolkit/commit/52e235524bfe62d954e661aa6f4c62a249343b0a))
* **ci:** resolve Smoke Test failure and refine StudySchemaInspector ([7dbf2db](https://github.com/fderuiter/imednet-toolkit/commit/7dbf2dbcfecec99e3bb3cc9cf76ef92e3fa66f83))
* **ci:** restore test coverage and resolve CI failures ([a1daedc](https://github.com/fderuiter/imednet-toolkit/commit/a1daedc4405bf935f175cd0585785293f52a5943))
* correct job poller entry points to reference package root ([9150c49](https://github.com/fderuiter/imednet-toolkit/commit/9150c49a538989e3acb91081e43e0737087e683c))
* **plugins-workflows:** resolve mypy type error in record_mapper.py ([7acb07e](https://github.com/fderuiter/imednet-toolkit/commit/7acb07ebea2810cee5a3ba14676dba94eafc0562))
* **plugins-workflows:** resolve ruff SLF001 private member access ([2bf6f9e](https://github.com/fderuiter/imednet-toolkit/commit/2bf6f9ead25d3269bfe4ed0865217b67beb812bf))
* reconcile cross-platform dependencies, linter rules, and verification gates ([91ccda7](https://github.com/fderuiter/imednet-toolkit/commit/91ccda7f6b87ce9ed555c19d9099a5de044b7ab9))
* refine StudySchemaInspector typing and cache docs ([b9b5ab6](https://github.com/fderuiter/imednet-toolkit/commit/b9b5ab640d126089ffbd363c6e4478e459eaa32a))
* remove subjectKey from RegisterSubjectRequest to fix subject registration ([a5d8b34](https://github.com/fderuiter/imednet-toolkit/commit/a5d8b34cf58ba784687071dea07c7012e6ed5003))
* resolve iterator regressions in CLI and workflows ([be6c52c](https://github.com/fderuiter/imednet-toolkit/commit/be6c52c49fed2663f20c45df03e79994bb30bd60))
* resolve NoneType AttributeErrors in live integration tests ([db27fbd](https://github.com/fderuiter/imednet-toolkit/commit/db27fbddfd05406ac61d8737694e2a17fcd37b2b))
* resolve ruff formatting, missing docstrings, and pin numpy to fix mypy failures in CI ([9166e51](https://github.com/fderuiter/imednet-toolkit/commit/9166e51751898090949849c2dae56a6e78b623f9))
* use timezone-aware generated_at default ([8c14632](https://github.com/fderuiter/imednet-toolkit/commit/8c14632e70fe0d31ba77045c1f25d1987ab54b18))


### Documentation

* 🖊️ Scribe: Comprehensive update of docstring placeholders ([4e13f4b](https://github.com/fderuiter/imednet-toolkit/commit/4e13f4b28b6343e1eb402c255081f24e2212d491))
* 🖊️ Scribe: Comprehensive update of docstring placeholders and fix formatting ([7bb3808](https://github.com/fderuiter/imednet-toolkit/commit/7bb3808e8dabdfc88a20e7151506d99ab21f4391))
* add comprehensive self-contained READMEs for Airflow provider, Sinks plugin, and Workflows plugin ([452066a](https://github.com/fderuiter/imednet-toolkit/commit/452066a973b1d74c2a71fbbdd4a854da27b0b96b))
* enforce strict docstring governance and fix sphinx warnings ([34ae046](https://github.com/fderuiter/imednet-toolkit/commit/34ae046911e03b978eb5c237508d5241bd4ab3d8))
* fix documentation build errors and native callouts ([#1432](https://github.com/fderuiter/imednet-toolkit/issues/1432)) ([77238da](https://github.com/fderuiter/imednet-toolkit/commit/77238da2a888f7419dc6d468c5e0cdc5988c2429))

## [0.6.0](https://github.com/fderuiter/imednet-python-sdk/compare/imednet-workflows-v0.5.3...imednet-workflows-v0.6.0) (2026-05-27)


### Features

* add records cache and schema profiler scaffolding ([ed64413](https://github.com/fderuiter/imednet-python-sdk/commit/ed64413c1601da46ce357b70c3376b71e040295c))
* add standards profiles and readiness validation ([33356c7](https://github.com/fderuiter/imednet-python-sdk/commit/33356c713b57d4a3091bc093eb14c4f16681fee5))
* add triage schemas, store, and review workbench scaffolding ([45d937b](https://github.com/fderuiter/imednet-python-sdk/commit/45d937b35d340d7ff38d6514fa8eec282e64ef1b))
* finalize records cache and schema profiler ([d02c747](https://github.com/fderuiter/imednet-python-sdk/commit/d02c747dc83dc1dbf1a3f990ffad3a06ac170f5c))
* **governance:** add config version control, publisher wizard, and data lineage modules ([c6bce78](https://github.com/fderuiter/imednet-python-sdk/commit/c6bce78f231959db37cf996fe7026b25bcdde99a))
* **performance:** add sync worker, chunked workflows, and paginated guardrails ([fb76e71](https://github.com/fderuiter/imednet-python-sdk/commit/fb76e71fa2851dda11e8910b55533abe99291616))
* stateful incremental tracker 987 ([#1084](https://github.com/fderuiter/imednet-python-sdk/issues/1084)) ([e67eb80](https://github.com/fderuiter/imednet-python-sdk/commit/e67eb802a32541acf95f90fd1e605559ff1dd96f))
* stream chunked workflow exports ([2feacec](https://github.com/fderuiter/imednet-python-sdk/commit/2feacec8f4921e6fefb9bb70051293686413897d))
* **streamlit:** implement Query Status Overview dashboard page ([#1045](https://github.com/fderuiter/imednet-python-sdk/issues/1045)) ([68b0591](https://github.com/fderuiter/imednet-python-sdk/commit/68b0591ef1b0bcdfadbb50b29f724b98f89ced22))
* **workflows:** add canonical extraction engine ([d78216e](https://github.com/fderuiter/imednet-python-sdk/commit/d78216e08dfcb777bbc8dfb7a91484f6e226d8c6))
* **workflows:** add DuckDBIngestionWorkflow for incremental eCRF bronze/silver centralization ([#1027](https://github.com/fderuiter/imednet-python-sdk/issues/1027)) ([58ffb7b](https://github.com/fderuiter/imednet-python-sdk/commit/58ffb7befb552d1855528abd3584e077e704de61))
* **workflows:** harden config version control ledger integrity ([c6ca772](https://github.com/fderuiter/imednet-python-sdk/commit/c6ca7721f80ae9de0fc18022122c545c9910b4c2))
* **workflows:** harden local triage store with WAL-safe writes, schema migration, and redacted errors ([#1144](https://github.com/fderuiter/imednet-python-sdk/issues/1144)) ([c3d5978](https://github.com/fderuiter/imednet-python-sdk/commit/c3d5978b626962f0bbb54bdb7d08221feb845ba3))


### Bug Fixes

* **governance:** replace old-style generics and improve exception messages in governance modules ([9a346fe](https://github.com/fderuiter/imednet-python-sdk/commit/9a346fecaaf5603da72165631d83af17713ed293))
* serialize SQLite WAL init per DB path to prevent concurrent lock errors ([f6e6991](https://github.com/fderuiter/imednet-python-sdk/commit/f6e69915ea999413e329b8eff9156fc4ff1cdd9c))


### Documentation

* clarify chunked workflow streaming APIs ([fe70965](https://github.com/fderuiter/imednet-python-sdk/commit/fe709657f08a62942e20ba1d05b3b08935418f84))

## [0.5.3](https://github.com/fderuiter/imednet-python-sdk/compare/imednet-workflows-v0.5.2...imednet-workflows-v0.5.3) (2026-05-21)


### Bug Fixes

* tighten sync-async workflow typing boundaries ([5dead1c6](https://github.com/fderuiter/imednet-python-sdk/commit/5ded1c61a65b6376b264d9b9d4dd5658aa78c831))

## [0.5.2](https://github.com/fderuiter/imednet-python-sdk/compare/imednet-workflows-v0.5.1...imednet-workflows-v0.5.2) (2026-05-13)


### Bug Fixes

* tighten workflow init errors and airflow s3 fallback messaging ([b706b3c](https://github.com/fderuiter/imednet-python-sdk/commit/b706b3c04691566b5fd6c632a93eaea30307e419))
