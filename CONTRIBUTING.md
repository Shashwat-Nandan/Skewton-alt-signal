# Contributing to the Skewton Platform

This repo runs real money. Read [`AGENTS.md`](./AGENTS.md) first — the
non-negotiable safety rules there apply to every contributor and every AI agent.
This document covers the mechanics: setup, branching, signing, and the PR flow.

## 1. One-time setup

```bash
# Python 3.11, hash-pinned dependencies
python3.11 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock -r requirements-dev.lock

# Secrets live locally, never in git
cp config_template.ini config.ini && chmod 600 config.ini
$EDITOR config.ini   # api_key, api_secret, totp_key, user_id, password
$EDITOR .env         # KITE_* vars, DASHBOARD_URL, etc.

# Install the pre-commit hooks (lint + secret scan on every commit)
.venv/bin/pip install pre-commit && pre-commit install
```

### Set up commit signing (required)

Signed commits give us a tamper-evident audit trail. Set it up once:

```bash
# SSH signing (simplest if you already push over SSH)
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
```

Then add the **same** key to GitHub as a *Signing key*
(Settings → SSH and GPG keys → New SSH key → key type "Signing key"). Prefer GPG?
See GitHub's "Managing commit signature verification" docs. CI checks that every
commit on a PR is verified-signed.

## 2. Branch & commit conventions

- Branch names: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`, `docs/<slug>`,
  `research/<slug>`.
- Commit messages: [Conventional Commits](https://www.conventionalcommits.org/)
  — e.g. `fix(proposer): delta strike-picker no longer bails on T=0`. This matches
  the existing history and keeps `git log` scannable.
- Keep commits signed and focused (Rule 3: surgical changes).

## 3. Before you open a PR

Run the same gates CI will run:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest tests/ -q -rs        # ≤ 8 known data_cache skips allowed
pre-commit run --all-files                       # secret scan + checks (no reformatting)
cd frontend && npm ci && npm run build           # only if you touched frontend/
```

If you changed dependencies, regenerate the lockfiles (never hand-edit `.lock`):

```bash
uv pip compile requirements.in --generate-hashes --output-file requirements.lock --python-version 3.11
uv pip compile requirements-dev.in --generate-hashes --output-file requirements-dev.lock \
  --python-version 3.11 --constraint requirements.lock
```

## 4. Pull request flow

1. Push your branch and open a PR into `main`. Fill in the PR template, including
   the **trading-safety checklist**.
2. CI must be green: `ci` (ruff + pytest + frontend build), `lockfile` (if you
   touched deps), and `security` (secret scan, deps audit, signature check).
3. A [CODEOWNER](./CODEOWNERS) reviews — **required** for money-affecting paths
   (`strategies/`, `signal_plane/`, `loop_engine/`, `backend/`, `core/risk_analyzer.py`,
   the runners, `deploy/`). No self-merge of those paths.
4. Squash-merge with a Conventional-Commit title. Keep `main` linear.

> **Paper before live.** A new or changed strategy must pass backtest **and** paper
> before it is ever considered for live trading. This is a hard gate — see
> `AGENTS.md`.

## 5. Where things live

See the repo map in [`AGENTS.md`](./AGENTS.md#how-to-work-in-this-repo). Track your
work in `tasks/todo.md` and capture corrections in `tasks/lessons.md`.

## Reporting a security issue

Do **not** open a public issue. See [`SECURITY.md`](./SECURITY.md).
