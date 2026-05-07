# Security review — deferred follow-ups

The 2026-05-05 security review (one Critical, eight Highs, ~nine Mediums) is
closed for the items below; this file captures what was deliberately deferred.
Pick up when the audit cycle restarts.

What's already shipped is in `tasks/todo.md` (the "Dashboard auth" section)
and the commits between `38f4b3d` (pre-review) and `638d94f` (last batch).

---

## Deferred items, in rough priority order

### 1. Run systemd units as a non-root `taleb` user (Medium)

Currently every taleb-* unit and `dashboard-backend.service` run as
`User=root`. Hardening directives (`NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectKernelTunables`, `PrivateTmp`, `UMask=0027`) are in place but a
Python-level RCE in any dependency is still root.

**Approach:**
- Create a system user: `useradd --system --home-dir /var/lib/taleb --shell /usr/sbin/nologin taleb`
- `chown -R taleb:taleb /root/algo-trading/taleb-karpathy-kite/{logs,data_cache}`
  and the secret-bearing files (`.env`, `.kite_session.json`, `config.ini`).
- Edit each unit:
  - `User=taleb`, `Group=taleb`
  - Drop the umbrella project-root entry from `ReadWritePaths`; keep only
    `logs/`, `data_cache/`, and `best_params.json` (autoresearch writes that).
  - Add `ProtectHome=read-only`, `LockPersonality=true`, `MemoryDenyWriteExecute=true`,
    `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `SystemCallFilter=@system-service`.
- `daemon-reload` and restart every unit; verify each one auths and ticks.
- Watch journal for permission-denied stragglers (a stray `~/.something`,
  `tempfile.mkstemp()` outside PrivateTmp, etc.).

**Risk if delayed:** unchanged — RCE on any router/SDK upgrade lands as root.

### 2. Pin Python dependencies with hashes (Medium) — **CLOSED 2026-05-07**

Shipped: `requirements.in` + `requirements.lock` (hashed) and the
matching `-dev` pair, both generated with `uv pip compile`. Drift gated
two ways: `.github/workflows/lockfile.yml` on PR and
`deploy/check_lockfile.sh` from `redeploy.sh` for out-of-band deploys.
Stale 3.8 `.pyc` files removed. Reproducing-the-venv docs in root
`README.md`. See `tasks/todo.md` "Pin Python deps with hashes" for the
full review.

### 3. Per-tick max-lots cap on rehedge proposals (Medium)

`validate_order` (HIGH #5) caps absolute order size at 10000 lots. That
catches obvious fat-finger / NaN errors but doesn't prevent a stale-quote
incident from sizing a hedge at, say, 50 lots when 5 was correct.

The audit specifically called out `_apply_risk_filters` running only on
entry/scan, not on `check_and_rehedge`. The mitigation is partial: stale
quote → `current_price` falls back to `entry_price` (so unrealized PnL is
0) → rehedge sizing uses fresh greeks at next tick. Still, an adversarial
sequence of quotes could drive a single rehedge to size up.

**Approach:**
- Add a strategy-config knob `max_rehedge_lots_per_tick` (default ~5x
  expected typical hedge size, e.g. 20). Apply in `check_and_rehedge` to
  every proposal before it leaves the function.
- Optional: track total notional deployed in last 5 minutes; refuse if
  it exceeds e.g. 2x position notional.

**Risk if delayed:** moderate — `validate_order` catches the worst cases;
this tightens the middle ground.

### 4. bhavcopy/screen_pairs row cap (Low)

Audit recommended `nrows=200000` on `pd.read_csv`. Files already use
`usecols` whitelist + a `required` column-set assertion; row cap adds
defence-in-depth but risks silent data loss on busy days (busy F&O days
can push 100k+ rows).

**Approach if pursued:**
- Cap raw download size at the network layer (`fetch_bhavcopy.py`):
  reject any `csv_bytes` exceeding e.g. 50MB. Fail loud, not silent.
- Don't cap `nrows` — silent truncation is worse than the threat model
  warrants.

**Risk if delayed:** low. The threat is a malformed bhavcopy CSV
exhausting memory on the daily timer. Hasn't happened in a year.

---

## Lows not addressed

These were in the original review and remain open. Each is genuinely low-impact
given the current trust boundary (single operator, dashboard now password-gated):

- **`npm ci` runs install scripts as root in `redeploy.sh`** — switch to
  `npm ci --ignore-scripts`, or build the SPA on a workstation/CI and
  ship `dist/` only.
- **TOTP secret stored next to password in `.env`** — unavoidable for
  unattended automation; document the trust model in `backend/README.md`.
- **`pair_candidates.csv` parser unbounded** — internal-only data source;
  defence-in-depth only.

## Out of scope from the start

- Per-user accounts / RBAC. Single-operator dashboard.
- 2FA on the dashboard login. Adds operator friction; password + cookie
  + nginx rate-limit is sufficient for the threat model.
- IP allowlisting. Operator's IP isn't stable; would create more outages
  than attacks prevented.

---

## How to resume

1. Read `tasks/todo.md` "Dashboard auth" section for what's already shipped.
2. Run `git log 38f4b3d..HEAD --oneline -- :^tasks/` to see the security
   commits in chronological order.
3. Pick an item above; the "Approach" section is the starting plan.
4. Run `pytest tests/ --tb=short` to confirm a clean baseline before
   touching anything.
