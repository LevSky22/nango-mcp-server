# Nango MCP Server

Self-hostable admin/operator MCP server for managing one or more Nango environments through the Nango REST API and Proxy.

Created by Lev Jampolsky at ValStratis.

This project is an independent MCP server for [Nango](https://www.nango.dev/). It is not affiliated with or endorsed by Nango. See the upstream Nango repository at [NangoHQ/nango](https://github.com/NangoHQ/nango) and Nango docs at [nango.dev/docs](https://nango.dev/docs).

It exposes tools for:

- environment checks
- provider template search
- integration management
- connection management
- connection tags and metadata
- Connect and reconnect sessions
- provider API calls through Nango Proxy

The default setup reads Nango secret keys from environment variables or a `.env` file. Infisical is optional.

## Status

This project is early-stage. It has been tested against a self-hosted Nango deployment and may need compatibility adjustments for Nango Cloud or future Nango API changes.

## Why This Exists

Nango already solves OAuth, token refresh, provider auth injection, and proxying. This MCP gives agents and operators a structured control surface for Nango administration and provider API calls without exposing provider tokens to the agent.

It is meant for the operator side of Nango usage: inspecting environments, managing integrations and connections, creating Connect sessions, maintaining tags/metadata, and calling providers through Nango Proxy.

Management API responses are redacted for credential-like fields. `proxy_request` intentionally returns provider response data because that is the requested data plane.

## Install

Recommended for MCP clients:

```bash
uvx --from git+https://github.com/LevSky22/nango-mcp-server.git nango-mcp
```

`uvx` is the Python equivalent of the `npx` pattern commonly used by MCP servers: it runs the package in an isolated environment without making you manage a project virtualenv.

This requires `uv` / `uvx` to be installed. If you do not use `uv`, use `pipx` instead.

For a persistent user-level install:

```bash
pipx install git+https://github.com/LevSky22/nango-mcp-server.git
```

Then your MCP client can use:

```bash
nango-mcp
```

For local development:

```bash
git clone https://github.com/LevSky22/nango-mcp-server.git
cd nango-mcp-server
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
```

Use the virtualenv command path in MCP configs when working from a checkout:

```bash
/absolute/path/to/nango-mcp-server/.venv/bin/nango-mcp
```

## Configuration

Create a `.env` file for the MCP server. If you run `nango-mcp` from this project checkout, `.env` can live in the repository root:

```bash
cp .env.example .env
chmod 600 .env
```

If your MCP client launches `nango-mcp` from another working directory, set `NANGO_MCP_ENV_FILE` to an absolute path so the server can find the file reliably:

```bash
NANGO_MCP_ENV_FILE=/path/to/.env
```

Single environment:

```dotenv
NANGO_BASE_URL=https://api.nango.dev
NANGO_ENVIRONMENT=default
NANGO_SECRET_KEY=nango_secret_key_here
```

### Finding Your Nango Secret Key

`NANGO_SECRET_KEY` is the Nango environment secret key, not a provider OAuth client secret and not a provider API key.

In the Nango UI:

1. Select the Nango environment you want this MCP server to operate against.
2. Open that environment's **Environment Settings**.
3. Copy the **Secret Key**.
4. Put it in your local `.env` as `NANGO_SECRET_KEY`, or as `NANGO_SECRET_KEY_<ENV>` for a multi-environment setup.

Nango uses this key as the bearer token for backend/API requests. Anyone with this key can operate against that Nango environment, so keep it server-side and never commit it.

Multiple environments:

```dotenv
NANGO_BASE_URL=https://api.nango.dev
NANGO_MCP_ENVIRONMENTS=dev,prod
NANGO_SECRET_KEY_DEV=nango_dev_secret_key_here
NANGO_SECRET_KEY_PROD=nango_prod_secret_key_here
NANGO_MCP_ENVIRONMENT_ALIASES_PROD=live,production
```

Optional settings:

```dotenv
NANGO_MCP_REQUEST_TIMEOUT=20
NANGO_MCP_METADATA_NAMESPACE=nango_mcp
```

## Optional Infisical Resolver

Direct `.env` secrets are the default. To resolve `NANGO_SECRET_KEY` values from Infisical instead:

```dotenv
NANGO_MCP_SECRET_RESOLVER=infisical
NANGO_MCP_ENVIRONMENTS=dev,prod

INFISICAL_URL=https://infisical.example.com
INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=...
INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=...
NANGO_MCP_INFISICAL_PROJECT_ID=...
NANGO_MCP_INFISICAL_ENVIRONMENT=prod
NANGO_MCP_INFISICAL_SECRET_PATH_TEMPLATE=/nango/{environment}
NANGO_MCP_INFISICAL_SECRET_NAME=NANGO_SECRET_KEY
```

For environment `prod`, the default template reads secret `NANGO_SECRET_KEY` from `/nango/prod`.

## MCP Client Example

Generic MCP JSON using `uvx`:

```json
{
  "mcpServers": {
    "nango": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/LevSky22/nango-mcp-server.git",
        "nango-mcp"
      ],
      "env": {
        "NANGO_MCP_ENV_FILE": "/absolute/path/to/.env"
      }
    }
  }
}
```

### Codex CLI

Use Codex's MCP command:

```bash
codex mcp add nango \
  --env NANGO_MCP_ENV_FILE=/absolute/path/to/.env \
  -- uvx --from git+https://github.com/LevSky22/nango-mcp-server.git nango-mcp
```

Or edit `~/.codex/config.toml` directly:

```toml
[mcp_servers.nango]
command = "uvx"
args = ["--from", "git+https://github.com/LevSky22/nango-mcp-server.git", "nango-mcp"]

[mcp_servers.nango.env]
NANGO_MCP_ENV_FILE = "/absolute/path/to/.env"
```

If you installed with `pipx`, use `command = "nango-mcp"` and remove `args`. Restart Codex after editing the config.

### Claude Code

Add the server with Claude Code's MCP command:

```bash
claude mcp add --transport stdio \
  --env NANGO_MCP_ENV_FILE=/absolute/path/to/.env \
  nango \
  -- uvx --from git+https://github.com/LevSky22/nango-mcp-server.git nango-mcp
```

If you installed with `pipx`, replace everything after `--` with `nango-mcp`. Restart Claude Code, or reload MCP servers if your client supports it.

### Starter Prompt

After installing, use a read-only prompt first:

```text
Use the nango MCP server. List the configured Nango environments, check the default environment, and list integrations. Do not create, update, delete, or proxy provider requests yet.
```

For a multi-environment setup:

```text
Use the nango MCP server. List configured environments, check the prod environment, and list integrations in prod. Do not make write/delete calls.
```

## Tool Notes

Write operations require:

```text
I understand this changes the Nango environment
```

Delete operations require:

```text
I understand this deletes Nango configuration
```

`proxy_request` accepts provider-relative paths such as `/v1.0/me`; do not include `/proxy`.

## Tags And Metadata

This project follows Nango's public guidance:

- Use tags for attribution, filtering, routing, and webhook reconciliation.
- Recommended tags: `end_user_id`, `end_user_email`, `organization_id`.
- Optional routing tags: `workspace_id`, `project_id`, `environment`.
- Use metadata for application/function configuration.
- Do not store credentials in tags or metadata.

The optional convention helpers can generate tags and metadata under `metadata.nango_mcp` by default. Change that namespace with `NANGO_MCP_METADATA_NAMESPACE`.

## Development

```bash
python -m pytest -q
```

## Security

Please do not open public issues containing Nango secret keys, provider OAuth tokens, Infisical credentials, customer data, or raw provider API responses. If you need to report a sensitive issue, use a private disclosure channel instead of a public issue.

## License

MIT. Attribution is appreciated and preserved in the license.
