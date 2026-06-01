"""US equity-market hours helper.

The trade scheduler needs to know whether right now is a regular trading
session before firing a tick — Alpaca rejects ``market+day`` orders
submitted outside RTH (verified live earlier: pre-open close orders all
came back ``canceled`` instantly), so a tick fired at 03:00 ET is pure
LLM spend with zero possibility of a fill.

US equity market regular session: 09:30 → 16:00 America/New_York,
Mon–Fri. Federal holidays (New Year, MLK, Presidents, Good Friday,
Memorial, Juneteenth, Independence, Labor, Thanksgiving, Christmas)
+ half-days (1pm close on day after Thanksgiving, Christmas Eve)
are NOT modelled here. Holiday support would need ``pandas_market_
calendars`` or similar; deferred to a follow-up. The cost of getting
holidays wrong: a tick fires, the order auto-cancels, ~$0.04 per ticker
wasted on the LLM call. Tolerable; saving the dependency for v1.

Uses ``zoneinfo`` (stdlib in Python 3.9+), no external deps.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Regular trading session. Stored as ``(hour, minute)`` rather than time
# objects so the math is obvious to a reader checking the gate.
_OPEN = (9, 30)
_CLOSE = (16, 0)


def is_market_open(when: _dt.datetime | None = None) -> bool:
    """True iff ``when`` falls inside US equity regular trading hours.

    ``when`` defaults to ``datetime.now(tz=UTC)``. Accepts any timezone-
    aware datetime; converts to ET internally. A naive datetime is
    interpreted as UTC for safety (the scheduler always passes UTC).
    """
    if when is None:
        when = _dt.datetime.now(tz=_dt.timezone.utc)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    et = when.astimezone(_ET)
    # Weekday: 0=Mon .. 6=Sun. 5/6 = Sat/Sun → closed.
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return (_OPEN[0] * 60 + _OPEN[1]) <= minutes < (_CLOSE[0] * 60 + _CLOSE[1])


def seconds_until_next_open(when: _dt.datetime | None = None) -> int:
    """Seconds until the next regular-session open at ``when``'s position.

    Used by callers that want to log "next tick eligible at …" instead of
    silently sleeping through nights/weekends. Conservative: rounds up to
    the next 09:30 ET on the next weekday. Returns 0 if currently open.
    """
    if is_market_open(when):
        return 0
    if when is None:
        when = _dt.datetime.now(tz=_dt.timezone.utc)
    et = when.astimezone(_ET) if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc).astimezone(_ET)
    # Step forward one minute at a time until we find an open slot. Cheap
    # — at most ~3 days of minutes (~4300 iterations) over a long weekend.
    cursor = et.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    while not is_market_open(cursor):
        cursor += _dt.timedelta(minutes=1)
    return int((cursor - et).total_seconds())
