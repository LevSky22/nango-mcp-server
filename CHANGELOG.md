# Changelog

## 2.0.0 - 2026-08-29

### Added

- Immutable outbound request-body staging for large provider mutations.
- Safe native connection end-user updates with preflight and read-back verification.
- Output schemas for proxy, artifact-query, staging, and end-user results.

### Changed

- Standardized all 28 tool schemas on strict camelCase inputs.
- Moved response artifacts and signed cursors to contract version 2.
- Made JSON artifact resources descriptor-only; bounded artifact queries are the sole JSON value interface.
- Added provider-relative response-path normalization, structured media-type recovery, and provider-error precedence.
- Split OAuth proxy permissions by read and write capability.
- Allowed trusted hosts to review conservative exact-target provider DELETE operations while keeping broad deletes server-approved.

### Removed

- Legacy snake_case tool arguments and retired tag/metadata tool names.
- Arbitrary connection tag-query expansion.
- Raw JSON artifact reads through MCP resources.

See [MIGRATION.md](MIGRATION.md) for upgrade instructions.
