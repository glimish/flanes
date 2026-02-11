# Changelog

All notable changes to Flanes are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.4.4] - 2026-02-11

### Added
- Schema migration framework with `schema_version` table and ordered migrations
- REST API request body size limit (10 MB, returns HTTP 413 on oversize)
- CHANGELOG.md, SECURITY.md, and Non-Goals section in README

### Changed
- Thread safety contract: "one Repository per thread" is now the official guidance (shared-repo access was never safe due to unsynchronized `_in_batch` flag)

### Fixed
- Concurrent test fixture overrides `max_blob_size` to 1 MB (was 100 MB default, ~100x faster)

## [0.4.3] - 2026-02-11

### Added
- Web UI upgrade: timeline swim-lane view, trace/lineage DAG, inline file diffs, search, workspace panel
- Hash-based routing, tooltip system, and search form in web viewer

### Fixed
- MCP tool names in guide (`fla_` prefix corrected to `flanes_`)
- MCP server version now uses `__version__` instead of hardcoded string
- Guide completeness: REST API auth docs, web viewer docs, workspace validation
- Renamed `FLA_API_TOKEN` to `FLANES_API_TOKEN` across server, CLI, and docs
- Ruff lint errors (E501, F401, F841) and format issues

## [0.4.2] - 2026-02-10

### Added
- Workspace name hardening: strict regex validation prevents path traversal
- Server security: non-loopback binding requires auth token or `--insecure`
- Crash consistency: dirty markers, atomic metadata writes
- Reliability documentation (`docs/reliability.md`)
- README rewrite with architecture diagram and real-world usage

### Fixed
- `_pid_alive` crash on Windows (SystemError wrapping WinError 87)
- Sensible `.flanesignore` defaults shipped on init
- Promote mode preservation and `/health` version endpoint

## [0.4.1] - 2026-02-10

### Added
- `atexit` handler for automatic resource cleanup
- Idempotent `close()` on Repository and ContentStore
- Stale dirty marker detection and auto-cleanup

### Fixed
- Version sync between `__init__.py`, `setup.py`, and `pyproject.toml`
- Ruff E501 lint errors in CI

## [0.4.0] - 2026-02-09

### Changed
- Renamed package from `fla` to `flanes`

### Added
- Web dashboard with navigation and token aggregation
- `update_transition_cost` API and cost parameter for promote
- CI workflow updated for new package name

## [0.3.1] - 2026-02-07

### Fixed
- Updated all URLs from `glimish/fla` to `glimish/flanes`
- Badge and clone URLs corrected
- Author metadata added to package

## [0.3.0] - 2026-02-05

### Added
- Thread safety: WAL mode, busy timeout, `check_same_thread=False`
- Git bridge: `export-git` / `import-git` commands
- Git-style main workspace: repo root IS the main workspace
- Thread safety documentation in user guide

### Fixed
- 7 audit issues for correctness and security
- Windows CI test failures (8.3 short path names, git committer identity)

[Unreleased]: https://github.com/glimish/flanes/compare/v0.4.4...HEAD
[0.4.4]: https://github.com/glimish/flanes/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/glimish/flanes/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/glimish/flanes/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/glimish/flanes/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/glimish/flanes/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/glimish/flanes/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/glimish/flanes/releases/tag/v0.3.0
