# Nango MCP Server

A public, self-hostable MCP server for Nango management and provider API access.

Created by Lev Jampolsky at ValStratis.

This independent project is not affiliated with or endorsed by Nango. See [Nango](https://www.nango.dev/), the [Nango repository](https://github.com/NangoHQ/nango), and [Nango documentation](https://nango.dev/docs).

Version 1.0 provides:

- stdio by default and optional Streamable HTTP at `/mcp`
- direct environment secrets or optional Infisical resolution
- static bearer policies or OAuth protected-resource authorization for HTTP
- native MCP approval flows for mutations
- redacted Nango management responses
- provider requests through Nango Proxy with safe 429 retry behavior
- bounded structured results, protected response artifacts, and resource links
- streamed provider downloads exposed as protected MCP resources

## Install

Run directly with `uvx`:

```bash
uvx --from git+https://github.com/LevSky22/nango-mcp-server.git@v1.0.0 nango-mcp
```

Or install persistently:

```bash
pipx install git+https://github.com/LevSky22/nango-mcp-server.git@v1.0.0
```

For development:

```bash
git clone https://github.com/LevSky22/nango-mcp-server.git
cd nango-mcp-server
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

## Basic stdio configuration

Create a private `.env` file:

```dotenv
NANGO_BASE_URL=https://api.nango.dev
NANGO_ENVIRONMENT=default
NANGO_SECRET_KEY=replace_with_your_nango_environment_secret_key
```

`NANGO_SECRET_KEY` is the Nango environment secret key, not a provider credential. Keep it server-side and never commit it.

For several environments:

```dotenv
NANGO_MCP_ENVIRONMENTS=development,production
NANGO_SECRET_KEY_DEVELOPMENT=replace_with_development_secret
NANGO_SECRET_KEY_PRODUCTION=replace_with_production_secret
NANGO_MCP_ENVIRONMENT_ALIASES_PRODUCTION=live
```

Point the process at the file when its working directory differs:

```bash
NANGO_MCP_ENV_FILE=/absolute/path/to/.env nango-mcp
```

Generic MCP client configuration:

```json
{
  "mcpServers": {
    "nango": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/LevSky22/nango-mcp-server.git@v1.0.0",
        "nango-mcp"
      ],
      "env": {
        "NANGO_MCP_ENV_FILE": "/absolute/path/to/.env"
      }
    }
  }
}
```

Stdio is the default. It uses a local implicit caller scope over the configured environments. Set `NANGO_MCP_READ_ONLY=true` to disable mutations entirely.

## Streamable HTTP

HTTP mode binds to loopback by default and requires authentication plus rotating request-state keys:

```dotenv
NANGO_MCP_TRANSPORT=http
NANGO_MCP_HTTP_HOST=127.0.0.1
NANGO_MCP_HTTP_PORT=3000
NANGO_MCP_REQUEST_STATE_KEYS=replace_with_at_least_32_random_bytes
```

`GET /health` is an unauthenticated shallow health check. MCP traffic uses `/mcp`.

### Static bearer policy

Static mode is suited to local and private-network deployments. Tokens must start with `nangomcp1_` and contain at least 43 URL-safe random characters. Store only their lowercase SHA-256 digests:

```bash
python -c 'import hashlib; print(hashlib.sha256(input().strip().encode()).hexdigest())'
```

```dotenv
NANGO_MCP_AUTH_MODE=static
NANGO_MCP_TOKENS={"replace_with_sha256_digest":{"label":"local-automation","scopes":["development"],"allowed_proxy_methods":["GET","HEAD"],"mutation_approval":"server"}}
```

Policy fields are:

- `label`: non-secret audit identity
- `scopes`: environment names or `['*']`
- `denied_tools`: optional tool names
- `allowed_proxy_methods`: methods or `['*']`
- `denied_proxy_path_patterns`: optional regular expressions
- `mutation_approval`: `server` or `host`
- `server_approval_proxy_path_patterns`: routes that remain server-approved

Use `NANGO_MCP_TOKEN_REGISTRY_FILE` instead of inline JSON for atomic hot reloads.

### OAuth protected-resource mode

OAuth mode expects an external authorization server and validates opaque access tokens through RFC 7662 introspection:

```dotenv
NANGO_MCP_AUTH_MODE=oauth
NANGO_MCP_OAUTH_ISSUER_URL=https://identity.example.com
NANGO_MCP_OAUTH_RESOURCE_URL=https://mcp.example.com/mcp
NANGO_MCP_OAUTH_INTROSPECTION_URL=https://identity.example.com/oauth/introspect
NANGO_MCP_OAUTH_CLIENT_ID=nango-mcp-resource-server
NANGO_MCP_OAUTH_CLIENT_SECRET=replace_with_resource_server_secret
NANGO_MCP_OAUTH_REQUIRED_SCOPES=nango-mcp
```

Access tokens use these scopes:

- `nango-mcp`: required base scope
- `nango:env:<name>` or `nango:env:*`: environment access
- `nango:read`: read intent
- `nango:write`: mutation access
- `nango:proxy`: proxy and download access

Issuer, resource, and introspection URLs must use HTTPS except for loopback development. The server validates activity, expiry, required scopes, and audience/resource.

## Approvals

Mutation tools use MCP-native approval flows. There are no confirmation-string arguments.

- `mutation_approval=server` asks for server-bound approval on every mutation.
- `mutation_approval=host` delegates routine writes to host policy.
- destructive actions always require server approval.
- matching proxy routes can be forced back to server approval.

Approval state is time-limited and bound to the caller policy, environment, tool, arguments, effect, and current target snapshot. Modern MCP clients receive `input_required`; compatible older sessions use elicitation.

## Large responses and MCP resources

`proxy_request` never emits an unbounded model-facing result. Small JSON responses remain inline. Large responses are stored privately and returned as:

- a bounded preview in `structuredContent`
- `responseMeta` with completeness, pagination, and artifact metadata
- a `resource_link` using `nango-mcp://artifact/<id>`

The host can fetch the complete immutable representation with MCP `resources/read`. Models should use `query_response_artifact` for bounded selection, projection, filtering, shape description, pagination, keyed-object entries, or literal text search.

This hybrid follows MCP's separation of concerns: tools initiate computation, resources carry application-controlled context, and resource links let hosts decide whether full content enters model context. See the MCP guidance for [resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources) and [tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).

Defaults:

- inline target: 32 KiB
- stored-result preview: 8 KiB
- hard emitted result limit: 128 KiB
- artifact limit: 50 MiB
- artifact TTL: 24 hours
- artifact quota: 1 GiB
- artifact directory/file modes: `0700`/`0600`

Override storage with `NANGO_MCP_ARTIFACT_ROOT`, `NANGO_MCP_ARTIFACT_MAX_BYTES`, and `NANGO_MCP_ARTIFACT_TTL_SECONDS`.

`download_provider_file` streams a provider GET with `Accept: */*`, enforces the size limit, computes SHA-256, and returns `nango-mcp://download/<id>` as a protected resource link. Results never expose a server filesystem path.

## Proxy v1 contract

The proxy, artifact-query, and download tools use strict camelCase wire names. Unknown arguments are rejected.

`proxy_request` accepts:

```text
environment, providerConfigKey, connectionId, method, path,
query, headers, baseUrlOverride, body, responseMode,
responsePath, fields, filters, pageSize, cursor
```

MCP-owned response fields include `contentType`, `responseHeaders`, `rateLimit`, and `responseMeta`. Provider JSON under `response` is preserved exactly.

`query_response_artifact` accepts:

```text
environment, artifactId, responsePath, fields, filters,
pageSize, cursor, describe, objectMode, textSearch
```

Paths use RFC 6901 JSON Pointer. `pageSize` defaults to 20 and is capped at 100. Cursors are signed and bound to the caller, environment, artifact, and query view.

Rate-limit handling distinguishes Nango gateway throttles from forwarded provider throttles by Nango's response body. Gateway rejections are safe to retry for any method because they were not forwarded. Provider rejections are replayed only for GET, HEAD, and OPTIONS.

## Tools

The server exposes 26 tools, grouped by workflow:

| Area | Tools | Access |
| --- | --- | --- |
| Environments | `list_environments`, `check_environment` | Read |
| Provider discovery | `search_provider_templates`, `download_provider_file` | Read |
| Integrations | `list_integrations`, `get_integration`, `create_integration`, `update_integration`, `delete_integration` | Read/write |
| Connections | `list_connections`, `get_connection`, `get_connection_context`, `refresh_connection_credentials`, `import_connection`, `delete_connection` | Read/write |
| Tags and metadata | `patch_connection_tags`, `set_connection_metadata` | Write |
| Connect sessions | `create_connect_session`, `create_standard_connect_session`, `create_reconnect_session` | Write |
| Provider API and large responses | `proxy_request`, `query_response_artifact` | Read/write |
| Optional connection conventions | `describe_connection_convention`, `build_connection_convention`, `apply_connection_convention`, `audit_connection_conventions` | Read/write |

See the [complete tool reference](https://github.com/LevSky22/nango-mcp-server/blob/main/docs/tools.md) for each tool's purpose, important inputs, result behavior, and safety notes. The MCP `tools/list` schema remains authoritative for exact input types and required fields.

Management responses redact credential-like fields. `refresh_connection_credentials` returns only a non-secret summary. Non-GET/HEAD proxy requests and all other mutations follow the approval policy described above.

The connection convention helpers are optional. They generate generic Nango tags and metadata; they are not profiles and do not discover organizations or impose deployment-specific policy.

## Optional Infisical resolver

```dotenv
NANGO_MCP_SECRET_RESOLVER=infisical
NANGO_MCP_ENVIRONMENTS=development,production
INFISICAL_URL=https://infisical.example.com
INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=replace_with_machine_identity_id
INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=replace_with_machine_identity_secret
NANGO_MCP_INFISICAL_PROJECT_ID=replace_with_project_id
NANGO_MCP_INFISICAL_ENVIRONMENT=production
NANGO_MCP_INFISICAL_SECRET_PATH_TEMPLATE=/nango/{environment}
NANGO_MCP_INFISICAL_SECRET_NAME=NANGO_SECRET_KEY
```

## Docker

Build locally:

```bash
docker build -t nango-mcp:1.0.0 .
```

HTTP containers must receive the Nango secret configuration, authentication policy, and request-state keys at runtime. Do not bake secrets into an image.

## Development and security

```bash
python -m pytest -q
python -m build
python -m twine check dist/*
```

CI tests Python 3.11 through 3.13, builds the distribution, checks package metadata, scans Git history for secrets, and scans the filesystem for high/critical vulnerabilities.

Do not put Nango keys, OAuth tokens, Infisical credentials, provider payloads, or customer data in issues, tests, fixtures, logs, or commits. Audit logs contain identities, tool names, outcomes, timing, and bounded operational metadata—not tokens, headers, arguments, or payloads.

See [MIGRATION.md](MIGRATION.md) when upgrading from 0.x.

## License

MIT. Attribution is appreciated and preserved in the license.
