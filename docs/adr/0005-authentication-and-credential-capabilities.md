# Authentication and credential capabilities

## Decision

Keep BaseClient and HTTPClientBase and their public imports. An explicit AuthStrategy
owns authentication and must initialize both transports and SDKs without placeholder
keys. Resolve noncredential configuration normally, then bypass only default credential
validation. The strategy wins even if it has false boolean value. Reject auth together
with an injected client because that client already owns its headers.

Credential precedence per field is explicit argument, caller-provided CredentialSource
snapshot, then environment. Explicit empty values suppress fallback and fail normal
validation. Once resolved, OIDC takes priority over an API/security pair as before;
select one mechanism rather than leaving an unintended environment token. CLI dotenv
loading remains opt-in fallback to environment loading, then uses the same load_config.
No TUI/MCP is currently shipped; future clients must use this same boundary.

Offer a caller-owned MemoryCredentialStore and a read-only CredentialSource protocol.
The source is read once when constructing a client. Overwrite/clear affects future
clients; no implicit renewal, global registry, credential deletion or mutation of user
stores occurs. Missing snapshots permit environment fallback; lookup failures fail
closed with a generic error and no original message. Config, store, credentials and
built-in auth reprs are redacted. Third-party sources/strategies own their own logging.

No requirement demonstrates a need for OS keyring or encrypted persistence. Defer both
rather than introducing key management, unlock UI and platform dependencies. Nothing
writes credentials to disk. Python strings cannot guarantee memory zeroization.

SDK-created transports are closed by the SDK context manager on success or failure.
For compatibility, passing client/async_client transfers close ownership to the SDK,
matching existing behavior; callers must not share that client across SDK owners.
The CredentialSource remains caller-owned and is never cleared by SDK shutdown.
General plugin-initialization and cancellation lifetime coverage remains owned by issue #1686.

## Validation

Synthetic strategies and HTTP transports verify sync/async requests without credentials,
false-valued strategy selection, environment/explicit/source precedence, OIDC selection,
source failure redaction, overwrite/clear/fallback and owned/injected client cleanup.
No real EDC, user store, token refresh or persistent adapter is used.
