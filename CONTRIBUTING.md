# Contributing

Thanks for considering a contribution to Nango MCP Server.

## Development Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
python -m pytest -q
```

## Before Opening A PR

- Run `python -m pytest -q`.
- Keep changes focused on one behavior or bug fix.
- Update `README.md` or `.env.example` when configuration or tool behavior changes.
- Update `MIGRATION.md` for breaking wire-contract changes and keep tool headings in `docs/tools.md` synchronized with `tools/list`.
- Do not commit `.env`, secrets, OAuth tokens, provider payloads, or customer data.
- Use neutral fixtures such as `sandbox`, `sample-integration`, and `person@example.test`; never copy private deployment names, aliases, routes, or identifiers into public changes.
- Prefer tests around request shape and redaction behavior when touching Nango API calls.

## Security And Secret Handling

- Use fake keys in tests and docs.
- Keep live `NANGO_SECRET_KEY` values in your own secret manager or local gitignored `.env`.
- Never paste provider OAuth tokens, Nango secret keys, Infisical credentials, or full provider API responses into issues or PRs.
- `proxy_request` returns provider data by design. Be careful when sharing logs from it.

## Code Style

- Keep the project dependency-light.
- Use clear Python type hints for public helpers and tool-facing data structures.
- Keep management API responses redacted for credential-like fields.
- Keep write/delete guardrails intact unless a replacement safety model is added.
- Preserve the query-only boundary for stored JSON values. Resource links may expose bounded descriptors; provider binary downloads are the only raw-readable artifact resources.

## Licensing

By contributing, you agree that your contribution is licensed under the MIT License in this repository.
