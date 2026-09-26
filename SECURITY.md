# Security Policy

## Supported versions

Security fixes are applied to the latest tagged release. Older pre-1.0 releases may be asked to
upgrade before a fix is evaluated.

| Version | Supported |
|---------|-----------|
| 0.9.x | Yes |
| 0.8.x and earlier | No |

## Reporting a vulnerability

Please do not open a public issue containing exploit details, private Zotero data, local paths,
tokens, or configuration contents.

Use GitHub's private **Report a vulnerability** flow on the repository's Security tab when it is
available. If private reporting is unavailable, open a minimal issue asking the maintainer to
establish a private contact channel; do not include vulnerability details in that issue.

Particularly relevant reports include unintended writes to Zotero data, path traversal or output
escape, secrets or absolute paths exposed through MCP responses, unsafe handling of untrusted paper
text, and ways to invoke opt-in mutation capabilities without their documented gates.

Include the affected version, operating system, impact, minimal reproduction, and any suggested
mitigation. Redact or synthesize all research-library data.
