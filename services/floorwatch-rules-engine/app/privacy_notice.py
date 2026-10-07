"""The privacy notice shown to staff — generated from the running configuration.

A privacy notice that says one thing while the system does another is itself a
compliance failure (the FTC treats that as deception). So this text is not a
hand-written document that can go stale: the retention periods, the list of
outside services and whether an AI assistant is in use all come from the same
settings that drive the actual behavior. Change a retention period or switch
on a new vendor and the notice changes — and its VERSION changes, which makes
every employee's app ask them to read and acknowledge it again.

It is a plain-language starting point written for a workplace operations tool,
not legal advice: have counsel for the jurisdictions you operate in review it
(checklist: "privacy policy reviewed by legal counsel"). Organisation-specific
facts (name, contact, regulator, legal basis) are configured, not guessed; any
that are missing are shown as a visible [placeholder] and `configured` is
False so the diagnostics view flags it."""

import hashlib
import html
from typing import List


def _sms_provider_name(config) -> str:
    return {"msg91": "MSG91", "fast2sms": "Fast2SMS"}.get(getattr(config, "SMS_PROVIDER", "twilio"), "Twilio")


def build_privacy_notice(config) -> dict:
    org = config.ORG_NAME or "[Organisation name — set FLOORWATCH_ORG_NAME]"
    contact = config.PRIVACY_CONTACT or "[Privacy contact — set FLOORWATCH_PRIVACY_CONTACT]"
    regulator = config.PRIVACY_REGULATOR or ""
    basis = config.PRIVACY_LEGAL_BASIS or (
        "our legitimate interest in running safe, well-covered venues, and — where applicable — the "
        "performance of your employment")
    configured = bool(config.ORG_NAME and config.PRIVACY_CONTACT)

    processors: List[str] = [
        f"Railway — cloud hosting of this system (hosted in: {config.HOSTING_REGION}); stores everything described above.",
    ]
    if config.FIREBASE_API_KEY:
        processors.append("Google Firebase Cloud Messaging — delivers task notifications to your phone "
                          "(sees your device's push token and the notification text).")
    processors.append(f"{_sms_provider_name(config)} — sends text-message notifications to your phone number "
                      "if you are set up to receive them by SMS.")
    if config.POSTHOG_API_KEY:
        processors.append("PostHog — receives technical error reports (what failed and where) so problems can be "
                          "fixed. These reports do not include your name, phone number or employee number.")
    if config.PRIVACY_AI_ASSISTANT:
        processors.append("Anthropic (Claude) and Voyage AI — power the supervisors' question-and-answer assistant. "
                          "Questions and the zone/task information they refer to are sent to them; it works from "
                          "employee numbers, not names or phone numbers.")

    sections = [
        ("Who we are", [
            f"{org} runs this system. Questions or requests about your information: {contact}."
            + (f" Our designated privacy contact is {config.PRIVACY_OFFICER}." if config.PRIVACY_OFFICER else ""),
        ]),
        ("What this system does", [
            "Floorwatch helps supervisors keep every area of the venue covered and tasks completed. Supervisors "
            "assign cleaning and service tasks to staff; the system sends you the task, tracks how long it takes, "
            "and flags areas that appear uncovered so a supervisor can follow up.",
        ]),
        ("What information we collect and why", [
            ["Your name, employee number, department and role, and your work phone number — to identify you and "
             "send you tasks.",
             "A login password (stored only as a one-way scrambled value, never readable).",
             "Your phone's notification address (push token) if you use the app — to deliver notifications.",
             "The tasks assigned to you and what happened to them (sent, started, completed, extended, handed off).",
             "Zone-level activity measured from the venue's cameras: whether someone is present in an area and "
             "roughly how much movement there is, to tell whether an assigned task is being worked on. "
             "No video or images are stored by this system and no facial recognition is used; the "
             "measurements are counts and timings tied to an area and a time, not to your face or body.",
             "Records of sign-ins and security events (such as failed sign-in attempts), including network "
             "addresses, to protect accounts."],
        ]),
        ("Why we're allowed to use it", [
            f"We rely on {basis}. We use your information only for the purposes above — assigning and tracking "
            "work, keeping areas covered, protecting the system and meeting legal obligations — and not to "
            "sell it or to advertise to you.",
        ]),
        ("Automated flags and decisions about you", [
            "The system compares the active time measured during a task with what the task is expected to need, "
            "and may flag a task for a supervisor's attention. A flag is not a decision about you: a supervisor "
            "must review it and either confirm or dismiss it, and the system takes no automatic action against "
            "you. You can ask for a human to review any flag that concerns you (see Your rights).",
        ]),
        ("Who receives your information", [
            "Only supervisors and administrators of this system can see staff information, and only what their "
            "role needs. We use these outside services to run it (they handle information on our behalf and under "
            "our instructions):",
            processors,
            "We do not sell your information or share it for advertising.",
        ]),
        ("Where it is processed", [
            f"This system is hosted in: {config.HOSTING_REGION}. Some of the services above may process "
            "information in other countries (for example, where a notification or text-message provider is based). "
            "Where the law requires it, we use approved transfer safeguards for that.",
        ]),
        ("How long we keep it", [
            [f"Task and activity history: {config.EVENT_HISTORY_RETENTION_DAYS} days.",
             f"Finished task records: {config.TASK_RETENTION_DAYS} days.",
             f"Supervisor shift summaries and dashboard accounts of people who have left: {config.RETENTION_DAYS} days.",
             f"Your staff record once you are deactivated: {config.EMPLOYEE_RETENTION_DAYS} days, after which your "
             "name, phone number and login are erased (the employee number is kept so past records remain "
             "consistent, but no longer identifies you).",
             f"Records of privacy requests: {config.PRIVACY_REQUEST_RETENTION_DAYS // 365} years."],
            "These are deleted automatically on a daily schedule.",
        ]),
        ("How we protect it", [
            "Sign-in is protected by passwords stored with strong one-way hashing, optional two-step "
            "verification, rate limiting and lockouts on repeated failures; traffic is encrypted in transit; "
            "access is limited by role; and sign-ins and security events are logged. If a breach affecting your "
            "information occurs, we will notify you and any regulator as the law requires.",
        ]),
        ("Your rights", [
            ["See what we hold about you and get a copy (use Privacy → Download my data in the app).",
             "Have inaccurate information corrected.",
             "Have your information erased, where we have no legal reason to keep it.",
             "Object to or ask us to limit how we use it, and ask for a human to review any automated flag.",
             "Receive your information in a portable, machine-readable format (the download above)."],
            f"Use Privacy → Make a request in the app, or contact {contact}. We respond within "
            f"{config.PRIVACY_REQUEST_DUE_DAYS} days. "
            + (f"If you are unhappy with our response you can complain to {regulator}." if regulator else
               "You may also complain to your local data-protection or privacy regulator."),
        ]),
        ("Children", [
            "This system is for staff of the venue only. It is not intended for anyone under 18 and we do not "
            "knowingly collect information about children.",
        ]),
        ("Changes to this notice", [
            "If this notice changes, the app asks you to read and acknowledge the new version.",
        ]),
    ]

    # Plain-text form (for the app and for hashing) and HTML form (for /privacy).
    text_parts, html_parts = [], []
    for heading, blocks in sections:
        text_parts.append(heading.upper())
        html_parts.append(f"<h2>{html.escape(heading)}</h2>")
        for block in blocks:
            if isinstance(block, list):
                text_parts.extend(f"  • {item}" for item in block)
                html_parts.append("<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in block) + "</ul>")
            else:
                text_parts.append(block)
                html_parts.append(f"<p>{html.escape(block)}</p>")
        text_parts.append("")
    text = "\n".join(text_parts).strip()
    version = config.PRIVACY_NOTICE_VERSION or hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]

    return {
        "version": version, "title": "Privacy notice for staff", "configured": configured,
        "text": text, "html_body": "".join(html_parts),
        "sections": [{"heading": h, "paragraphs": [b if isinstance(b, str) else "\n".join("• " + i for i in b)
                                                    for b in blocks]} for h, blocks in sections],
    }


def render_privacy_page(notice: dict) -> str:
    """Standalone HTML page (public — app-store listings need a privacy-policy URL)."""
    warning = ("" if notice["configured"] else
               '<p class="warn">This notice is not fully configured yet: the organisation name and privacy contact '
               "must be set before it is published.</p>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(notice['title'])}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.55 system-ui, sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.6rem; }} h2 {{ font-size: 1.1rem; margin-top: 1.8rem; }}
  .meta {{ opacity: .65; font-size: .85rem; }} .warn {{ background: #fff3cd; color: #5c4300; padding: .6rem .8rem; border-radius: 6px; }}
</style></head><body>
<h1>{html.escape(notice['title'])}</h1>
<p class="meta">Version {html.escape(notice['version'])}</p>
{warning}
{notice['html_body']}
</body></html>"""
