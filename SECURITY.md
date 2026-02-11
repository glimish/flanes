# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.4.x   | Yes       |
| < 0.4   | No        |

## Reporting a Vulnerability

If you discover a security vulnerability in Flanes, please report it
responsibly:

1. **Do not** open a public GitHub issue for security vulnerabilities.
2. Email security concerns to the maintainer via the contact information
   in the project's GitHub profile.
3. Include a description of the vulnerability, steps to reproduce, and
   any potential impact.

You should receive an acknowledgment within 72 hours. We will work with
you to understand the issue and coordinate a fix before any public
disclosure.

## Security Considerations

Flanes is designed primarily as a **local development tool**. When
deploying the REST API server:

- Bind to `127.0.0.1` (default) for local-only access.
- Set `FLANES_API_TOKEN` or use `--token` when binding to non-loopback
  addresses.
- The `--insecure` flag explicitly acknowledges the risk of serving
  without authentication on a network interface.
- Request body size is capped at 10 MB to prevent memory exhaustion.

## Scope

The following are **in scope** for security reports:

- Authentication bypass in the REST API
- Path traversal in workspace or static file serving
- Denial of service through resource exhaustion
- Data corruption through concurrent access

The following are **out of scope**:

- Attacks requiring local filesystem access (Flanes trusts the local
  user)
- Social engineering
