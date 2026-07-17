# Security Policy

This repository powers a live financial-trading and advisory platform. Security
issues here can move real money and expose customer or broker credentials. Treat
them with corresponding seriousness.

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Email **security@skewton.in** (or support@skewton.in) with:
- a description of the issue and its impact,
- steps to reproduce, and
- any relevant logs (with secrets redacted).

We aim to acknowledge within **2 business days** and to agree a remediation
timeline with you. Please give us reasonable time to fix before any disclosure.

## Secrets: the hard rules

- Broker and app credentials — `config.ini`, `config_banknifty.ini`, `.env`, and
  the Kite `api_key` / `api_secret` / `totp_key` / `user_id` / `password` — **must
  never** be committed. They are gitignored; keep it that way.
- Only `config_template.ini` (secret-free) belongs in git.
- On the VPS, production secrets live in `config.ini` with `chmod 600`, owned by
  the service user — never in git, never in logs.

### If a secret is ever committed (even in history)

1. **Rotate immediately** — revoke and reissue the exposed Kite API key/secret,
   TOTP seed, and any affected passwords. Assume the old value is compromised.
2. Purge it from history (`git filter-repo` or BFG) and force-update — coordinate
   with the team first (see the rename/deploy runbook for a quiet-window process).
3. Record the incident and remediation in `tasks/lessons.md` and, if
   customer-facing, follow the incident runbook in `docs/runbooks/`.

## Data & privacy

The platform and its marketing site collect personal data (e.g. subscriber
emails), which is subject to India's DPDP Act. Access to subscriber/PII data and
admin endpoints is restricted and token-gated; do not expose or export it outside
approved channels.

## Automated scanning

Every PR runs a `security` workflow: `gitleaks` (secret scanning across history),
`pip-audit` (Python dependency CVEs), `npm audit` (frontend), and a commit
signature check. Dependabot watches `npm` and GitHub Actions; Python CVEs are
surfaced by `pip-audit` and Dependabot security alerts.
