# Data Protection Impact Assessment (draft)

Status: **draft for legal/DPO review**. Not a legal opinion.

## 1. Processing described
Floorwatch assigns tasks to venue staff and uses zone-level camera measurements (presence and movement counts with
timestamps) to judge whether an area is covered or a task is progressing. No video or images are stored and no facial
recognition is used. See `DATA_MAP.md`.

## 2. Necessity and proportionality
* Purpose: keep areas covered and tasks completed. Alternatives (manual checks) are slower and less consistent.
* Minimisation: measurements are tied to areas, not individuals; names appear only on task records.
* Employees are told via a versioned privacy notice acknowledged in the app.

## 3. Risks and mitigations

| Risk | Likelihood | Severity | Mitigation | Residual |
|---|---|---|---|---|
| A security alert is read as an accusation against a staff member (it cannot identify anyone, and detections can be wrong) | Medium | High | Alerts say "possible", carry no identity, ask a person to check the cameras first, need a human to close them with an outcome and note, and are audited; no automatic action is ever taken; *Authorised person* / *False alarm* outcomes exist; staff are told in the privacy notice | Low-Medium: needs a written rule that an alert alone is never grounds for discipline |
| Security alerts used to track individual staff movements after hours | Low | High | Presence readings are zone-level, anonymous and not stored; only alerts (zone, time, response) are kept, for the short event-history period; no per-person tracking exists | Low |
| Used for individual performance surveillance | Medium | High | Notice states purpose; objection / human-review request route; access limited by role | Medium: needs written management policy |
| Unauthorised access to staff data | Low | High | MFA, scrypt, rate limits, role checks, least-privilege DB role, encryption of MFA secrets | Low |
| Over-retention | Medium | Medium | Automatic retention loop | Low |
| Automated decision harms an employee | Low | Medium | Flags prompt supervisor follow-up, no automatic sanction; right to human review | Low |
| Vendor/cross-border exposure | Medium | Medium | DPAs, truncated IPs, no secrets to vendors | Medium until DPAs signed |

## 4. Consultation and sign-off
* Employee/works-council consultation where required: **to do**.
* DPO / legal review: **to do**. Reviewer, date, outcome:
