# Security alerts — break-in, after-hours, off-limits areas, people hiding

Floorwatch started as a coverage and task system. Security alerts add a second job on the same cameras: tell a
supervisor when a person is detected somewhere, or at a time, they should not be.

**Read the limits at the bottom before relying on this.** It is an early-warning prompt to look at the cameras,
not an alarm system, and it cannot tell an intruder from a staff member.

## What it raises

| Alert | When it fires | Severity |
|---|---|---|
| **Possible break-in (door / window)** | A person is detected in a zone marked as a door or window entry point while the venue is *armed* (closed, past the closing grace period) | Critical |
| **Person here while closed** | A person is detected in a zone marked *Alert when closed*, while armed | High |
| **Person in an off-limits area** | A person is detected in a zone marked *Off-limits*, at any time, open or closed | High |
| **Someone still inside after closing** | A person was already in a monitored zone when the venue armed and is still there — possibly hiding, or locked in | High |

Anything raised while a break-in alert is still open is escalated to **critical** and says "After a possible
break-in", because the person at the stockroom is probably the person who came through the door.

Alerts are de-duplicated per zone and kind, repeated while the person is still on camera and nobody has responded
(every 10 minutes, up to 6 times), and **stay open until a human acknowledges or closes them**. When the person
leaves the alert says so but does not close itself.

## How it works

```
camera → detector → floorwatch-coverage ──zone_person_present/cleared──▶ Redis ──▶ rules engine
         (person boxes)   (which zone? fast, anonymous)                           │ security_engine.py
                                                                                  │  opening hours? zone rule?
                                                                                  ▼
                                                      dashboard feed + bell badge ── push/SMS to supervisors
```

The camera side only reports facts ("a person is in the Back Door zone"). The rules engine applies the policy, so you
change what counts as suspicious from the dashboard, never by recalibrating a camera.

## Setting it up

1. **Draw the zones.** In the camera calibration tool, draw a polygon for each door, window or restricted room and give
   it a `zone_id` (e.g. `back_door`) and `role_tag: security`. Place door/window polygons on the floor area just inside
   the opening: a person's *feet* (bottom-centre of their box) must fall inside the polygon.
2. **Add the same `zone_id` in Manage Zones** (web dashboard, or the app's Manage Zones) and set its **Security**
   settings: *Alert when closed* / *Off-limits*, and *Door* / *Window* for entry points. A zone with no rule never alerts.
3. **Set the opening hours** on the rules-engine service (Railway variables):
   `FLOORWATCH_OPEN_HOURS="mon-fri 09:00-23:00; sat 10:00-02:00; sun closed"` and `FLOORWATCH_TIMEZONE=Asia/Kolkata`
   (use your own). Without opening hours the venue is never "closed", so only *Off-limits* zones can alert.
4. **Check who gets told.** Active staff whose role is in `FLOORWATCH_SECURITY_ALERT_ROLES` (default
   `supervisor,secondary_admin,admin`) are messaged through their own channel — the app (push) if they log in
   there, otherwise SMS. System status (admin) shows "who is messaged" and warns when it is nobody.
5. **Start in shadow mode.** While `FLOORWATCH_SHADOW_MODE=true` alerts appear on the dashboard and in the app but nobody
   is texted or pushed. Run a few nights, tune, then set it to `false`.

Opening hours format: days are `mon`..`sun`, a range (`mon-fri`, may wrap: `fri-mon`), a list (`mon,wed`) or `daily`;
times are 24-hour `HH:MM`; a closing time at or before the opening time runs past midnight (`sat 10:00-02:00` is
Saturday 10:00 to Sunday 02:00); `24h` is open all day; a day not mentioned (or `closed`) is closed all day. A setting
that cannot be understood does **not** stop the service: it is logged as an error, shown red on System status, and the
venue is treated as never closed until it is fixed.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `FLOORWATCH_SECURITY_ALERTS_ENABLED` | `true` | Master switch. Nothing alerts until a zone is marked, so enabling it changes nothing by itself |
| `FLOORWATCH_OPEN_HOURS` | empty | Opening hours (above) |
| `FLOORWATCH_TIMEZONE` | `UTC` | The venue's timezone, e.g. `Asia/Kolkata`, `America/New_York` |
| `FLOORWATCH_SECURITY_CLOSING_GRACE_MINUTES` | `30` | Time after closing before it is armed (locking up, last customers) |
| `FLOORWATCH_SECURITY_OPENING_GRACE_MINUTES` | `30` | Time before opening when it stops being armed (staff arriving) |
| `FLOORWATCH_SECURITY_REPEAT_MINUTES` / `_MAX_REPEATS` | `10` / `6` | Reminder cadence while unresolved and the person is still detected |
| `FLOORWATCH_SECURITY_MAX_DISARM_MINUTES` | `720` | Longest a supervisor can disarm for in one go |
| `FLOORWATCH_SECURITY_AUTO_CLOSE_HOURS` | `24` | Open alerts nobody touched are closed (and recorded as expired) after this |
| `FLOORWATCH_SECURITY_ALERT_ROLES` | `supervisor,secondary_admin,admin` | Who is messaged |

On the camera side (`floorwatch-coverage`): `--presence-arrive-seconds` (3), `--presence-clear-seconds` (30),
`--presence-heartbeat-seconds` (30), `--presence-confidence` (0.6), `--no-presence-events`.

## Day to day

* **Dashboard:** *Security alerts* button (red, with a count, when something is open; it also beeps for a new alert
  once the page has been clicked). **App:** the bell in the top bar of the supervisor screens.
* **I'm on it** stops the reminders and shows who is handling it. **Close alert** records the outcome — *Handled*,
  *False alarm* or *Authorised person* — and an optional note, and who did it. Everything is in History.
* **Expecting people** (cleaners, an early delivery)? **Disarm** for up to 12 hours with a reason. The team is told, it is
  recorded, it re-arms by itself, and anyone still inside when it re-arms is checked then. Disarming never silences
  *Off-limits* zones.
* Changing a zone's security setting is also recorded in History, with who changed it.

## Tuning false alarms

| Symptom | Fix |
|---|---|
| Staff keep tripping it at closing | Raise `CLOSING_GRACE_MINUTES` |
| Early staff trip the door at opening | Raise `OPENING_GRACE_MINUTES` |
| Cleaners / a delivery are expected | Disarm with a reason |
| Flickers from reflections, posters, TVs | Raise `--presence-arrive-seconds` and `--presence-confidence` |
| A zone is a thoroughfare | Set it to *No rule*, and watch the door/window instead |

## Test it before you trust it (a walk test)

1. Set opening hours so that right now counts as closed (and wait out the grace period), turn on shadow mode.
2. Mark a door zone, a closed-hours zone and an off-limits zone. Walk into each on camera. Each should alert within
   a few seconds (arrival time + network).
3. Stand still in the closed-hours zone across a pretend closing time and check the "still inside" alert.
4. Walk out; the alert should say no one is detected and stay open. Close it, check the outcome in History.
5. Disarm, repeat step 2, and confirm door and closed-hours zones stay quiet but off-limits still alerts.
6. Repeat at the real hours, in real night-time lighting. This is the test that matters most.

## Limits — please read

* **A detection is not proof.** It means the person detector saw a person-shaped object in a zone at that time. Reflections,
  mannequins, posters and screens can be false positives; a person in a blind spot, behind stock, or in poor light can be
  a false negative. Every alert says "possible" and asks a person to check the cameras.
* **It cannot identify anyone.** By design there is no facial recognition (see the privacy notice and `DPIA.md`), so it
  cannot tell a thief from the night cleaner. That is what *Disarm* and *Authorised person* are for.
* **Night-time quality is the weak point.** Detector accuracy drops in low light, and break-ins happen at night. Use
  IR-capable cameras with adequate lighting, and measure the miss rate in your own building (walk test, step 6) before
  depending on it.
* **"Hiding" only catches someone the cameras could see when the venue closed**, in a zone with a rule. Someone who
  went straight from a blind spot to a hiding place was never detected.
* **Cameras only cover what they cover.** A door with no camera on it, or a polygon that misses the doorway, is not monitored.
* **It is not a monitored burglar alarm.** There is no siren, no police dispatch and no 24/7 human on the end of it. If
  nobody sees the alert, nothing happens. Keep any existing alarm; use this as an additional prompt.
* **Alerts depend on the pipeline and Redis being up.** A silent camera pipeline looks like a quiet night. Watch for the
  coverage skill and detector being healthy (System status) as part of the opening checklist.
* **Daylight-saving changes** can move an opening or closing boundary by an hour for that one night; the grace periods
  absorb most of it.
* Check local rules on workplace monitoring, and tell staff (the privacy notice already does, and everyone is asked to
  re-read it when this ships).

## What is stored

* **Not stored:** video, images, identities, and the presence readings themselves (they are neither broadcast nor kept).
* **Stored:** the alerts and what people did about them (who acknowledged or closed, outcome, note), zone-setting
  changes and disarm/re-arm events, in the event history — kept for `FLOORWATCH_EVENT_HISTORY_RETENTION_DAYS`, then
  deleted automatically, like every other history event. Open alerts are also mirrored to the shared Redis snapshot so a
  redeploy does not make them vanish.
