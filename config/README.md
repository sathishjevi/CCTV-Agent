# Floorwatch deployment configuration

Two templates, deliberately split by sensitivity:

- **`deployment.env.template`** — operational settings (timers, thresholds, which channel/provider to use, retention window). No secrets. Safe to commit as a template and safe to read for anyone operating the system.
- **`secrets.env.template`** — third-party API keys, tokens, and connection strings. Never commit the filled-in version.

## Setup

```bash
cp config/deployment.env.template config/deployment.env
cp config/secrets.env.template config/secrets.env
# edit both — deployment.env is safe to eyeball with anyone on the ops team,
# secrets.env should only be handled by whoever owns credentials for this deployment
```

Both `config/deployment.env` and `config/secrets.env` (everything except the two `.template` files) are `.gitignore`'d. Run `python tools/check_no_secrets.py` before your first commit to double-check nothing secret-shaped got tracked anyway.

## Precedence

Real environment variables (set by your process manager, `systemd` `EnvironmentFile=`, a Docker secret, a real secrets manager, etc.) **always win** over anything in these files — both services load `deployment.env` then `secrets.env` with `override=False`, so this is safe to use for local/dev convenience without it fighting a real production environment variable if one's already set.

## Why `AUTH_SECRET` is handled separately

`FLOORWATCH_AUTH_SECRET` signs every login session token across both services. Whoever has it can mint a valid supervisor login for the entire system — it's a different category of sensitive from "this deployment's Twilio account," and bundling it into the same file as vendor API keys makes it easy to treat with the same (lower) level of care as everything else in that file.

It's intentionally **not** in `secrets.env.template`. Leave it unset and it self-generates once, persisted to `services/.floorwatch_auth_secret` (already `.gitignore`'d, already outside this `config/` directory). For a real deployment, prefer setting `FLOORWATCH_AUTH_SECRET` directly via your process manager or secrets store — not through a file that lives next to less-sensitive config.

## File permissions

Both services log a warning at startup if `secrets.env` (or wherever your secrets actually live) looks broadly readable. On Windows, tighten this with `icacls`:

```powershell
icacls "config\secrets.env" /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

On Linux/macOS: `chmod 600 config/secrets.env`.

## What this doesn't solve

This is a small, incremental improvement over "one plaintext file with everything in it" — it is not a substitute for a real secrets manager (Windows Credential Manager, HashiCorp Vault, AWS/Azure/GCP secret stores). For a real client deployment handling real employee data and real API spend, moving actual runtime secrets into one of those — with these files only as local-dev/template convenience — is the more durable answer. Treat this setup as "meaningfully better than the naive approach," not as final.

## `FLOORWATCH_CONSENT_CONFIRMED`

This flag in `deployment.env.template` is an engineering discipline gate, not a legal control. Setting it to `true` doesn't verify that employee notice/consent was actually obtained — it's just a place for that fact to be recorded so a deployment can't silently skip thinking about it. Never present this flag to legal/auditors as evidence consent was obtained; the real record of that lives wherever your organization actually documents legal sign-off.
