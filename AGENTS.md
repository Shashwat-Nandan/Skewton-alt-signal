# AGENTS.md — Skewton Signal Engine (`skewton-signal`)

**This file is the single source of truth for how anyone — human or AI agent —
works in this repository.** It follows the tool-agnostic [AGENTS.md](https://agents.md)
convention so Claude Code, Cursor, Copilot, Codex, and every other assistant read
the same rules. `CLAUDE.md` is a symlink to this file.

Skewton runs **real money** in Indian equity-derivatives markets. Treat every
change as if a bug could place, mis-size, or fail to exit a live trade — because
it can. When in doubt, stop and ask a human. The safety rules below are **not**
style preferences; they are the reason we can run unattended.

---

## 🛑 Non-negotiable safety rules (read first)

1. **Never enable or commit live trading in the dashboard or in CI.** Live trading
   lives **only** on the headless, audited path (`runners/run_paper*.py` /
   `runners/run_equity_swing.py` under systemd timers). `backend/main.py` + `frontend/`
   **never** place live orders — that is by design so the audit trail is the daily
   log file, not a browser session. Do not "wire up" live trading anywhere else.
2. **Never commit, print, or log secrets.** `config.ini`, `config_banknifty.ini`,
   `.env`, and the Kite `api_key` / `api_secret` / `totp_key` / `user_id` /
   `password` are radioactive. They are gitignored — keep it that way. Use
   `config_template.ini` (secret-free) for anything checked in. If you ever see a
   secret in the diff, stop and remove it before committing.
3. **Respect the paper → live gate.** A new or changed strategy must pass
   **backtest _and_ paper trading** before anyone even considers live. Agents may
   not shortcut, skip, or fake this progression.
4. **Never weaken a safety guard to "make it run."** The market-hours gate
   (09:15–15:30 IST), the `--force` requirement, position/exposure limits, throttle
   (`core/broker_throttle.py`), and any kill-switch exist on purpose. If a guard blocks
   you, the guard is working — do not delete or bypass it; ask.
5. **Money-affecting changes require human review** via `CODEOWNERS` — no
   self-merge. This covers `strategies/`, `signal_plane/`, `loop_engine/`,
   order/execution paths in `backend/`, `core/risk_analyzer.py`, `core/greeks_engine.py`,
   `runners/run_paper*.py`, `core/runner_common.py`, and `deploy/`.
6. **Dependencies change only through the lockfile flow.** Edit `requirements.in` /
   `requirements-dev.in`, then regenerate the `.lock` files with hashes (see
   commands below). **Never hand-edit a `.lock`.** CI enforces drift and installs
   with `--require-hashes`.
7. **Signed commits are required.** See `CONTRIBUTING.md` for one-time SSH/GPG
   signing setup. Unsigned commits are rejected by the signature-verification CI
   job (and will be hard-blocked once the org is on a paid plan).
8. **Fail loud** (this is Rule 12 below, restated because it matters most here). A
   partial fill, a skipped record, a swallowed retry, a test you skipped — surface
   it explicitly. "It worked" must mean it actually, verifiably worked.

---

## How to work in this repo

**Setup (Python 3.11, hash-pinned):**
```bash
python3.11 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock -r requirements-dev.lock
cp config_template.ini config.ini && chmod 600 config.ini   # then fill secrets locally
```

**Everyday commands:**
```bash
.venv/bin/python -m ruff check .            # lint gate (config in ruff.toml; ruff-format is intentionally NOT used)
.venv/bin/python -m pytest tests/ -q -rs    # tests; ≤ 8 known data_cache skips are allowed, more fails CI
cd frontend && npm ci && npm run build      # frontend: tsc -b && vite build
```

**Run the two systems:**
```bash
# Dashboard (never trades live): backend + SPA
.venv/bin/uvicorn backend.main:app --reload --port 8000
cd frontend && npm run dev                  # SPA on :5173

# Headless paper/live runner (refuses outside 09:15–15:30 IST unless --force)
.venv/bin/python -m runners.run_paper --force
```

**Change dependencies:**
```bash
# edit requirements.in / requirements-dev.in, then:
uv pip compile requirements.in --generate-hashes --output-file requirements.lock --python-version 3.11
uv pip compile requirements-dev.in --generate-hashes --output-file requirements-dev.lock \
  --python-version 3.11 --constraint requirements.lock
```

**Repo map:**
- `backend/` — FastAPI dashboard API (read-only w.r.t. live trading).
- `frontend/` — React/Vite/TS dashboard SPA (`taleb-karpathy-dashboard`).
- `strategies/`, `signal_plane/`, `loop_engine/` — the trading brain (money-affecting).
- `runners/` — headless daemons/entry points (paper, pairs, arbitrage, equity swing, autoresearch). Money-affecting.
- `core/` — shared engines & broker I/O: `broker` (adapter factory: kotak default, then zerodha/groww/dhan), `greeks_engine`, `risk_analyzer`, `market_profile`, `regime_classifier`, `runner_common`, `screen_pairs`, `data_cache_io`, `kite_auth`, `broker_throttle`.
- `market_data/` — market-data acquisition (`fetch_*`, `tick_capture`, `tape_to_parquet`) plus `holidays.csv`.
- `research/` — backtests, parameter sweeps, optimizers and validators. Never on a live path.
- `scripts/` — operational one-shots (scoreboard, decay ledger, verify, feature logging, reports).
- `tests/` — pytest suite (~74 files). `docs/` — architecture, data-pipeline, strategy & research notes. `deploy/` — VPS (systemd + nginx). `state/` — on-disk runtime state (gitignored bits). `.claude/skills/` — repo-specific agent skills.

**Invocation:** every entry point under `core/`, `market_data/`, `research/`
and `runners/` runs as a module from the repo root —
`.venv/bin/python -m runners.run_paper`, `-m research.backtest_pairs`,
`-m core.screen_pairs`. Calling them by file path
(`python runners/run_paper.py`) **fails**: that puts `runners/` on `sys.path`
instead of the repo root, so the cross-package imports don't resolve. There is
deliberately no `sys.path` bootstrap left in those packages to paper over it —
one existed, sat *after* the imports it was supposed to enable, and made the
breakage look intermittent (Rule 7: one convention, not two). systemd units
use `-m` and set `WorkingDirectory` to the repo root.

`scripts/` is the exception: those one-shots keep an explicit repo-root
bootstrap so `python scripts/strategy_scoreboard.py` still works, and they may
also be run with `-m scripts.<name>`. Inside `scripts/`, always import siblings
through the package (`from scripts import strategy_decay`) — a bare
`import strategy_decay` creates a *second*, separate module object, which
splits the decay ledger's state (reorg 2026-07-19).

**The two-systems model:** headless daemons and the dashboard **share the same
strategy code and on-disk caches**. That coupling is why the backend, strategies,
and daemons live in one repo — do not try to "split the backend out." Before
editing shared code, read its callers on *both* paths (Rule 8).

---

## The engineering rules

> The original 4 rules (from Karpathy via Forrest Chang) close ~40% of the failure
> modes seen in unsupervised agent sessions. The 8 added below cover the remaining
> ~60%. Each added rule comes from a specific failure the original 4 did not
> prevent; the "moment" note records the incident so the rule keeps its provenance.

### Rule 1 — Think Before Coding
No silent assumptions. State what you're assuming. Surface tradeoffs. Ask before
guessing. Push back when a simpler approach exists.

### Rule 2 — Simplicity First
Minimum code that solves the problem. No speculative features. No abstractions for
single-use code. If a senior engineer would call it overcomplicated — simplify.

### Rule 3 — Surgical Changes
Touch only what you must. Don't "improve" adjacent code, comments, or formatting.
Don't refactor what isn't broken. Match existing style.

### Rule 4 — Goal-Driven Execution
Define success criteria. Loop until verified. Don't tell the agent what steps to
follow, tell it what success looks like and let it iterate.

### Rule 5 — Use the model only for judgment calls
Use the model for: classification, drafting, summarization, extraction from
unstructured text. Do NOT use it for: routing, retries, status-code handling,
deterministic transforms. If a status code already answers the question, plain
code answers the question.

*Moment:* Code that called the model to "decide if we should retry on 503" worked
beautifully for two weeks, then started flaking because the model started reading
the request body as context for the decision. The retry policy was random because
the prompt was random.

### Rule 6 — Token budgets are not advisory
Per-task budget: 4,000 tokens. Per-session budget: 30,000 tokens. If a task is
approaching budget, summarize and start fresh. Do not push through. Surfacing the
breach > silently overrunning.

*Moment:* A debugging session ran for 90 minutes. The model was perfectly happy
iterating on the same 8KB error message, gradually losing track of which fix it
had already tried. By the end, it was suggesting fixes rejected 40 messages
earlier. Token budget would have killed it at minute 12.

### Rule 7 — Surface conflicts, don't average them
If two existing patterns in the codebase contradict, don't blend them. Pick one
(the more recent / more tested), explain why, and flag the other for cleanup.
"Average" code that satisfies both rules is the worst code.

*Moment:* A codebase had two error-handling patterns — one async/await with
explicit try/catch, one with a global error boundary. New code did both. Doubled
error handlers. Took 30 minutes to figure out why errors were swallowed twice.

### Rule 8 — Read before you write
Before adding code in a file, read the file's exports, the immediate caller, and
any obvious shared utilities. If you don't understand why existing code is
structured the way it is, ask before adding to it. "Looks orthogonal to me" is the
most dangerous phrase in this codebase.

*Moment:* A function was added next to an existing identical function that hadn't
been read. Both did the same thing. The new one took precedence because of import
order. The old one had been the source of truth for 6 months.

### Rule 9 — Tests verify intent, not just behavior
Every test must encode WHY the behavior matters, not just WHAT it does. A test like
`expect(getUserName()).toBe('John')` is worthless if the function takes a hardcoded
ID. If you can't write a test that would fail when business logic changes, the
function is wrong.

*Moment:* 12 tests were written for an auth function. All passed. Auth was broken
in production. The tests checked the function returned something, not whether it
returned the right thing. It passed because it was returning a constant.

### Rule 10 — Checkpoint after every significant step
After completing each step in a multi-step task: summarize what was done, what's
verified, what's left. Don't continue from a state you can't describe back.
If you lose track, stop and restate.

*Moment:* A 6-step refactor went wrong on step 4. By the time it was noticed, steps
5 and 6 had been done on top of the broken state. Untangling took longer than
redoing the whole thing. Checkpoints would have caught it at step 4.

### Rule 11 — Match the codebase's conventions, even if you disagree
If the codebase uses snake_case and you'd prefer camelCase: snake_case. If it uses
class-based components and you'd prefer hooks: class-based. Disagreement is a
separate conversation. Inside the codebase, conformance > taste. If you genuinely
think the convention is harmful, surface it. Don't fork it silently.

*Moment:* React hooks were introduced into a class-component codebase. They worked.
They also broke the codebase's testing patterns, which assumed componentDidMount.
Half a day to remove and rewrite.

### Rule 12 — Fail loud
If you can't be sure something worked, say so explicitly. "Migration completed" is
wrong if 30 records were skipped silently. "Tests pass" is wrong if you skipped
any. "Feature works" is wrong if you didn't verify the edge case that was asked
about. Default to surfacing uncertainty, not hiding it.

*Moment:* A database migration was reported "completed successfully." It had
silently skipped 14% of records that hit a constraint violation. The skip was
logged but not surfaced. The problem was discovered 11 days later when reports
started looking wrong.

---

## Task management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items.
2. **Verify Plan**: Check in before starting implementation.
3. **Track Progress**: Mark items complete as you go.
4. **Explain Changes**: High-level summary at each step.
5. **Document Results**: Add a review section to `tasks/todo.md`.
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections.

---

## Definition of done

A change is done only when **all** of the following are true:
- `ruff check .` is clean and `pytest tests/ -q` is green with **no new skips**
  beyond the known 8 data_cache skips (Rule 12).
- The frontend builds (`npm run build`) if you touched `frontend/`.
- `pre-commit` ran and `gitleaks` is clean — **no secret introduced** (safety rule 2).
- Money-affecting paths were reviewed by a `CODEOWNERS` owner (safety rule 5).
- Commits are signed (safety rule 7).
- Dependency changes went through `requirements.in` → regenerated `.lock` (safety rule 6).
- Relevant `docs/` and `tasks/todo.md` are updated.
