"""US equity-market hours helper.

The trade scheduler needs to know whether right now is a regular trading
session before firing a tick — Alpaca rejects ``market+day`` orders
submitted outside RTH (verified live earlier: pre-open close orders all
came back ``canceled`` instantly), so a tick fired at 03:00 ET is pure
LLM spend with zero possibility of a fill.

US equity market regular session: 09:30 → 16:00 America/New_York,
Mon–Fri **minus US federal holidays**. The holiday rules are
hand-coded below (``is_us_market_holiday``) — no external dependency
because the NYSE holiday set is algorithmically stable:

  New Year's Day, MLK Day (3rd Mon Jan), Presidents Day (3rd Mon Feb),
  Memorial Day (last Mon May), Juneteenth (Jun 19, since 2022),
  Independence Day (Jul 4), Labor Day (1st Mon Sep),
  Thanksgiving (4th Thu Nov), Christmas (Dec 25).

Observed-shift: a holiday landing on Saturday is observed the prior
Friday; on Sunday, the following Monday — same rule NYSE uses.

**Not modelled**: Good Friday (movable, needs Easter math; gap = 1 day
per year). Early-close days (day after Thanksgiving, Christmas Eve)
are modelled as full trading sessions — Alpaca closes early but
``market+day`` orders submitted before 13:00 ET still fill. Same call
on whether to back this out as the user's appetite for false-positive
ticks dictates.

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


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> _dt.date:
    """The Nth occurrence of ``weekday`` in ``year-month``. ``weekday``
    follows Python's Monday=0 convention. Used for floating holidays
    (MLK Day = 3rd Monday of January, Memorial Day = last Monday of May,
    Thanksgiving = 4th Thursday of November)."""
    first = _dt.date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + _dt.timedelta(days=delta + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> _dt.date:
    """The last occurrence of ``weekday`` in ``year-month``. Memorial Day is
    the last Monday of May (not the 4th, because May sometimes has 5)."""
    if month == 12:
        next_month = _dt.date(year + 1, 1, 1)
    else:
        next_month = _dt.date(year, month + 1, 1)
    last = next_month - _dt.timedelta(days=1)
    delta = (last.weekday() - weekday) % 7
    return last - _dt.timedelta(days=delta)


def _observed_shift(d: _dt.date) -> _dt.date:
    """NYSE observed-day rule: Sat → prior Friday, Sun → following Monday.
    Used for fixed-date holidays (New Year, Juneteenth, Independence Day,
    Christmas). Floating holidays already land on a weekday by definition."""
    if d.weekday() == 5:  # Saturday
        return d - _dt.timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + _dt.timedelta(days=1)
    return d


def is_us_market_holiday(date: _dt.date) -> bool:
    """True iff ``date`` is a US equity-market holiday under NYSE rules.

    Does NOT include Good Friday (movable, Easter-based, deferred to v2).
    Does NOT treat early-close days as full closures. The set is fixed
    enough that algorithmic generation is more reliable than a hardcoded
    table that drifts year to year.
    """
    y = date.year
    # Fixed-date holidays with observed-day shifts.
    fixed_observed = {
        _observed_shift(_dt.date(y, 1, 1)),   # New Year's Day
        _observed_shift(_dt.date(y, 7, 4)),   # Independence Day
        _observed_shift(_dt.date(y, 12, 25)), # Christmas
    }
    # Juneteenth became a federal holiday in 2021; NYSE observed from 2022.
    if y >= 2022:
        fixed_observed.add(_observed_shift(_dt.date(y, 6, 19)))
    if date in fixed_observed:
        return True
    # Floating holidays — these always land on a fixed weekday so no shift.
    floating = {
        _nth_weekday_of_month(y, 1, 0, 3),    # MLK Day (3rd Mon Jan)
        _nth_weekday_of_month(y, 2, 0, 3),    # Presidents Day (3rd Mon Feb)
        _last_weekday_of_month(y, 5, 0),      # Memorial Day (last Mon May)
        _nth_weekday_of_month(y, 9, 0, 1),    # Labor Day (1st Mon Sep)
        _nth_weekday_of_month(y, 11, 3, 4),   # Thanksgiving (4th Thu Nov)
    }
    return date in floating


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
    if is_us_market_holiday(et.date()):
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
