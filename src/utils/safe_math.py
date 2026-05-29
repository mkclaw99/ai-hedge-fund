"""Small numeric helpers that DCF/CAGR code in the analyst agents keeps reinventing.

Why this exists: ``(latest / oldest) ** (1 / years)`` is the bog-standard CAGR
formula, and every analyst that touches a growth rate has its own copy of it.
The trap: when ``latest`` and ``oldest`` have **opposite signs** (e.g. a company
flips from a profit to a loss between the endpoints), the ratio is negative,
and Python's ``**`` operator on a negative base with a fractional exponent
returns a **complex** number — which silently propagates until something
later (typically ``min``/``max`` or a comparison) blows up with
``TypeError: '<' not supported between instances of 'float' and 'complex'``.

This module centralises the safe version so every analyst gets the same
guard for free: any endpoint that's non-positive (or missing) yields ``None``
and the caller falls back to its conservative default.
"""

from __future__ import annotations

from typing import Optional


def safe_cagr(latest, oldest, years) -> Optional[float]:
    """Compound annual growth rate, returning ``None`` when undefined.

    Returns ``None`` if:
    - ``years`` is missing or non-positive (no time elapsed),
    - either endpoint is missing or non-positive (sign change → complex result),
    - or anything raises during the math (defensive).

    Callers should treat ``None`` as "growth couldn't be measured" and
    substitute a conservative default — *not* propagate it into arithmetic.
    """
    try:
        if years is None or years <= 0:
            return None
        if latest is None or oldest is None:
            return None
        # Non-positive endpoints make CAGR meaningless (and the fractional
        # power on a negative base returns a complex number in Python).
        if oldest <= 0 or latest <= 0:
            return None
        return (float(latest) / float(oldest)) ** (1.0 / float(years)) - 1.0
    except Exception:
        return None
