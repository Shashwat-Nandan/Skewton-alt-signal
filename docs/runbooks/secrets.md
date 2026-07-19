# Runbook: secrets ownership & rotation

Companion to [`SECURITY.md`](../../SECURITY.md). This records **who holds what** and
**how we rotate**. Keep it current.

## What secrets exist
| Secret | Used by | Lives where (never git) |
|---|---|---|
| Kite `api_key` / `api_secret` | broker auth (`core/kite_auth.py`) | `config.ini` (local + VPS, `chmod 600`) |
| Kite `totp_key` (TOTP seed) | automated login | `config.ini` / `.env` |
| Kite `user_id` / `password` | login | `config.ini` |
| `DASHBOARD_URL`, `KITE_REDIRECT_URL` | OAuth flow | `.env` |
| Telegram / notify tokens (if used) | `notify-failure.sh`, `pair-eod-telegram.sh` | `.env` / systemd env |

## Ownership
<!-- TODO: fill in. Who is the custodian of the broker credentials and the VPS? -->
- Broker credentials custodian: ____
- VPS root / deploy keys: ____
- Rotation cadence: ____ (recommend at least every 90 days and on any suspected exposure)

## Rotate a credential
1. Generate/reset the new value in the source system (Kite developer console for
   API key/secret; reset password/TOTP as applicable).
2. Update `config.ini` / `.env` **locally** and on the **VPS** (`chmod 600`).
3. Restart affected units (see [`incident-and-killswitch.md`](./incident-and-killswitch.md)).
4. Verify a paper session authenticates cleanly before re-enabling live.
5. Revoke the old value. Record the rotation date above.

## If a secret was committed to git
Follow the emergency steps in `SECURITY.md` (rotate first, then purge history).
Assume the old value is compromised the moment it touched a commit.
