---
name: 🚨 Trading incident
about: Live money impacted — bad fill, missed exit, runaway daemon, broker/API outage, risk-limit breach
title: "incident: "
labels: incident, priority:high
---

> If money is actively at risk RIGHT NOW, act first (halt the daemon / kill-switch),
> then file this. See the incident & kill-switch runbook in `docs/runbooks/`.

## Summary
<!-- One line: what broke, what it cost / risked. -->

## Timeline (IST)
<!-- When it started, when noticed, when contained. -->

## What was affected
- Strategy / runner:
- Positions / instruments:
- Estimated P&L impact:

## Immediate action taken
<!-- Halted? Positions squared? Daemon stopped? -->

## Root cause (if known)

## Follow-ups
- [ ] Root cause fixed / PR linked
- [ ] Guardrail added so it can't recur
- [ ] Lesson recorded in `tasks/lessons.md`
- [ ] Credentials rotated (if any exposure)
