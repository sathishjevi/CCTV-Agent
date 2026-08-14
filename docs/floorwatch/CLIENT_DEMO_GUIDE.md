# Floorwatch — Client Demo Guide

Companion to [`Floorwatch_Client_Demo.pptx`](Floorwatch_Client_Demo.pptx) (14 slides). This guide is for whoever presents the deck — what to say on each slide, what to demo live, what questions to expect, and where the honest boundaries are. Read the "Don't overclaim" callouts especially closely; several are there because an earlier internal review (`SECURITY_REVIEW.md`, `REQUIREMENTS_STATUS.md`) specifically flagged the risk of overstating either accuracy or financial claims.

**Suggested time**: 20-25 minutes deck + 10-15 minutes live demo + Q&A. Trim slides 2-3 if the client is already sold on the problem.

---

## Before the meeting

- Confirm which of the 4 CCTV source types (RTSP/NVR, local folder, cloud storage, third-party API) is relevant to *this* client, if known — lets you speak to their specific setup on slide 8 instead of all four generically.
- Have the dashboard running against test/demo data (not a real site) before the call — see `SETUP_AUTH_AND_CONFIG.md` and `CCTV_INTEGRATION_SETUP.md` if it's not already up.
- Decide in advance how you'll answer "is this live at a real site yet" — the honest answer is "verified end-to-end against test footage; not yet connected to a real camera" unless that's changed. Slide 10 says this explicitly — don't contradict it live.

---

## Slide-by-slide notes

### Slide 1 — Title
Just the opener. Say the one-line pitch out loud as you land here: *"Floorwatch turns the cameras you already have into real-time coverage intelligence, without storing a single new video."* Don't over-explain yet — slide 2 does the problem framing.

### Slide 2 — The Problem
Ask, don't tell, if the room allows it: *"When a station's uncovered for 10 minutes, how do you find out today?"* Most answers are "a customer complains" or "we don't, until the manager notices." That's the gap this fills. Keep this conversational — it's the only slide built to invite a response rather than present information.

### Slide 3 — Three Pillars
Framing slide only — one sentence per pillar, then move on. This is the outline for slides 5-7, not a place to go deep yet.

### Slide 4 — How It Works
Keep this **high-level**. The audience is the client, not an engineer — "camera feed goes in, AI reads it, alerts come out" is the right depth. If someone asks about the detection model, backend, or infrastructure, it's fine to say "happy to go deeper after — the short version is it runs the detection locally, nothing leaves the building unless you choose a cloud storage source."

**Don't overclaim**: this is a pipeline description, not a benchmark claim. Don't state a specific accuracy percentage on this slide — none is validated against this client's real footage yet (see slide 10 and the note below).

### Slide 5 — Coverage Tracking
This is usually the slide that lands the demo — pause here and go to the live dashboard if you're doing the interleaved-demo format (see "Live demo script" below) rather than finishing all 14 slides first.

Explain the three tiers concretely: *nudge* is a soft reminder, *command* is a direct instruction, *escalate* puts it in front of a supervisor with context. Emphasize **configurable timers** — this isn't a fixed policy, it adapts to their operation.

### Slide 6 — Effort Tracking
The task-card mockup on this slide is illustrative, not a live screenshot — say so if asked directly ("this is a mockup of what you'll see; let me show you the real one" — then pivot to the dashboard). Emphasize the *grace window* concept — this is the answer to "what if someone just paused for a second," a question that comes up almost every time.

### Slide 7 — Supervisor Chat
The "grounded and cited" point is the one to slow down on — it's the difference between a chatbot that might hallucinate and one that only ever answers from real logged events. If asked "can it be wrong," the honest answer: it can only be wrong about *interpretation* of a real event, never invent an event that didn't happen — and every answer shows its source so a supervisor can check it themselves.

### Slide 8 — Any CCTV Setup
This is the slide to customize live if you know the client's actual setup — point at their specific card (RTSP, folder, cloud, or API) rather than presenting all four with equal weight. If they use a third-party surveillance provider, be upfront that this specific integration is the one that needs a short adapter step (see `CCTV_INTEGRATION_SETUP.md` §3) — not a criticism, just accurate.

### Slide 9 — Privacy & Security
Go through every bullet if the client is enterprise/legal-sensitive (many cineplex/retail clients will have loss-prevention or legal in the room) — this slide tends to get the most questions. The "no new video storage" point is usually the biggest relief to a room worried about surveillance overreach — Floorwatch is not building a second video archive of employees, only structured events (zone status, motion signal).

**Don't overclaim**: "shadow mode by default" is accurate and important — say it plainly. Don't imply the system is already handling real notifications at any live site unless it actually is.

### Slide 10 — Current Status
This is the honesty slide — walk through it plainly rather than rushing past it. Clients respond well to a vendor who's upfront about what's tested vs. pending, and it pre-empts the "wait, is this actually running somewhere yet" question. If asked why the real-camera row says "pending access": *"We build and verify against test footage first, then connect real credentials once you're ready — that's a config change, not new development."*

### Slide 11 — Live Demo Agenda
Sets expectations for the next block. If you're doing slides-then-demo (rather than interleaved), this is where you switch to the browser.

### Slide 12 — Roadmap
**Don't overclaim — this is the single most important "don't" in this whole guide.** Everything on this slide is unbuilt. Say "could come next" and "not yet built," not "coming soon" or "in progress." If asked "when will Phase 6 be ready," the honest answer is "that depends on a decision we'd make together first — which POS system, and whether you even want automated discrepancy flags" — not a date.

The footer line about avoiding dollar-figure claims is deliberate — if a client pushes for "so how much money will this save us," redirect to discrepancy *flags for human review*, not a projected dollar figure. `REQUIREMENTS_STATUS.md` explicitly calls out that a wrong financial claim is a bigger liability than a wrong coverage alert — this isn't just caution for its own sake.

### Slide 13 — Next Steps
This is the close — end here, not on the roadmap slide, so the last thing the client remembers is "here's what happens next," not "here's a list of unbuilt features." Walk through each item as an actual open question, not a formality — especially employee notice/consent, which is a real legal prerequisite, not a checkbox.

### Slide 14 — Thank You
Q&A. See below for likely questions.

---

## Live demo script (if showing the real dashboard)

Two ways to run it — pick based on the room:

- **Interleaved** (recommended for a small, engaged group): present slides 1-4, then jump to the dashboard for slides 5-7's content live, then return to slides 8-13.
- **Deck-then-demo** (recommended for a larger/more formal group): present all 14 slides straight through, then demo per the slide 11 agenda.

Either way, cover in the live dashboard:
1. **Coverage view** — point at a zone, explain what "covered" vs. "gap" looks like, and if you can trigger a demo gap, show the nudge appear.
2. **Task card** — assign a task, show the motion/effort indicator update, mark it complete.
3. **Supervisor chat** — ask a real question against demo data ("was zone X covered during hour Y") and show the cited answer, not just the answer text.
4. **Login screen** — worth 10 seconds to show it exists; ties back to slide 9's "per-supervisor authentication" point.

Don't demo real employee data or a real site's live feed in front of a prospective client unless that client is the one being shown their own (already consented) data.

---

## Anticipated questions

| Question | Answer |
|---|---|
| "Is this live anywhere right now?" | Built and verified end-to-end against test footage; not yet connected to a real camera at a live site (see slide 10). |
| "What happens to the video?" | Nothing new is stored — only structured events (zone status, motion signal) are kept. See slide 9. |
| "Can employees be identified from this?" | Floorwatch tracks zone presence and motion, not identity — it doesn't do facial recognition. (If this changes, this line needs to change with it.) |
| "How accurate is it?" | Verified functionally against test footage; accuracy against *this* client's real cameras, lighting, and layout won't be known until it's connected and calibrated on-site. Don't state a number here. |
| "What does it cost / how much will it save us?" | Cost is a business conversation outside this deck's scope. On savings: redirect to discrepancy flags for human review, not a dollar projection (see slide 12 notes above). |
| "Can we turn on notifications right away?" | It ships in shadow mode by default — logs and tracks without notifying — until you explicitly approve going live, after the go-live checklist is satisfied. |
| "What if our POS/surveillance system isn't one you support yet?" | For CCTV, the architecture is designed to plug in a new source type without touching detection — most integrations are a config change. For a genuinely new third-party API, there's a small adapter step. POS integration (Phase 6) hasn't been built for any system yet. |
| "Who else uses this?" | Answer honestly based on actual deployment status — don't imply existing customers if there aren't any yet. |

---

## What NOT to say

- Don't state a specific detection accuracy percentage — none has been validated against a real client site.
- Don't call Phases 6-8 "coming soon," "in development," or give them a date — they're "planned, not built," gated on decisions not yet made.
- Don't promise dollar-figure savings or "$ recovered" numbers — this is explicitly *not* being built without an agreed methodology.
- Don't imply the system currently notifies real employees anywhere — shadow mode is the default and, unless told otherwise, the current state everywhere.
- Don't claim facial recognition or identity tracking — Floorwatch doesn't do this; if asked, say so directly rather than deflecting.
