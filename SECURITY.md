# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.9.x   | Yes |
| < 0.9   | No |

## Reporting a Vulnerability

Please report security vulnerabilities to **security@sessionfs.dev**.

Do NOT open a public GitHub issue for security vulnerabilities.

We will acknowledge receipt within 48 hours and provide a fix timeline within 7 days.

## Security Measures

- **Dependency scanning**: Weekly pip-audit + Trivy scans via GitHub Actions
- **Container scanning**: All GHCR images scanned with Trivy on publish
- **Static analysis**: Bandit runs on every PR
- **Dependabot**: Auto-PRs for vulnerable dependencies (pip, npm, Docker, Actions)
- **Config security**: API keys encrypted with Fernet, config files chmod 600
- **No server-side LLM keys**: All LLM API calls are client-side (BYOK)
