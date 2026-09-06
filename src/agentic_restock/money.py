"""Indian-scale rupee formatting, shared by the detectors and the narration.

It lives here rather than in `narration` because the detectors now build their own derivation
strings -- "how this figure was arrived at" belongs where the inputs are, and having detection
import presentation to get a comma in the right place was the wrong direction of dependency.
"""

from __future__ import annotations


def format_inr(value: float, *, paise: bool = False) -> str:
    """Indian digit grouping: 6,40,000 rather than 640,000.

    The reports are read by an Indian manufacturer's planners, and 2,45,00,000 is legible to them
    in a way 24,500,000 is not.
    """
    negative = value < 0
    whole = abs(float(value))
    fraction = f"{whole - int(whole):.2f}"[1:] if paise else ""
    digits = str(int(whole))

    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join([*parts, tail])

    return f"{'-' if negative else ''}Rs {grouped}{fraction}"


def format_scale(value: float) -> str:
    """A rupee figure with its crore/lakh scale, since the magnitudes span both."""
    absolute = abs(float(value))
    if absolute >= 1_00_00_000:
        return f"{format_inr(value)} ({absolute / 1_00_00_000:.2f} crore)"
    if absolute >= 1_00_000:
        return f"{format_inr(value)} ({absolute / 1_00_000:.2f} lakh)"
    return format_inr(value)
