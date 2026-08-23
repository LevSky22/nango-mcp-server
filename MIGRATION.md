# Migrating from 0.x to 1.0

Version 1.0 is intentionally breaking. Update the package and MCP client configuration together.

## Runtime and transport

- The package now uses MCP Python SDK 2.x and requires Python 3.11 or newer.
- Stdio remains the default.
- Streamable HTTP is enabled with `NANGO_MCP_TRANSPORT=http` and serves MCP at `/mcp`.
- HTTP requires `NANGO_MCP_REQUEST_STATE_KEYS` and either static bearer or OAuth resource-server authentication.

## Mutation approval

Remove every `confirmation` argument and delete `NANGO_MCP_REQUIRE_CONFIRMATION`. Mutations now use MCP-native `input_required` or elicitation. Keep `NANGO_MCP_READ_ONLY=true` for deployments that must expose no writes.

Static bearer policies choose `mutation_approval=server` or `mutation_approval=host`. DELETE operations always remain server-approved.

## Proxy arguments

Rename proxy arguments:

| 0.x | 1.0 |
| --- | --- |
| `provider_config_key` | `providerConfigKey` |
| `connection_id` | `connectionId` |
| `base_url_override` | `baseUrlOverride` |
| `response_mode` | `responseMode` |
| `response_path` | `responsePath` |
| `response_page_size` | `pageSize` |
| `response_cursor` | `cursor` |
| `response_filter` | `filters` |

Unknown proxy arguments now fail validation. Provider JSON under `response` is unchanged, but MCP wrapper fields are camelCase: `contentType`, `responseHeaders`, `rateLimit`, and `responseMeta`.

## Large responses

`proxy_request` no longer returns an unbounded JSON text blob. Read the bounded `structuredContent` first.

- Follow `responseMeta.nextCursor` with the same query view for another bounded page.
- Use `query_response_artifact` for structured inspection.
- Let the MCP host fetch a returned `resource_link` when the complete representation is needed.
- Do not expect or parse local filesystem paths.

Artifact query arguments are strict camelCase: `artifactId`, `responsePath`, `pageSize`, `objectMode`, and `textSearch`.

## Downloads

Use `download_provider_file` for provider binary responses. It returns metadata plus a protected MCP resource link; it does not return a host path.
