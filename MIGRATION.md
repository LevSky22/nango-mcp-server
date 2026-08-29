# Migrating from 1.x to 2.0

Version 2.0 standardizes every tool contract and strengthens the artifact and mutation boundaries. Update the server and MCP client tool cache together; old response artifacts and cursors are intentionally not reusable.

## Tool and argument changes

- Every tool now rejects unknown arguments and uses strict camelCase wire names.
- Rename `patch_connection_tags` to `replace_connection_tags`.
- Rename `set_connection_metadata` to `update_connection_metadata`, and replace its boolean patch switch with `mode="merge"` or `mode="replace"`.
- `list_connections` now exposes Nango's documented `connectionId`, `integrationId`, `search`, `endUserId`, `endUserOrganizationId`, and `limit` filters. Arbitrary tag-query expansion was removed.
- `apply_connection_convention` no longer accepts display-name or email overrides. It projects identity tags only from the native Nango `end_user` record.
- Add `update_connection_end_user` for native `id`, `email`, and `displayName` updates. It is deliberately separate from tag and metadata tools.

Legacy snake_case arguments and retired tool names are not aliases in v2.

## Response artifacts

- Response artifacts and cursors use contract version 2. Re-run the provider request to mint a v2 artifact, and repeat the query to mint a v2 cursor.
- `responsePath` still accepts exact RFC 6901 pointers. When an exact path is absent, v2 tries it once beneath the advertised `/response` root and reports the canonical path in `responseMeta.inferredResponsePath`.
- Complete object/array JSON can be recovered when a provider sends a missing or incorrect media type. The response includes a warning when inference was required.
- Provider 4xx/5xx envelopes take precedence over shaping controls, so an invalid `responsePath` can no longer hide the upstream failure.
- MCP `resources/read` for `nango-mcp://artifact/<id>` now returns a bounded descriptor, never the raw provider payload. Use `query_response_artifact` as the sole JSON value reader. Binary `nango-mcp://download/<id>` resources remain byte-readable.

## Mutation request bodies

Inline mutation bodies are limited to 4 KiB, 40 total collection entries, and JSON depth 8. When `proxy_request` returns `INLINE_BODY_REQUIRES_STAGING`:

1. Call `stage_proxy_request_body` with the same `environment` and exact `body`.
2. Retry `proxy_request` with the returned `bodyArtifactId`.
3. Omit `body`; the two arguments are mutually exclusive.

Staged bodies are immutable, expiring, caller/environment-bound, HMAC content-bound, and digest-verified immediately before transmission. They have no read, list, query, or resource interface.

## Approval and OAuth policy

- `mutation_approval=host` may delegate only exact-target provider DELETE paths with an unambiguous in-path ID and no query or body. Collection nouns, bulk/wildcard/template paths, query/body deletes, ambiguous targets, and configured override patterns remain server-approved.
- OAuth provider reads require both `nango:proxy` and `nango:read`; provider mutations and staging require both `nango:proxy` and `nango:write`. Downloads require proxy plus read.

## Migrating from 0.x to 1.0

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
