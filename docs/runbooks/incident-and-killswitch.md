# Runbook: trading incident & kill-switch

**Purpose:** the fastest authoritative path to *stop live money* and contain a
trading incident. This is an emergency quick-card — the authoritative,
fully-detailed procedures live in [`deploy/VPS_DEPLOYMENT.md`](../../deploy/VPS_DEPLOYMENT.md).
Act first, file a [`🚨 Trading incident`](../../.github/ISSUE_TEMPLATE/trading_incident.md)
issue second.

## 🔴 Stop live trading NOW (on the VPS)

Live trading runs **only** on the headless path. To halt it:

```bash
# 1. Stop the LIVE runner from re-arming, and kill any in-flight run:
sudo systemctl disable --now pair-paper-persistent-live.timer
sudo systemctl stop        pair-paper-persistent-live.service

# 2. If the options hedger is live, stop it too:
sudo systemctl stop        taleb-hedger.timer taleb-hedger.service

# 3. Confirm nothing live is active:
systemctl status pair-paper-persistent-live.service   # expect inactive/exited
```

> There is an existing safety net — `pair-live-watchdog` and
> `pair-live-halt-on-failure` (see `deploy/`). Understand what they've already done
> before taking manual action; don't fight the watchdog.

## Square / reconcile positions
Positions opened live are **not** unwound by stopping a unit. Reconcile through the
broker (Kite) per the position-management steps in `VPS_DEPLOYMENT.md`. Paper
runners hold no real positions — nothing to unwind.

## Stop everything (full halt)
To disarm all timers (paper + data + live) in one pass, use the disable pattern
documented in `VPS_DEPLOYMENT.md` (`systemctl disable --now <timer>` per unit; the
`deploy/sync-units.sh` list enumerates them).

## After containment
1. File the incident issue with the timeline (IST) and estimated P&L impact.
2. Root-cause it; add a guardrail so it cannot recur (a test — Rule 9 — or a gate).
3. Record the lesson in `tasks/lessons.md`.
4. If any credential was exposed, follow [`secrets.md`](./secrets.md) and rotate.

## Who to contact
<!-- TODO: fill in on-call / escalation contacts for the 3 founders. -->
