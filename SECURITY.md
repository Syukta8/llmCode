# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| `main`  | :white_check_mark: |

---

## Reporting a Vulnerability

If you discover a security vulnerability in `llmCode`, please report it responsibly:

1. **Do not create a public GitHub issue** for undisclosed vulnerabilities.
2. Submit a report through **GitHub Security Advisories** on the repository:
   - Navigate to the **Security** tab of `https://github.com/Syukta8/llmCode`.
   - Click **Report a vulnerability**.
   - Alternatively, contact the maintainer directly via GitHub profile: [Syukta8](https://github.com/Syukta8).

### Information to Include
- Detailed description of the vulnerability.
- Steps to reproduce or proof-of-concept (PoC).
- Potential impact and affected configurations (e.g. host binding, backends, endpoints).

### Response Timeline
- Maintainers will acknowledge reports within **48 hours**.
- We will coordinate a remediation patch and release a security advisory once validated.

---

## Deployment Security Best Practices

- **Host Binding**: By default, `llmCode` dashboard and engine backends bind strictly to `127.0.0.1` (localhost). Do not bind to `0.0.0.0` or expose port `7860` / `8080` to public networks without a secure reverse proxy and authentication layer.
- **API Keys**: Dynamic session keys are auto-generated and stored with restricted permissions in `config/api_key` and `config/agent.env`. Ensure your `config/` directory remains ignored and private.
