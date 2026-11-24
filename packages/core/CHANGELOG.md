# Changelog

## [1.2.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-v1.1.0...imednet-v1.2.0) (2026-11-23)


### Features

* add AST stub generator and MockTransport contract suite ([ca21dd9](https://github.com/fderuiter/imednet-toolkit/commit/ca21dd973944c6956ead654fa90105aebf93b067))
* add CLI server filters and bounded fetches ([#1887](https://github.com/fderuiter/imednet-toolkit/issues/1887)) ([4a9f24b](https://github.com/fderuiter/imednet-toolkit/commit/4a9f24b2cdd37059e95ac229da878537506ec9d1))
* add native form DataFrame export ([#1809](https://github.com/fderuiter/imednet-toolkit/issues/1809)) ([b052887](https://github.com/fderuiter/imednet-toolkit/commit/b05288769b8a3f6ce6d9e1d1268bb8f5c7cae822))
* add native to_dataframe methods to visits, intervals, and sites endpoints ([#1823](https://github.com/fderuiter/imednet-toolkit/issues/1823)) ([c979d86](https://github.com/fderuiter/imednet-toolkit/commit/c979d86acfc61dc4b64c254ce00a151e481e8c0f))
* add symmetric CSV upsert and schema-aware CLI dry-run ([#2026](https://github.com/fderuiter/imednet-toolkit/issues/2026)) ([c8bb983](https://github.com/fderuiter/imednet-toolkit/commit/c8bb983c0cee63ecaf5e9f2202c712491a0dd351))
* add tabular record import and CLI write commands ([#1978](https://github.com/fderuiter/imednet-toolkit/issues/1978)) ([358fbb9](https://github.com/fderuiter/imednet-toolkit/commit/358fbb9191035135056cd9513bdeaa281cb4ffe0))
* **airflow:** add provider metadata, deferrable trigger, and SDK parity ([#2004](https://github.com/fderuiter/imednet-toolkit/issues/2004)) ([2763e22](https://github.com/fderuiter/imednet-toolkit/commit/2763e22cfe08226230345de354731620d22909cf))
* centralize batch coordination and job error diagnostics ([#1909](https://github.com/fderuiter/imednet-toolkit/issues/1909)) ([20232c6](https://github.com/fderuiter/imednet-toolkit/commit/20232c6729f185cf7941cf9585ba1bd9ec089dcb))
* centralize default PHI masking across enrichment and exports ([#1936](https://github.com/fderuiter/imednet-toolkit/issues/1936)) ([99a0bc0](https://github.com/fderuiter/imednet-toolkit/commit/99a0bc0aef9d389c48a53d1841ddb10f23516d33))
* **cli:** add horizontal grid scrolling and full detail inspection ([#1884](https://github.com/fderuiter/imednet-toolkit/issues/1884)) ([b51776c](https://github.com/fderuiter/imednet-toolkit/commit/b51776c6b1af465b5096fd5aef0b05b744e71268))
* **cli:** add modular terminal interface plugin with dynamic discovery ([#1842](https://github.com/fderuiter/imednet-toolkit/issues/1842)) ([e4d5070](https://github.com/fderuiter/imednet-toolkit/commit/e4d50708f828befc900e0d4be35141d2dbd878e7))
* **cli:** add record data filter helper and study_key filter guards ([#1934](https://github.com/fderuiter/imednet-toolkit/issues/1934)) ([dc25fcd](https://github.com/fderuiter/imednet-toolkit/commit/dc25fcd4a57a0c5f2a83f6f7976dd81e13a0d13d))
* **cli:** add users and codings subparsers with filter flags ([#1827](https://github.com/fderuiter/imednet-toolkit/issues/1827)) ([884dd01](https://github.com/fderuiter/imednet-toolkit/commit/884dd018a6161a1eadb7297feb5da5f9fb0fa11a))
* **cli:** add visits subparser and command handlers ([#1815](https://github.com/fderuiter/imednet-toolkit/issues/1815)) ([19f2ed9](https://github.com/fderuiter/imednet-toolkit/commit/19f2ed9182c556b8fea4be3e91db8d16111aed0f))
* **cli:** expose shared CLI helpers in SPI ([#1998](https://github.com/fderuiter/imednet-toolkit/issues/1998)) ([b52e79b](https://github.com/fderuiter/imednet-toolkit/commit/b52e79ba6ac80e8562b5b77c930eda9be372cb6f))
* **cli:** implement live ANSI terminal progress rendering with poller integration ([#1852](https://github.com/fderuiter/imednet-toolkit/issues/1852)) ([9ea62ef](https://github.com/fderuiter/imednet-toolkit/commit/9ea62ef13339f034e983d8af5b7ba1e1b2048408))
* **cli:** register forms subparser with list command ([#1798](https://github.com/fderuiter/imednet-toolkit/issues/1798)) ([723cb85](https://github.com/fderuiter/imednet-toolkit/commit/723cb85eac7669416ec4209bda1b4e54287f677f))
* **cli:** unify model-aware CLI filtering and scalar coercion ([#1931](https://github.com/fderuiter/imednet-toolkit/issues/1931)) ([fd60c2b](https://github.com/fderuiter/imednet-toolkit/commit/fd60c2bff18bc9f16cce00176663f4063d76c181))
* **core:** add DataFrame export to variable endpoints ([#1803](https://github.com/fderuiter/imednet-toolkit/issues/1803)) ([362accb](https://github.com/fderuiter/imednet-toolkit/commit/362accb751868753d88fa6aa7eaec4b7d10bf97e))
* **core:** add query dataframe export matching castor ([#1794](https://github.com/fderuiter/imednet-toolkit/issues/1794)) ([c6514b8](https://github.com/fderuiter/imednet-toolkit/commit/c6514b891658a09251758a89000903afd247065e))
* **core:** bundle postman spec in wheel and resolve via package resources ([#1781](https://github.com/fderuiter/imednet-toolkit/issues/1781)) ([2e920ea](https://github.com/fderuiter/imednet-toolkit/commit/2e920eaf4fadf5010de98fdb3f16eebae8bbfce2))
* **core:** preserve SDTM attributes in CDISC ODM export ([#1784](https://github.com/fderuiter/imednet-toolkit/issues/1784)) ([f80a7b5](https://github.com/fderuiter/imednet-toolkit/commit/f80a7b5acf340964a1de8fe1bb0b64208b007fe1))
* **core:** validate filter syntax and connector rules client-side ([#1779](https://github.com/fderuiter/imednet-toolkit/issues/1779)) ([880fc4e](https://github.com/fderuiter/imednet-toolkit/commit/880fc4e96cd1d27ce25808344edda7dd63dcb12b))
* embed CDISC SDTM transformation and standards readiness in core SPI ([#2002](https://github.com/fderuiter/imednet-toolkit/issues/2002)) ([1d7ca31](https://github.com/fderuiter/imednet-toolkit/commit/1d7ca3164a958194be9aae5e3e5746912de78112))
* embed lifecycle invariants in domain models and stores ([#1915](https://github.com/fderuiter/imednet-toolkit/issues/1915)) ([2154ff4](https://github.com/fderuiter/imednet-toolkit/commit/2154ff4479fcee63d70d55407694af3e1d7e3ab9))
* **endpoints:** introduce list-only EDC endpoints with strict validation ([#1856](https://github.com/fderuiter/imednet-toolkit/issues/1856)) ([154dcd1](https://github.com/fderuiter/imednet-toolkit/commit/154dcd18bc142a7445c0f15b654ed5db8c971504))
* enforce mode-safe plugin protocols and static types ([#2000](https://github.com/fderuiter/imednet-toolkit/issues/2000)) ([5c46bfa](https://github.com/fderuiter/imednet-toolkit/commit/5c46bfaf7f321e7026f5d6fbabfcec5acee2e954))
* interactive keyboard data grid with side panel inspection ([#1841](https://github.com/fderuiter/imednet-toolkit/issues/1841)) ([ec7aa0a](https://github.com/fderuiter/imednet-toolkit/commit/ec7aa0abe52b10f5d02ca79bcb5f92dc968082a2))
* **models:** generate overloaded constructor stubs for aliased models ([#2011](https://github.com/fderuiter/imednet-toolkit/issues/2011)) ([5d71610](https://github.com/fderuiter/imednet-toolkit/commit/5d7161080e0d56267b702ed88bbb6225bbabafa8))
* override get in studies operation definition ([#1854](https://github.com/fderuiter/imednet-toolkit/issues/1854)) ([dbfd03a](https://github.com/fderuiter/imednet-toolkit/commit/dbfd03aff8897ba7881cf5786bc506f21ae75170))
* **simulation:** add contract routes, filter operators, and pagination envelopes ([#1905](https://github.com/fderuiter/imednet-toolkit/issues/1905)) ([66e8f7a](https://github.com/fderuiter/imednet-toolkit/commit/66e8f7a7d3bd605d0f1b1b6524e0b65c94b28755))
* **spi:** introduce formal SPI sink_base and dynamic entry point discovery ([#1773](https://github.com/fderuiter/imednet-toolkit/issues/1773)) ([f5817de](https://github.com/fderuiter/imednet-toolkit/commit/f5817de89f43a6f0916cfb8604e1f55c7c2afca7))
* **tui:** add inline job detail pane and default PHI masking ([#2034](https://github.com/fderuiter/imednet-toolkit/issues/2034)) ([059cc10](https://github.com/fderuiter/imednet-toolkit/commit/059cc1022a32717558d79cff9b8b7bf1ae2ba694))
* unify DataDictionary validation with XML rule evaluator ([#1913](https://github.com/fderuiter/imednet-toolkit/issues/1913)) ([2595f8e](https://github.com/fderuiter/imednet-toolkit/commit/2595f8e43ccf70f03dbe94ae3200143cdbfefb19))
* unify streaming batches and ledger checkpoints via SPI ([#1912](https://github.com/fderuiter/imednet-toolkit/issues/1912)) ([820d2b6](https://github.com/fderuiter/imednet-toolkit/commit/820d2b621ec78ff2f7b10d6ea9a62174a578d94e))
* **users:** add native DataFrame export for user endpoints ([#1860](https://github.com/fderuiter/imednet-toolkit/issues/1860)) ([53e375e](https://github.com/fderuiter/imednet-toolkit/commit/53e375eb0ca7be693deca45aae1830cb75d46b6d))
* **workflows:** bridge QueryManagementWorkflow to TriageStore with schema v2 ([#1977](https://github.com/fderuiter/imednet-toolkit/issues/1977)) ([62e5694](https://github.com/fderuiter/imednet-toolkit/commit/62e56947ab9a78b04d0c4381479a957543b9de88))
* **workflows:** promote SPI write helpers for stateful UAT runs ([#2048](https://github.com/fderuiter/imednet-toolkit/issues/2048)) ([bf7c020](https://github.com/fderuiter/imednet-toolkit/commit/bf7c020f3d3efec061d5adfbd89b1fec9b01c4e3))


### Bug Fixes

* **airflow:** use Airflow Variable checkpoints and verify Job resultUrl ([#1995](https://github.com/fderuiter/imednet-toolkit/issues/1995)) ([ce2e65c](https://github.com/fderuiter/imednet-toolkit/commit/ce2e65c14ce46ad999d80498d4f78d9d0fcbc7d2))
* align filters, exports, and DuckDB revision schemas ([478f0fb](https://github.com/fderuiter/imednet-toolkit/commit/478f0fb44d34ffba811cfe816bc2865848106582))
* align sink showcases and enforce symbol-level showcase validation ([#1920](https://github.com/fderuiter/imednet-toolkit/issues/1920)) ([0536005](https://github.com/fderuiter/imednet-toolkit/commit/053600566fd550f3379edd35a3ce4d04125e2994))
* **auth:** enforce cryptographic signature and claims verification in OIDCAuth ([#1801](https://github.com/fderuiter/imednet-toolkit/issues/1801)) ([87f2881](https://github.com/fderuiter/imednet-toolkit/commit/87f28811e15e634b612f849403d91d5c02bfa255))
* **cli,tui:** unify TTY policy, contrast badges, and TUI layout ([#1994](https://github.com/fderuiter/imednet-toolkit/issues/1994)) ([a1afe02](https://github.com/fderuiter/imednet-toolkit/commit/a1afe024e6441143c33dae3ba1bee7a6c2d28a09))
* **cli:** refine grid truncation recovery and panel controls ([#1963](https://github.com/fderuiter/imednet-toolkit/issues/1963)) ([0546b25](https://github.com/fderuiter/imednet-toolkit/commit/0546b2514a406d41bda64519a0d531ca0a9256f8))
* **cli:** support Unicode display widths, UTF-8 search, and control-char sanitization ([#1939](https://github.com/fderuiter/imednet-toolkit/issues/1939)) ([0c80439](https://github.com/fderuiter/imednet-toolkit/commit/0c8043900d124991513987fe9833a4d0b98c9813))
* **cli:** thread endpoint filter allowlists through CLI preflight ([bee7d00](https://github.com/fderuiter/imednet-toolkit/commit/bee7d00fcc205c4acfdcdd256a3d0eb2a391d859))
* **cli:** unify CLI contracts in core SPI ([#1960](https://github.com/fderuiter/imednet-toolkit/issues/1960)) ([7f0c9fa](https://github.com/fderuiter/imednet-toolkit/commit/7f0c9fa742facb4c72c5beeae2ef9adc90434d82))
* **cli:** unify CLI output and format contracts via SPI ([#1987](https://github.com/fderuiter/imednet-toolkit/issues/1987)) ([2caa9c6](https://github.com/fderuiter/imednet-toolkit/commit/2caa9c6c14a61b73739bba133fdb222e1b217b97))
* **cli:** unify terminal policy with opt-out interactive mode ([#1888](https://github.com/fderuiter/imednet-toolkit/issues/1888)) ([066801e](https://github.com/fderuiter/imednet-toolkit/commit/066801e349af5b96b543b7e261aad7b06c9d879e))
* **core,airflow:** guard optional pandas import and stabilize Airflow 3.4 CI suites ([#2005](https://github.com/fderuiter/imednet-toolkit/issues/2005)) ([b0e99ff](https://github.com/fderuiter/imednet-toolkit/commit/b0e99ff852d5e19c3d88b5d312a0a4cb39da344f))
* **core:** add fail-fast pandas preflight to endpoint DataFrame exports ([#1866](https://github.com/fderuiter/imednet-toolkit/issues/1866)) ([6fb8f0e](https://github.com/fderuiter/imednet-toolkit/commit/6fb8f0e34fc37fa1516a59eb1afe6c61a6d81ec8))
* **core:** align dynamic stubs and endpoint contracts ([#1967](https://github.com/fderuiter/imednet-toolkit/issues/1967)) ([429b69c](https://github.com/fderuiter/imednet-toolkit/commit/429b69c68b23896e18962b83cea88c9d649aa56a))
* **core:** preserve DefaultRetryPolicy for SDK retry arguments ([#1917](https://github.com/fderuiter/imednet-toolkit/issues/1917)) ([ef8733e](https://github.com/fderuiter/imednet-toolkit/commit/ef8733e5429f5ec3e6d4bc232a564f6b02d88a7c))
* **core:** restrict evaluator method calls to safe allowlist ([#1806](https://github.com/fderuiter/imednet-toolkit/issues/1806)) ([980cb31](https://github.com/fderuiter/imednet-toolkit/commit/980cb315882a9c5df17429beb09e8524f3e339fd))
* **core:** scope circuit breakers with probe leases ([#1965](https://github.com/fderuiter/imednet-toolkit/issues/1965)) ([025dd10](https://github.com/fderuiter/imednet-toolkit/commit/025dd10fb4c602339cee317cb0a5444e073088e1))
* **core:** synchronize workflow plugin contract and documentation ([#1799](https://github.com/fderuiter/imednet-toolkit/issues/1799)) ([29e1366](https://github.com/fderuiter/imednet-toolkit/commit/29e13668c2f5dc4e216ade51f6ffa4fd413f83e9))
* **datadict:** discriminate file targets in CDISC ODM and REDCap loaders ([#1800](https://github.com/fderuiter/imednet-toolkit/issues/1800)) ([db05ec7](https://github.com/fderuiter/imednet-toolkit/commit/db05ec771050779759eb980f68280f9fc0f5a278))
* **datadict:** guard LogicTreeEvaluator regex rules against ReDoS ([#2055](https://github.com/fderuiter/imednet-toolkit/issues/2055)) ([69ca40c](https://github.com/fderuiter/imednet-toolkit/commit/69ca40c62fc0c417d4bafa71b491b55ff14db6e4))
* **datadict:** normalize loaders and preserve ODM v1.0 fidelity ([#1906](https://github.com/fderuiter/imednet-toolkit/issues/1906)) ([265473e](https://github.com/fderuiter/imednet-toolkit/commit/265473e52ec644002359386f99a99c25ba71d846))
* **datadict:** share matching ODM definitions and qualify divergences ([#1921](https://github.com/fderuiter/imednet-toolkit/issues/1921)) ([62c147b](https://github.com/fderuiter/imednet-toolkit/commit/62c147beb0a1503b336b378f52a3eb4236bb751c))
* **docs:** fix examples and expand docs verification scripts ([#1945](https://github.com/fderuiter/imednet-toolkit/issues/1945)) ([78b3ad8](https://github.com/fderuiter/imednet-toolkit/commit/78b3ad859e67b930bbb396c12622914cca1d0892))
* **docs:** preserve runtime metadata and scope canonical autodoc ([#1933](https://github.com/fderuiter/imednet-toolkit/issues/1933)) ([e68fa9b](https://github.com/fderuiter/imednet-toolkit/commit/e68fa9b896e99049c16533a1f4549393e3cb4ff0))
* enforce endpoint filter registries and list-only contracts ([#1893](https://github.com/fderuiter/imednet-toolkit/issues/1893)) ([ed927c0](https://github.com/fderuiter/imednet-toolkit/commit/ed927c0f67f9983cc1ba38fb0b0b5df95358ea87))
* **errors:** propagate diagnostic context and token-aware redaction ([#2012](https://github.com/fderuiter/imednet-toolkit/issues/2012)) ([0fe406c](https://github.com/fderuiter/imednet-toolkit/commit/0fe406c30e1efcba717e4bc05a7a36701db45ea4))
* **export:** reconcile incremental exports directly in destination files ([#1964](https://github.com/fderuiter/imednet-toolkit/issues/1964)) ([f8a7758](https://github.com/fderuiter/imednet-toolkit/commit/f8a775886c2c94910ac9853afc0f1266b318ac18))
* **integrations:** cap quality-gate watermarks and harden incremental syncs ([#2003](https://github.com/fderuiter/imednet-toolkit/issues/2003)) ([7c8d933](https://github.com/fderuiter/imednet-toolkit/commit/7c8d9339b6c9c04d50b975cfa9f3a16d7226ac2b))
* **models:** infer contract types and synchronize Postman schemas ([#1889](https://github.com/fderuiter/imednet-toolkit/issues/1889)) ([64efe0f](https://github.com/fderuiter/imednet-toolkit/commit/64efe0f3bd62a19a86e99b56fabf7b6ead7a33a1))
* normalize pagination bounds and filters in endpoint strategies ([#1918](https://github.com/fderuiter/imednet-toolkit/issues/1918)) ([add6048](https://github.com/fderuiter/imednet-toolkit/commit/add6048a46fb53011dc1b97c23328d961765d851))
* parse nested metadata.error envelopes and dispatch upstream error codes ([#1885](https://github.com/fderuiter/imednet-toolkit/issues/1885)) ([a9aca28](https://github.com/fderuiter/imednet-toolkit/commit/a9aca280555b534b61c58ac2424de649cd0d38bd))
* pass resolved Config and scope HTTP client lifecycles ([#1929](https://github.com/fderuiter/imednet-toolkit/issues/1929)) ([ff6775b](https://github.com/fderuiter/imednet-toolkit/commit/ff6775b444dac188d128533afa0e5cdc87c48f14))
* preserve uncommitted watermarks, recover stale leases, and chain sanitized batch causes ([#1955](https://github.com/fderuiter/imednet-toolkit/issues/1955)) ([cc69622](https://github.com/fderuiter/imednet-toolkit/commit/cc696223e5e0e3e4196f4c76f7ee0ce8f967c523))
* resolve Airflow destination connections and merge Snowflake batches ([c2a1b79](https://github.com/fderuiter/imednet-toolkit/commit/c2a1b7955b86c5c58e9339a80ffd90f3ddf94c07))
* scope HTTP auth, isolate circuit breakers, and make job pollers cooperative ([#1947](https://github.com/fderuiter/imednet-toolkit/issues/1947)) ([e15e872](https://github.com/fderuiter/imednet-toolkit/commit/e15e8724825bae66d8a03bc0f2522e3bf1e9ffde))
* **security:** enforce asymmetric defaults and key separation in OIDCAuth ([#1950](https://github.com/fderuiter/imednet-toolkit/issues/1950)) ([19a193c](https://github.com/fderuiter/imednet-toolkit/commit/19a193ca6a26457e92839fa32102c03c0f621f67))
* **security:** mask PHI at mapper ingress via SPI ([#1911](https://github.com/fderuiter/imednet-toolkit/issues/1911)) ([432d8a5](https://github.com/fderuiter/imednet-toolkit/commit/432d8a5e3e7fe784f6143d7eae55206d770a7bd2))
* **security:** remove format methods from RestrictedAstEvaluator allowlist ([#1956](https://github.com/fderuiter/imednet-toolkit/issues/1956)) ([926306f](https://github.com/fderuiter/imednet-toolkit/commit/926306fa2c2c507dc33304e9fc4e1ae2af8f19e5))
* **security:** standardize PHI redaction on SensitivityRegistry ([#1923](https://github.com/fderuiter/imednet-toolkit/issues/1923)) ([cb52acc](https://github.com/fderuiter/imednet-toolkit/commit/cb52accc22789ee6f805bc8f8d6cc1f2c9379dad))
* **security:** validate and encode API path segments strictly ([#1968](https://github.com/fderuiter/imednet-toolkit/issues/1968)) ([78e605b](https://github.com/fderuiter/imednet-toolkit/commit/78e605bea40845d6a3130aafe432091b58357b49))
* **security:** validate SDTM sequence keys before building Neo4j Cypher ([#2028](https://github.com/fderuiter/imednet-toolkit/issues/2028)) ([758605b](https://github.com/fderuiter/imednet-toolkit/commit/758605b7ffb2c985600c2360717e402ec2a241c0))
* **simulation:** enforce domain lifecycles in offline simulation ([#1957](https://github.com/fderuiter/imednet-toolkit/issues/1957)) ([f158392](https://github.com/fderuiter/imednet-toolkit/commit/f15839292f0564c3dd50ab5f5be5d0e9d8eba972))
* **sinks:** centralize SPI mapping and add direct Neo4j subject-record edges ([#1949](https://github.com/fderuiter/imednet-toolkit/issues/1949)) ([2342129](https://github.com/fderuiter/imednet-toolkit/commit/234212996a823c21c47e35adb9c5bf239de32648))
* **standards:** register concomitant medications aliases in canonical domains ([#1818](https://github.com/fderuiter/imednet-toolkit/issues/1818)) ([ae55528](https://github.com/fderuiter/imednet-toolkit/commit/ae55528b1c4918383c68d514cd5fa792522b14b6))
* unify governance context and audit checks ([#2044](https://github.com/fderuiter/imednet-toolkit/issues/2044)) ([bcec554](https://github.com/fderuiter/imednet-toolkit/commit/bcec5541232e4e7be669b38fc3e3ce462ae42712))
* unify opt-in quality gates and optional variable readiness scoring ([#2046](https://github.com/fderuiter/imednet-toolkit/issues/2046)) ([f2c54b8](https://github.com/fderuiter/imednet-toolkit/commit/f2c54b89084f84d6e482ed7eaa2795e529336131))
* **validation:** canonicalize clinical variable type validators in SchemaCache ([#1916](https://github.com/fderuiter/imednet-toolkit/issues/1916)) ([bfef674](https://github.com/fderuiter/imednet-toolkit/commit/bfef67489c900724822bfa8e5a33f0c1cef287ea))
* **wizard:** add all-packages flag to setup wizard synchronization ([#1816](https://github.com/fderuiter/imednet-toolkit/issues/1816)) ([dd44e6b](https://github.com/fderuiter/imednet-toolkit/commit/dd44e6bd08490d2956b88bc486bf63637e9ff461))
* **workflows:** stream sorted SDTM batches with ISO PHI redaction ([#1979](https://github.com/fderuiter/imednet-toolkit/issues/1979)) ([c52d849](https://github.com/fderuiter/imednet-toolkit/commit/c52d849e3ea1b0e9193457e8ed69f95ad2c7a05f))
* **workflows:** validate study context with validate_partition_key in ConfigVersionStore ([#1789](https://github.com/fderuiter/imednet-toolkit/issues/1789)) ([3b9009d](https://github.com/fderuiter/imednet-toolkit/commit/3b9009d46ef05ab3418c1dea656342563256e12e))


### Performance Improvements

* adopt lazy package barrels and deferred imports ([#2013](https://github.com/fderuiter/imednet-toolkit/issues/2013)) ([733cfe7](https://github.com/fderuiter/imednet-toolkit/commit/733cfe736f52423a02fe5577936e4012dad96884))
* **core:** bypass recursive traversal for empty enrichment rule sets ([#1790](https://github.com/fderuiter/imednet-toolkit/issues/1790)) ([56f3217](https://github.com/fderuiter/imednet-toolkit/commit/56f32173edf68ee959d9b7cb2cbd730a74c5c26a))
* **core:** cache field metadata and share default enrichment pipeline ([#1877](https://github.com/fderuiter/imednet-toolkit/issues/1877)) ([5b7aef0](https://github.com/fderuiter/imednet-toolkit/commit/5b7aef02c59900df1a0c22061c8b755136fc3836))
* **core:** cache model field metadata for drift detection ([#1792](https://github.com/fderuiter/imednet-toolkit/issues/1792)) ([bfef317](https://github.com/fderuiter/imednet-toolkit/commit/bfef3171016b71005f9348221eaa84deb401404d))
* **core:** memoize field key normalization in security registry ([#1786](https://github.com/fderuiter/imednet-toolkit/issues/1786)) ([296ddce](https://github.com/fderuiter/imednet-toolkit/commit/296ddced9708056732a296746ab4ac0a8739590f))
* **export:** stream study records once across per-form exports ([#1996](https://github.com/fderuiter/imednet-toolkit/issues/1996)) ([3fec197](https://github.com/fderuiter/imednet-toolkit/commit/3fec1974fecc1f27f55c220aefc744910cbe2d8a))
* **sinks:** streamline warehouse Arrow conversion to single-pass checks ([#1952](https://github.com/fderuiter/imednet-toolkit/issues/1952)) ([40197f6](https://github.com/fderuiter/imednet-toolkit/commit/40197f6daf6e7723d2430671bff3e85ae9259cc3))


### Documentation

* add autodoc descriptor hooks and curated core reference ([#1946](https://github.com/fderuiter/imednet-toolkit/issues/1946)) ([a3ae3c5](https://github.com/fderuiter/imednet-toolkit/commit/a3ae3c5dbf17fa450ad6a8dd6ea4b427dd3cc09e))
* curate example wrappers, MyST changelogs, and maintainer sidebars ([#2019](https://github.com/fderuiter/imednet-toolkit/issues/2019)) ([5d78db0](https://github.com/fderuiter/imednet-toolkit/commit/5d78db024682254a6975d8f947d7b7706bf46a9d))

## [1.1.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-v1.0.0...imednet-v1.1.0) (2026-11-16)


### Features

* add Demographics and Vital Signs profiles and reporting models ([#1746](https://github.com/fderuiter/imednet-toolkit/issues/1746)) ([228becb](https://github.com/fderuiter/imednet-toolkit/commit/228becb8c490bcd58020e98dcff173e39caa5760))
* add label and hierarchy flags to cli export ([#1731](https://github.com/fderuiter/imednet-toolkit/issues/1731)) ([320a21f](https://github.com/fderuiter/imednet-toolkit/commit/320a21f15c33b54ff5b45ac37483710d736021aa))
* **core:** implement built-in simulation transport and in-memory fixture state ([#1735](https://github.com/fderuiter/imednet-toolkit/issues/1735)) ([193ea00](https://github.com/fderuiter/imednet-toolkit/commit/193ea008c01545b3c562e66f3bef6c5f3580d7b5))
* integrate typed job polling and CLI JSON contracts ([#1750](https://github.com/fderuiter/imednet-toolkit/issues/1750)) ([72e8957](https://github.com/fderuiter/imednet-toolkit/commit/72e8957959159467c22875757526ac0d232e5a38))
* native CDISC ODM XML serialization and SDTM domain mapper ([#1745](https://github.com/fderuiter/imednet-toolkit/issues/1745)) ([426723d](https://github.com/fderuiter/imednet-toolkit/commit/426723d8bb1b5c95843fb81960ad3ccdafc95147))
* **standards:** implement CDASH and SDTM deterministic normalization engine ([#1744](https://github.com/fderuiter/imednet-toolkit/issues/1744)) ([c1ba240](https://github.com/fderuiter/imednet-toolkit/commit/c1ba240565c08f7fb04563fc95b896e729990018))
* **subjects:** add to_dataframe export to subjects endpoints ([#1728](https://github.com/fderuiter/imednet-toolkit/issues/1728)) ([3804979](https://github.com/fderuiter/imednet-toolkit/commit/38049795dfeabf185f9c92bee5d67a9343b44faf))


### Bug Fixes

* **airflow:** restore export delegation and core 1.x installation ([#1709](https://github.com/fderuiter/imednet-toolkit/issues/1709)) ([1d17233](https://github.com/fderuiter/imednet-toolkit/commit/1d172334ae24553d69b8941d9fc0fa13a02d19d9))
* **cli:** defer dotenv loading with environment guards ([#1727](https://github.com/fderuiter/imednet-toolkit/issues/1727)) ([1f71b1c](https://github.com/fderuiter/imednet-toolkit/commit/1f71b1c76c6d3ced8451143b53c423e2ce15c89b))
* **cli:** silently load dotenv lazily in SDK context ([#1729](https://github.com/fderuiter/imednet-toolkit/issues/1729)) ([5ce027a](https://github.com/fderuiter/imednet-toolkit/commit/5ce027a73ae209795ee283f6233e40c0afb1fd16))
* **core:** preserve CDISC ODM SDTM attributes in data dictionary ([#1743](https://github.com/fderuiter/imednet-toolkit/issues/1743)) ([33327cc](https://github.com/fderuiter/imednet-toolkit/commit/33327cceb681eb76ce553687295a0171f40b9995))
* **datadict:** discriminate file targets in schema validation ([#1723](https://github.com/fderuiter/imednet-toolkit/issues/1723)) ([51a7dfe](https://github.com/fderuiter/imednet-toolkit/commit/51a7dfedc95b63bd10d081870eb72ff2e0f422af))
* isolate test paths and default DuckDB fixtures to in-memory mode ([#1739](https://github.com/fderuiter/imednet-toolkit/issues/1739)) ([e320d06](https://github.com/fderuiter/imednet-toolkit/commit/e320d06e9fc0684f8d4ca7e9c8c9e9611be55b69))
* resolve explicit authentication without placeholder credentials ([#1749](https://github.com/fderuiter/imednet-toolkit/issues/1749)) ([154e593](https://github.com/fderuiter/imednet-toolkit/commit/154e593c141b96e1233f072e7d9f4a9ee5ceee0d))
* **retries:** enforce pagination ownership and configured sink attempts ([#1710](https://github.com/fderuiter/imednet-toolkit/issues/1710)) ([ac185b2](https://github.com/fderuiter/imednet-toolkit/commit/ac185b275ee9055e46f9e62f0650a1c720ae99a5))
* **security:** normalize field keys in SensitivityRegistry to protect PHI ([#1722](https://github.com/fderuiter/imednet-toolkit/issues/1722)) ([741c477](https://github.com/fderuiter/imednet-toolkit/commit/741c4775c240964b6dc01bde85a9b27723f994ea))


### Performance Improvements

* **core:** memoize snake case conversions in record enrichment ([#1718](https://github.com/fderuiter/imednet-toolkit/issues/1718)) ([1364d3d](https://github.com/fderuiter/imednet-toolkit/commit/1364d3d67c0785adb8f6effb718046a53dca13ed))

## [1.0.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-v0.11.0...imednet-v1.0.0) (2026-11-15)


### ⚠ BREAKING CHANGES

* retire scheduled smoke tests and harden validation ([#1702](https://github.com/fderuiter/imednet-toolkit/issues/1702))

### Bug Fixes

* retire scheduled smoke tests and harden validation ([#1702](https://github.com/fderuiter/imednet-toolkit/issues/1702)) ([a46cce8](https://github.com/fderuiter/imednet-toolkit/commit/a46cce8759edd1fe4035cdf3075a441d8fe1b8a6))

## [0.11.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-v0.10.1...imednet-v0.11.0) (2026-11-15)


### Features

* **datadict:** add unified core loaders for CDISC ODM and REDCap ([#1669](https://github.com/fderuiter/imednet-toolkit/issues/1669)) ([e16658a](https://github.com/fderuiter/imednet-toolkit/commit/e16658a4f9a4b57f93ed1b2fda596464b15ec86e))
* **jobs:** implement pydantic job lifecycle state machine with monotonic transition validation ([#1674](https://github.com/fderuiter/imednet-toolkit/issues/1674)) ([a8f171e](https://github.com/fderuiter/imednet-toolkit/commit/a8f171eeb48df2108c2ad3b26716f9b4792fa047))
* query lifecycle state machine with formal discrepancy status enum ([#1673](https://github.com/fderuiter/imednet-toolkit/issues/1673)) ([9176fe3](https://github.com/fderuiter/imednet-toolkit/commit/9176fe363ba7d3425f9d6996e69b6896e0faf7ef))
* **records:** add to_dataframe export to records endpoints ([#1658](https://github.com/fderuiter/imednet-toolkit/issues/1658)) ([8390b1e](https://github.com/fderuiter/imednet-toolkit/commit/8390b1edc88e77dda3ea6e9ec8a8f2b200a938ce))
* validate deterministic status transitions in TriageStore ([#1655](https://github.com/fderuiter/imednet-toolkit/issues/1655)) ([3afdb60](https://github.com/fderuiter/imednet-toolkit/commit/3afdb60a3f13d34d793cd6b3f1661c99dca5245b))


### Bug Fixes

* **cli:** enhance central CLI dispatcher for active subparser help ([#1667](https://github.com/fderuiter/imednet-toolkit/issues/1667)) ([4bedd72](https://github.com/fderuiter/imednet-toolkit/commit/4bedd722609c1f5fbc8df5bc5966e74b5cdef0d0))
* **cli:** redirect CLI errors to standard error stream ([#1701](https://github.com/fderuiter/imednet-toolkit/issues/1701)) ([b51b8c1](https://github.com/fderuiter/imednet-toolkit/commit/b51b8c199b8b2c06f68be0e41dfac75630a8d634))
* **export:** validate SQL identifiers, directory paths, and study keys in DuckDB queries ([#1657](https://github.com/fderuiter/imednet-toolkit/issues/1657)) ([39eaa07](https://github.com/fderuiter/imednet-toolkit/commit/39eaa076b4a09e41f862f4c1b670c47c9e6888fa))
* **security:** migrate datadict XML parsing to defusedxml ([#1654](https://github.com/fderuiter/imednet-toolkit/issues/1654)) ([2c53cad](https://github.com/fderuiter/imednet-toolkit/commit/2c53cad8c0680fe0f6dd882e5748e1fdd71ff765))
* **security:** replace eval with restricted AST expression evaluation engine ([#1671](https://github.com/fderuiter/imednet-toolkit/issues/1671)) ([dccd8be](https://github.com/fderuiter/imednet-toolkit/commit/dccd8be5f64cdde01b7dc201903f1a3c611e548f))


### Performance Improvements

* **export:** prefetch variable metadata in DuckDB form export ([#1668](https://github.com/fderuiter/imednet-toolkit/issues/1668)) ([0b57cd7](https://github.com/fderuiter/imednet-toolkit/commit/0b57cd7a6a16b408f9b8fa98466ec52f43473155))


### Documentation

* align domain vocabulary across interfaces and models ([#1666](https://github.com/fderuiter/imednet-toolkit/issues/1666)) ([712409b](https://github.com/fderuiter/imednet-toolkit/commit/712409b7de232cdd710d4073bab4dd8276a77e9b))
* integrate linkcheck builder and repair broken links ([#1699](https://github.com/fderuiter/imednet-toolkit/issues/1699)) ([56d15b2](https://github.com/fderuiter/imednet-toolkit/commit/56d15b29f9e859cf66b88103ef8c8acee2804d63))

## [0.10.1](https://github.com/fderuiter/imednet-toolkit/compare/imednet-v0.10.0...imednet-v0.10.1) (2026-10-23)


### Bug Fixes

* **smoke:** accept coding timestamps and preserve failure reporting ([#1623](https://github.com/fderuiter/imednet-toolkit/issues/1623)) ([6fb02dc](https://github.com/fderuiter/imednet-toolkit/commit/6fb02dc0c0bca7023076dcf6fed9b7a4bd7f3c68))

## [0.10.0](https://github.com/fderuiter/imednet-toolkit/compare/imednet-v0.9.1...imednet-v0.10.0) (2026-09-30)


### Features

* **datadict:** standardize canonical data dictionary subsystem ([#1584](https://github.com/fderuiter/imednet-toolkit/issues/1584)) ([e7233ac](https://github.com/fderuiter/imednet-toolkit/commit/e7233aca98d6df74cb08441599ffb4c5e49cbc5c))

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
