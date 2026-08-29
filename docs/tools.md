# Tool reference

Nango MCP Server exposes 26 tools. This page explains when to use them and highlights behavior that is easy to miss. The MCP `tools/list` response is authoritative for exact JSON Schema types, required fields, and defaults.

Access labels describe intent:

- **Read** does not intentionally change Nango or provider state.
- **Write** changes state and is subject to server read-only mode, caller policy, and MCP-native approval where applicable.
- `proxy_request` is read or write according to its HTTP method.

All tools that accept `environment` enforce the authenticated caller's environment scope. Management responses redact credential-like fields.

## Environments

### `list_environments`

**Read.** Lists the configured environments and aliases visible to the caller without returning secret material. Set `refresh` to report refresh intent; use `check_environment` to resolve a specific secret.

### `check_environment`

**Read.** Resolves one configured environment and reports whether it is ready. `refresh` requests a fresh secret resolution. The secret itself is never returned.

## Provider discovery

### `search_provider_templates`

**Read.** Searches Nango's provider catalog before an integration is created. Important inputs are `environment`, `query`, `limit`, and `includeRawTemplates`; results include compact matches and any related configured integrations.

### `download_provider_file`

**Read.** Streams a provider GET through Nango Proxy into a protected MCP resource. It accepts strict camelCase inputs including `providerConfigKey`, `connectionId`, `path`, and optional query, headers, base URL override, and suggested filename. The result contains a `nango-mcp://download/<id>` resource link, content metadata, and SHA-256—not a server filesystem path.

## Integrations

### `list_integrations`

**Read.** Lists integrations in one environment. `refreshSecret` can request fresh environment-secret resolution.

### `get_integration`

**Read.** Gets one integration by `integrationId`. Credential-like fields remain redacted even when `includeCredentials` asks Nango for credential configuration.

### `create_integration`

**Write.** Creates an integration using Nango's API payload shape. The server does not embed provider- or deployment-specific templates.

### `update_integration`

**Write.** Patches an integration using `fields`. OAuth scope changes can also create reconnect sessions for explicitly supplied connection IDs, or for a single unambiguous matching connection when automatic inference is enabled.

### `delete_integration`

**Write, destructive.** Deletes an integration by `integrationId`. Destructive actions always require server-bound approval.

## Connections

### `list_connections`

**Read.** Lists connections with optional `connectionId`, `integrationId`, text search, native `endUserId`, `endUserOrganizationId`, and limit filters.

### `get_connection`

**Read.** Gets a connection using `connectionId` and `providerConfigKey`. Credential-like response fields are always redacted.

### `get_connection_context`

**Read.** Returns a compact, redacted view combining connection, integration, provider-template, identity, tag, metadata, and visible scope information. Raw provider-template inclusion is optional.

### `refresh_connection_credentials`

**Write.** Forces an OAuth credential refresh for a connection. It returns only booleans and non-secret token metadata such as type, scope, and expiry; access and refresh tokens are never returned.

### `import_connection`

**Write.** Imports or creates a connection using Nango's API payload shape. Treat payload credential fields as secrets and never place them in logs, fixtures, or issues.

### `delete_connection`

**Write, destructive.** Deletes a connection identified by `connectionId` and `providerConfigKey`. Destructive actions always require server-bound approval.

## Tags and metadata

### `replace_connection_tags`

**Write.** Replaces the connection's complete tag object. Fetch and merge the existing tags first when changing only selected keys.

### `update_connection_metadata`

**Write.** Merges or replaces connection metadata according to `mode`, which defaults to `merge`. Metadata is for application configuration, not credentials or large synchronized datasets.

## Connect sessions

### `create_connect_session`

**Write.** Creates a generic Nango Connect session for `allowed_integrations`, with optional tags and integration configuration defaults.

### `create_standard_connect_session`

**Write, optional convention.** Creates a single-integration Connect session with generic attribution tags and returns a post-auth finalization contract. It does not discover identities or impose deployment-specific policy.

### `create_reconnect_session`

**Write.** Creates a reconnect session for an existing `connectionId` and `providerConfigKey`.

## Provider API and large responses

### `proxy_request`

**Read/write.** Calls a provider API through Nango Proxy without exposing provider tokens. GET and HEAD are read operations; other methods are mutations and require approval. Its public contract uses strict camelCase names and rejects unknown arguments.

Small JSON results remain inline. Large results return a bounded preview, `responseMeta`, and an optional descriptor link using `nango-mcp://artifact/<id>`. Use `responseMode`, `responsePath`, `fields`, `filters`, `pageSize`, and `cursor` to control the returned view. Provider JSON beneath `response` is preserved verbatim. Complete object/array JSON is recognized even when a provider sends a missing or incorrect media type, with an explicit warning.

### `query_response_artifact`

**Read.** Queries a contract-v2 stored response artifact without loading the complete representation into model context. It supports JSON Pointer selection, field projection, filters, signed pagination cursors, shape description, keyed-object entry views, and literal text search. Exact pointers win; a missing provider-relative pointer is tried beneath `/response` and the canonical selection is returned. Inputs use strict camelCase and unknown arguments are rejected.

MCP `resources/read` returns only a bounded, authorized descriptor for JSON artifacts. `query_response_artifact` is the only model-facing JSON value reader.

## Optional connection conventions

These helpers provide generic tag and metadata conventions. They are optional and do not perform identity discovery or impose deployment-specific policy.

### `describe_connection_convention`

**Read.** Explains the suggested division between attribution/routing tags and application metadata, including fields that should not contain credentials.

### `build_connection_convention`

**Read.** Builds a suggested deterministic connection ID, tags, and namespaced metadata without changing Nango state.

### `apply_connection_convention`

**Write.** Merges suggested attribution tags and writes convention metadata to an existing connection. Existing tags are fetched before the update.

### `audit_connection_conventions`

**Read.** Audits a bounded set of connections for missing suggested fields and returns required issues separately from recommendations. It does not modify connections.

## Result and safety conventions

- Management API results recursively redact credential-like field names.
- Mutations are disabled when `NANGO_MCP_READ_ONLY=true`.
- Caller policy can deny tools, restrict environments and proxy methods, and force approval for selected proxy paths.
- Destructive operations always require server-bound approval.
- Large provider responses use bounded structured output plus protected artifacts; provider downloads use protected binary resources.
- JSON artifact resource reads are descriptor-only; binary download reads return bytes. Both are authorized against the original caller and environment scope.
