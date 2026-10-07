"""Building blocks for request bodies — server-side input validation.

Two things every request model used to lack:

  * Unknown fields were silently accepted and dropped (Pydantic's default).
    `StrictModel` rejects them, so a typo or a smuggled field is an error
    instead of an ignored one — and nothing can ever be mass-assigned into a
    record by sending extra keys.
  * Strings and numbers were unbounded. A 5 MB task name, a 10 MB password
    (which is then hashed) or `assigned_minutes: NaN` (which serialises to
    invalid JSON in the shared snapshot and breaks every dashboard for every
    user) were all accepted. Each field now has a realistic ceiling.

Only UPPER bounds are added to text; emptiness and format stay with the
handlers that already validate them, so existing error messages and status
codes don't change. The one new lower bound is `assigned_minutes > 0`: zero or
negative budgets divide by zero in the dashboards."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _text(max_length: int):
    return Annotated[str, StringConstraints(max_length=max_length)]


Username = _text(64)
Password = _text(256)
Identifier = _text(64)      # employee numbers, zone ids, roles, task types, channel names
Name = _text(120)           # person names, departments, task names
Phone = _text(32)
OtpCode = _text(12)
PushToken = _text(1024)     # FCM tokens are ~160 chars; leave headroom
ChallengeToken = _text(2048)   # a short-lived two-step-verification challenge token
MfaCode = _text(32)          # a 6-digit code, or a recovery code like abcd-1234-abcd-1234
Note = _text(2000)          # a free-text note a person writes (privacy request details, resolution notes)

# A task budget is a working-day-sized number of minutes, never NaN/inf/negative.
AssignedMinutes = Annotated[float, Field(gt=0, le=1440, allow_inf_nan=False)]
# Extensions keep their sign check in the engine (it returns a specific error);
# here we only block NaN/inf and absurd magnitudes.
ExtraMinutes = Annotated[float, Field(ge=-1440, le=1440, allow_inf_nan=False)]
