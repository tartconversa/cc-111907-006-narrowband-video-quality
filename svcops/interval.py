"""Half-open interval algebra used for playback coverage and windows."""

from __future__ import annotations

from datetime import datetime

Interval = tuple[datetime, datetime]


def _valid(start: datetime, end: datetime) -> bool:
    return start < end


def merge(intervals: list[Interval]) -> list[Interval]:
    """Union of intervals; touching intervals are combined."""

    if not intervals:
        return []
    ordered = sorted((s, e) for s, e in intervals if _valid(s, e))
    if not ordered:
        return []
    merged: list[Interval] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def intersect(one: Interval, other: Interval) -> Interval | None:
    start = max(one[0], other[0])
    end = min(one[1], other[1])
    return (start, end) if _valid(start, end) else None


def subtract(base: Interval, cuts: list[Interval]) -> list[Interval]:
    """Return the parts of ``base`` not covered by ``cuts``."""

    result: list[Interval] = []
    cursor = base[0]
    for cut in merge(cuts):
        if cut[1] <= cursor:
            continue
        if cut[0] >= base[1]:
            break
        if cut[0] > cursor:
            result.append((cursor, min(cut[0], base[1])))
        cursor = max(cursor, cut[1])
        if cursor >= base[1]:
            break
    if cursor < base[1]:
        result.append((cursor, base[1]))
    return result


def clip(intervals: list[Interval], start: datetime, end: datetime) -> list[Interval]:
    result = []
    for one in intervals:
        clipped = intersect(one, (start, end))
        if clipped:
            result.append(clipped)
    return result


def total_minutes(intervals: list[Interval]) -> float:
    return sum((e - s).total_seconds() / 60.0 for s, e in merge(intervals))


def overlaps(one: Interval, other: Interval) -> bool:
    return intersect(one, other) is not None


def covers_point(one: Interval, moment: datetime) -> bool:
    return one[0] <= moment < one[1]
