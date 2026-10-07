# Personal data breach notification playbook

Use with `INCIDENT_RESPONSE_RUNBOOK.md` (technical containment).

## Clock
Start the clock when you become **aware** a breach of personal data is likely.

| Regime | Who to tell | Deadline |
|---|---|---|
| GDPR / UK GDPR | Supervisory authority (unless unlikely to risk individuals); affected people if high risk | Authority: 72 hours. Individuals: without undue delay |
| US states | State AG / affected residents (varies) | Commonly 30-60 days; some shorter |
| Other | Check local law and contracts | Varies |

Confirm which apply with counsel; this table is a prompt, not advice.

## Steps
1. **Contain**: revoke tokens, rotate secrets (JWT secret, DB password, encryption key), block the vector.
2. **Assess**: what data, how many people, was it encrypted, was it accessed or just exposed.
3. **Decide** on notification with the DPO/counsel; record the reasoning even if you decide not to notify.
4. **Notify** authority and individuals with: what happened, data involved, likely consequences, measures taken, contact point.
5. **Record** in the breach register, **review** root cause, add a test or control.

## Breach register template

| ID | Date aware | Date occurred | Description | Data / people affected | Risk | Notified (who/when) | Actions | Closed |
|---|---|---|---|---|---|---|---|---|
