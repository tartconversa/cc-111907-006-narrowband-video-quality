"""Service-experience analysis.

Rules implemented here (see INPUT_CONTRACT.md):

* overlapping fragments of one session are never added twice;
* a newer revision replaces older versions of the same source identity;
* equal-rank records with conflicting quality are a dispute, not a best value;
* unknown quality is neither good nor bad and is always disclosed;
* only verifiable bad playback inside the service period consumes tolerance;
* planned maintenance removes its own interval only and never restores service;
* every site is accounted against its own contract, never pooled.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from . import interval
from .sources import Complaint, Contract, Probe, Segment, SourceData, Window
from .timeutil import minutes_between, month_bounds

GOOD = "good"
BAD = "bad"
UNKNOWN = "unknown"
DISPUTED = "disputed"

QUALITY_STATES = (GOOD, BAD, UNKNOWN, DISPUTED)


def r3(value: float) -> float:
    return round(value + 0.0, 3)


@dataclass
class Piece:
    start: datetime
    end: datetime
    state: str
    quality: float | None
    values: tuple[float, ...]
    segments: tuple[str, ...]
    revisions: tuple[int, ...]

    def minutes(self) -> float:
        return minutes_between(self.start, self.end)


@dataclass
class SessionTimeline:
    site: str
    session: str
    span_start: datetime
    span_end: datetime
    observed: list[tuple[datetime, datetime]]
    gaps: list[tuple[datetime, datetime]]
    pieces: list[Piece]
    segment_ids: list[str]
    networks: list[str]
    other_sites: list[str]
    totals: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "site": self.site,
            "session": self.session,
            "span": [self.span_start.isoformat(), self.span_end.isoformat()],
            "observed_minutes": r3(interval.total_minutes(self.observed)),
            "gap_minutes": r3(interval.total_minutes(self.gaps)),
            "gaps": [_interval_dict(g) for g in self.gaps],
            "segments": sorted(self.segment_ids),
            "networks": sorted(self.networks),
            "other_sites": sorted(self.other_sites),
            "migration": bool(self.other_sites),
            "link_switch": len(set(self.networks)) > 1,
            "totals": {k: r3(v) for k, v in self.totals.items()},
            "pieces": [
                {
                    "start": p.start.isoformat(),
                    "end": p.end.isoformat(),
                    "minutes": r3(p.minutes()),
                    "state": p.state,
                    "quality": p.quality,
                    "values": list(p.values),
                    "segments": list(p.segments),
                }
                for p in self.pieces
            ],
        }


@dataclass
class SiteAnalysis:
    site: str
    contract: Contract | None
    network: str | None
    region: str | None
    sessions: list[SessionTimeline]
    totals: dict[str, float]
    maintenance_excluded: float
    month_start: datetime
    month_end: datetime
    session_gap_minutes: float = 0.0
    findings: list[dict] = field(default_factory=list)

    @property
    def tolerance(self) -> float:
        return self.contract.monthly_bad_minutes if self.contract else 0.0

    @property
    def remaining_tolerance(self) -> float:
        return r3(self.tolerance - self.totals.get(BAD, 0.0))

    @property
    def within_tolerance(self) -> bool | None:
        if self.contract is None:
            return None
        return self.totals.get(BAD, 0.0) <= self.tolerance + 1e-9

    @property
    def observed_minutes(self) -> float:
        return r3(
            self.totals.get(GOOD, 0.0)
            + self.totals.get(BAD, 0.0)
            + self.totals.get(UNKNOWN, 0.0)
            + self.totals.get(DISPUTED, 0.0)
        )

    def verdict(self) -> str:
        if self.contract is None:
            return "合同缺失，无法判定"
        if self.observed_minutes <= 0:
            return "无可用播放证据，不能视为良好"
        if not self.within_tolerance:
            return "超出当月坏分钟容忍"
        if self.totals.get(UNKNOWN, 0.0) or self.totals.get(DISPUTED, 0.0):
            return "在容忍内，但存在未核实覆盖"
        return "在容忍内"

    def as_dict(self) -> dict:
        contract = self.contract
        return {
            "site": self.site,
            "network": self.network,
            "region": self.region,
            "contract": contract.contract if contract else None,
            "tier": contract.tier if contract else None,
            "min_quality": contract.min_quality if contract else None,
            "tolerance_bad_minutes": r3(self.tolerance),
            "bad_minutes": r3(self.totals.get(BAD, 0.0)),
            "good_minutes": r3(self.totals.get(GOOD, 0.0)),
            "unknown_minutes": r3(self.totals.get(UNKNOWN, 0.0)),
            "disputed_minutes": r3(self.totals.get(DISPUTED, 0.0)),
            "observed_minutes": self.observed_minutes,
            "maintenance_excluded_minutes": r3(self.maintenance_excluded),
            "session_gap_minutes": r3(self.session_gap_minutes),
            "remaining_tolerance": self.remaining_tolerance,
            "within_tolerance": self.within_tolerance,
            "verdict": self.verdict(),
        }


def _interval_dict(pair: tuple[datetime, datetime]) -> dict[str, str]:
    return {"start": pair[0].isoformat(), "end": pair[1].isoformat()}


def _classify_piece(active: list[Segment], min_quality: float | None) -> tuple[str, float | None, tuple[float, ...]]:
    """Classify one instant from every live fragment covering it.

    Revision ordering applies only within one source identity (``id``).  Two
    different identities that overlap are both live; one does not supersede the
    other.  A live fact with a known quality observes the instant even if a
    parallel fragment has no quality (missing quality is not a contradicting
    observation).  If live facts disagree on known quality the instant is
    disputed; if no live fact knows the quality it is unknown.  Nothing here
    chooses the flattering value.
    """

    by_id: dict[str, list[Segment]] = defaultdict(list)
    for segment in active:
        by_id[segment.id].append(segment)
    live: list[Segment] = []
    for identity, rows in by_id.items():
        newest = max(row.revision for row in rows)
        top = [row for row in rows if row.revision == newest]
        qualities = {row.quality for row in top}
        if len(qualities) > 1:
            known = tuple(sorted(value for value in qualities if value is not None))
            return DISPUTED, None, known or (None,)
        live.append(top[0])
    known_values = {row.quality for row in live if row.quality is not None}
    if len(known_values) > 1:
        return DISPUTED, None, tuple(sorted(known_values))
    if not known_values:
        return UNKNOWN, None, ()
    value = next(iter(known_values))
    if min_quality is None:
        return UNKNOWN, value, (value,)
    state = GOOD if value >= min_quality else BAD
    return state, value, (value,)


def build_timeline(
    site: str,
    session: str,
    rows: list[Segment],
    all_site_rows: list[Segment],
    min_quality: float | None,
) -> SessionTimeline:
    boundaries = sorted({row.start for row in rows} | {row.end for row in rows})
    pieces: list[Piece] = []
    for start, end in zip(boundaries, boundaries[1:]):
        active = [row for row in rows if row.start < end and row.end > start]
        if not active:
            continue
        state, quality, values = _classify_piece(active, min_quality)
        pieces.append(
            Piece(
                start=start,
                end=end,
                state=state,
                quality=quality,
                values=values,
                segments=tuple(sorted({row.id for row in active})),
                revisions=tuple(sorted({row.revision for row in active})),
            )
        )
    observed = interval.merge([(row.start, row.end) for row in rows])
    span_start = min(row.start for row in rows)
    span_end = max(row.end for row in rows)
    gaps = interval.subtract((span_start, span_end), observed)
    totals = {state: 0.0 for state in QUALITY_STATES}
    for piece in pieces:
        totals[piece.state] += piece.minutes()
    networks = sorted({row.network for row in rows})
    other_sites = sorted({row.site for row in all_site_rows if row.site != site})
    return SessionTimeline(
        site=site,
        session=session,
        span_start=span_start,
        span_end=span_end,
        observed=observed,
        gaps=gaps,
        pieces=pieces,
        segment_ids=[row.id for row in rows],
        networks=networks,
        other_sites=other_sites,
        totals=totals,
    )


def _site_rows(data: SourceData, site: str, as_of: datetime) -> list[Segment]:
    return [row for row in data.segments if row.site == site and row.received <= as_of]


def build_session_timelines(data: SourceData, site: str, as_of: datetime) -> list[SessionTimeline]:
    contract = data.site_contract(site)
    min_quality = contract.min_quality if contract else None
    rows = _site_rows(data, site, as_of)
    site_rows_all = [row for row in data.segments if row.received <= as_of]
    by_session: dict[str, list[Segment]] = defaultdict(list)
    for row in rows:
        by_session[row.session].append(row)
    timelines = []
    for session, session_rows in by_session.items():
        timelines.append(build_timeline(site, session, session_rows, site_rows_all, min_quality))
    timelines.sort(key=lambda t: (t.span_start, t.session))
    return timelines


def _maintenance(windows: list[Window], site: str) -> list[tuple[datetime, datetime]]:
    return interval.merge([(w.start, w.end) for w in windows if w.site == site and w.type == "maintenance"])


def _consume(piece: Piece, start: datetime, end: datetime, maintenance: list[tuple[datetime, datetime]]):
    clipped = interval.intersect((piece.start, piece.end), (start, end))
    if clipped is None:
        return []
    return interval.subtract(clipped, maintenance)


def analyse_site(
    data: SourceData,
    site: str,
    month: str,
    as_of: datetime,
    timelines: list[SessionTimeline] | None = None,
) -> SiteAnalysis:
    start, end = month_bounds(month)
    contract = data.site_contract(site)
    site_record = data.sites.get(site)
    timelines = timelines if timelines is not None else build_session_timelines(data, site, as_of)
    maintenance = _maintenance(data.windows, site)
    totals = {state: 0.0 for state in QUALITY_STATES}
    maintenance_hits: list[tuple[tuple[datetime, datetime], list[tuple[datetime, datetime]]]] = []
    gap_hits: list[tuple[datetime, datetime]] = []
    month_timelines = []
    for timeline in timelines:
        if timeline.span_end <= start or timeline.span_start >= end:
            continue
        gap = interval.intersect((timeline.span_start, timeline.span_end), (start, end))
        if gap:
            gap_hits.extend(interval.subtract(gap, timeline.observed))
        for piece in timeline.pieces:
            clipped = interval.intersect((piece.start, piece.end), (start, end))
            if clipped is None:
                continue
            kept = interval.subtract(clipped, maintenance)
            totals[piece.state] += sum(minutes_between(*part) for part in kept)
            maintenance_hits.append((clipped, kept))
        month_timelines.append(timeline)
    # Sampling gaps and maintenance exclusions are wall-clock figures; merging
    # them stops two parallel sessions from reporting the same minute twice.
    session_gaps = interval.total_minutes(gap_hits)
    # Report exactly the observed playback that maintenance removed from the
    # contract totals (merged so parallel sessions are not counted twice).
    removed = []
    for clipped, kept in maintenance_hits:
        for part in interval.subtract(clipped, kept):
            removed.append(part)
    maintenance_excluded = interval.total_minutes(removed)
    if contract is not None and sum(totals.values()) <= 0:
        month_timelines = []
    return SiteAnalysis(
        site=site,
        contract=contract,
        network=site_record.network if site_record else None,
        region=site_record.region if site_record else None,
        sessions=month_timelines,
        totals=totals,
        maintenance_excluded=maintenance_excluded,
        month_start=start,
        month_end=end,
        session_gap_minutes=session_gaps,
    )


def _course_windows(data: SourceData, site: str, start: datetime, end: datetime) -> list[Window]:
    return [w for w in data.windows if w.site == site and w.type == "course" and w.start < end and w.end > start]


def course_impact(site_analysis: SiteAnalysis, data: SourceData) -> list[dict]:
    windows = _course_windows(data, site_analysis.site, site_analysis.month_start, site_analysis.month_end)
    impact = []
    for window in windows:
        bad = good = unknown = disputed = 0.0
        for timeline in site_analysis.sessions:
            for piece in timeline.pieces:
                clipped = interval.intersect((piece.start, piece.end), (window.start, window.end))
                if clipped is None:
                    continue
                clipped = interval.subtract(clipped, _maintenance(data.windows, site_analysis.site))
                minutes = sum(minutes_between(*part) for part in clipped)
                if piece.state == BAD:
                    bad += minutes
                elif piece.state == GOOD:
                    good += minutes
                elif piece.state == UNKNOWN:
                    unknown += minutes
                else:
                    disputed += minutes
        impact.append(
            {
                "window": window.id,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "bad_minutes": r3(bad),
                "good_minutes": r3(good),
                "unknown_minutes": r3(unknown),
                "disputed_minutes": r3(disputed),
            }
        )
    impact.sort(key=lambda item: item["start"])
    return impact


def late_facts(data: SourceData, as_of: datetime) -> dict:
    segments = [row.id for row in data.segments if row.received > as_of]
    complaints = [row.id for row in data.complaints if row.received > as_of]
    return {
        "segments": sorted(segments),
        "complaints": sorted(complaints),
        "count": len(segments) + len(complaints),
    }


def late_corrections(data: SourceData, as_of: datetime) -> list[dict]:
    """Corrections that arrived after a month snapshot.

    A correction is any fact received after ``as_of``.  For segment identities
    with more than one stored revision the later revision is reported here even
    when its own ``received`` is inside the window, so a monthly conclusion can
    point at what changed.
    """

    corrections = []
    latest: dict[str, int] = {}
    for row in data.segments:
        latest[row.id] = max(latest.get(row.id, 0), row.revision)
    for row in data.segments:
        if row.received > as_of:
            corrections.append(
                {
                    "kind": "segment",
                    "id": row.id,
                    "revision": row.revision,
                    "received": row.received.isoformat(),
                    "supersedes_older_revision": row.revision < latest[row.id],
                }
            )
    for row in data.complaints:
        if row.received > as_of:
            corrections.append(
                {
                    "kind": "complaint",
                    "id": row.id,
                    "revision": None,
                    "received": row.received.isoformat(),
                    "supersedes_older_revision": False,
                }
            )
    corrections.sort(key=lambda item: (item["received"], item["kind"], item["id"]))
    return corrections


def build_month_analysis(data: SourceData, month: str, as_of: datetime) -> dict:
    start, end = month_bounds(month)
    sites = []
    all_sessions: dict[str, list[dict]] = defaultdict(list)
    for site in sorted(data.sites):
        timelines = build_session_timelines(data, site, as_of)
        site_analysis = analyse_site(data, site, month, as_of, timelines)
        for timeline in site_analysis.sessions:
            all_sessions[timeline.session].append({"site": site, "timeline": timeline})
        record = site_analysis.as_dict()
        record["course_impact"] = course_impact(site_analysis, data)
        record["evidence"] = {
            "has_playback": site_analysis.observed_minutes > 0,
            "unknown_or_disputed_minutes": r3(
                site_analysis.totals.get(UNKNOWN, 0.0) + site_analysis.totals.get(DISPUTED, 0.0)
            ),
            "sampling_gap_minutes": r3(site_analysis.session_gap_minutes),
            "note": "未观察到的时段不代表服务良好；未知质量不计入好或坏。",
        }
        sites.append(record)

    handoffs = []
    for session, occurrences in sorted(all_sessions.items()):
        site_names = sorted({item["site"] for item in occurrences})
        networks = sorted({n for item in occurrences for n in item["timeline"].networks})
        if len(site_names) > 1 or len(networks) > 1:
            handoffs.append(
                {
                    "session": session,
                    "sites": site_names,
                    "networks": networks,
                    "migration": len(site_names) > 1,
                    "link_switch": len(networks) > 1,
                    "segments": sorted(
                        {sid for item in occurrences for sid in item["timeline"].segment_ids}
                    ),
                }
            )

    disputes = []
    for site_analysis in (
        analyse_site(data, site, month, as_of, build_session_timelines(data, site, as_of)) for site in sorted(data.sites)
    ):
        for timeline in site_analysis.sessions:
            for piece in timeline.pieces:
                if piece.state == DISPUTED and interval.intersect((piece.start, piece.end), (start, end)):
                    disputes.append(
                        {
                            "site": site_analysis.site,
                            "session": timeline.session,
                            "start": piece.start.isoformat(),
                            "end": piece.end.isoformat(),
                            "values": list(piece.values),
                            "segments": list(piece.segments),
                            "reason": "无先后关系且质量冲突，不任选好值",
                        }
                    )

    breaches = [site for site in sites if site["within_tolerance"] is False]
    tier1_breaches = [site for site in sites if site["within_tolerance"] is False and site["tier"] == 1]
    no_evidence = [site for site in sites if site["observed_minutes"] <= 0]
    aggregate_bad = r3(sum(site["bad_minutes"] for site in sites))
    aggregate_tolerance = r3(sum(site["tolerance_bad_minutes"] for site in sites))
    aggregate_within = aggregate_bad <= aggregate_tolerance + 1e-9
    if breaches:
        conclusion = "不达标：存在站点超出自身合同容忍"
    elif no_evidence:
        conclusion = "证据不足：有站点没有任何可核实播放"
    else:
        conclusion = "各站点均未超出自身合同容忍（不等于所有时段已核实）"
    masking = bool(aggregate_within and breaches)
    return {
        "month": month,
        "as_of": as_of.isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "sites": sites,
        "contracts": _contract_rollup(sites),
        "overall": {
            "aggregate_bad_minutes": aggregate_bad,
            "aggregate_tolerance": aggregate_tolerance,
            "aggregate_within_tolerance": aggregate_within,
            "sites_in_breach": [site["site"] for site in breaches],
            "tier1_sites_in_breach": [site["site"] for site in tier1_breaches],
            "sites_without_playback_evidence": [site["site"] for site in no_evidence],
            "conclusion": conclusion,
            "aggregate_masks_site_breach": masking,
            "note": (
                "合同坏分钟按站点分别核算，不在站间调剂；整体数值仅作背景，"
                "不得覆盖站点级结论。"
            ),
        },
        "handoffs": handoffs,
        "disputes": disputes,
        "late_facts": late_facts(data, as_of),
        "late_corrections": late_corrections(data, as_of),
        "findings": [f.as_dict() for f in data.findings],
        "source_digests": dict(data.digests),
    }


def _contract_rollup(sites: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for site in sites:
        grouped[str(site["contract"])].append(site)
    rollup = []
    for contract, members in sorted(grouped.items()):
        rollup.append(
            {
                "contract": contract,
                "tier": members[0]["tier"],
                "tolerance_bad_minutes": members[0]["tolerance_bad_minutes"],
                "sites": [
                    {
                        "site": member["site"],
                        "bad_minutes": member["bad_minutes"],
                        "within_tolerance": member["within_tolerance"],
                        "verdict": member["verdict"],
                    }
                    for member in sorted(members, key=lambda item: item["site"])
                ],
                "note": "同一合同的站点各自核算，不合并坏分钟。",
            }
        )
    return rollup


def site_priority(site_record: dict) -> dict:
    """Operational priority.  Higher score sorts first.

    Combines remaining tolerance (less remaining is more urgent), tier, affected
    course minutes and recent bad exposure.  The explanation is returned so the
    duty lead can see why one site outranks another.
    """

    tier = site_record.get("tier") or 99
    remaining = site_record.get("remaining_tolerance", 0.0)
    tolerance = site_record.get("tolerance_bad_minutes") or 0.0
    bad = site_record.get("bad_minutes", 0.0)
    course_bad = sum(item["bad_minutes"] for item in site_record.get("course_impact", []))
    used_ratio = bad / tolerance if tolerance else (1.0 if bad else 0.0)
    score = (5 - min(tier, 4)) * 100 + used_ratio * 60 + course_bad * 1.5
    if remaining < 0:
        score += 200
    reasons = [
        f"等级 {tier}",
        f"剩余容忍 {r3(remaining)} 分钟",
        f"课程时段坏分钟 {r3(course_bad)}",
        f"当月坏分钟 {r3(bad)}",
    ]
    return {"score": r3(score), "reasons": reasons}


@dataclass
class RestoreEvidence:
    site: str
    since: datetime
    as_of: datetime
    contract: Contract | None
    new_good_minutes: float
    observed_minutes: float
    bad_minutes: float
    unknown_minutes: float
    disputed_minutes: float
    maintenance_excluded: float
    pieces: list[Piece]

    @property
    def required_minutes(self) -> float:
        return self.contract.restore_minutes if self.contract else 0.0

    @property
    def sufficient(self) -> bool:
        return self.contract is not None and self.new_good_minutes >= self.required_minutes - 1e-9

    def as_dict(self) -> dict:
        if self.contract is None:
            status = "合同缺失，无法确认恢复"
        elif self.observed_minutes <= 0:
            status = "无修复后播放证据，等候确认"
        elif not self.sufficient:
            status = "观察不足，等候确认"
        else:
            status = "满足合同恢复条件"
        return {
            "site": self.site,
            "since": self.since.isoformat(),
            "as_of": self.as_of.isoformat(),
            "contract": self.contract.contract if self.contract else None,
            "required_good_minutes": r3(self.required_minutes),
            "new_good_minutes": r3(self.new_good_minutes),
            "observed_minutes": r3(self.observed_minutes),
            "bad_minutes_after": r3(self.bad_minutes),
            "unknown_minutes_after": r3(self.unknown_minutes),
            "disputed_minutes_after": r3(self.disputed_minutes),
            "maintenance_excluded_minutes": r3(self.maintenance_excluded),
            "sufficient": self.sufficient,
            "status": status,
            "note": "只计算修复完成后新增、且在维护排除区间外的好质量播放覆盖。",
            "pieces": [
                {
                    "start": piece.start.isoformat(),
                    "end": piece.end.isoformat(),
                    "state": piece.state,
                    "minutes": r3(piece.minutes()),
                    "segments": list(piece.segments),
                    "role": "good_after_repair",
                }
                for piece in self.pieces
            ],
        }


def restore_evidence(data: SourceData, site: str, since: datetime, as_of: datetime) -> RestoreEvidence:
    timelines = build_session_timelines(data, site, as_of)
    maintenance = _maintenance(data.windows, site)
    new_good = observed = bad = unknown = disputed = excluded = 0.0
    evidence_pieces: list[Piece] = []
    for timeline in timelines:
        for piece in timeline.pieces:
            clipped = interval.intersect((piece.start, piece.end), (since, as_of))
            if clipped is None:
                continue
            parts = interval.subtract(clipped, maintenance)
            minutes = sum(minutes_between(*part) for part in parts)
            excluded += minutes_between(*clipped) - minutes
            observed += minutes
            if piece.state == BAD:
                bad += minutes
            elif piece.state == UNKNOWN:
                unknown += minutes
            elif piece.state == DISPUTED:
                disputed += minutes
            elif piece.state == GOOD:
                # "新增" good coverage: the fragments proving it must have
                # entered the system after the repair, so pre-repair history is
                # never recycled as recovery evidence.  The post-repair tail of
                # a session that was already playing still counts.
                contributing = [
                    row
                    for row in data.segments
                    if row.id in piece.segments and row.site == site and row.received <= as_of
                ]
                if contributing and all(row.received > since for row in contributing):
                    new_good += minutes
                    if minutes > 0:
                        evidence_pieces.append(piece)
    return RestoreEvidence(
        site=site,
        since=since,
        as_of=as_of,
        contract=data.site_contract(site),
        new_good_minutes=new_good,
        observed_minutes=observed,
        bad_minutes=bad,
        unknown_minutes=unknown,
        disputed_minutes=disputed,
        maintenance_excluded=excluded,
        pieces=evidence_pieces,
    )


# ---------------------------------------------------------------------------
# Complaints
# ---------------------------------------------------------------------------

DEFAULT_LINK_WINDOW_MINUTES = 30
DEFAULT_LOSS_THRESHOLD = 1.0


def complaint_evidence(
    data: SourceData,
    complaint: Complaint,
    as_of: datetime,
    *,
    window_minutes: float = DEFAULT_LINK_WINDOW_MINUTES,
    loss_threshold: float = DEFAULT_LOSS_THRESHOLD,
) -> dict:
    """Evidence available for linking a complaint.  Pure proximity is not basis."""

    from datetime import timedelta

    start = complaint.occurred - timedelta(minutes=window_minutes)
    end = complaint.occurred + timedelta(minutes=window_minutes)
    timelines = build_session_timelines(data, complaint.site, as_of)
    playback = []
    unresolved = []
    for timeline in timelines:
        for piece in timeline.pieces:
            overlap = interval.intersect((piece.start, piece.end), (start, end))
            if not overlap or piece.state == GOOD:
                continue
            entry = {
                "session": timeline.session,
                "start": piece.start.isoformat(),
                "end": piece.end.isoformat(),
                "state": piece.state,
                "minutes": r3(minutes_between(*overlap)),
                "segments": list(piece.segments),
            }
            if piece.state == BAD:
                playback.append(entry)
            else:
                # Unknown/disputed quality is disclosed but is not by itself
                # evidence that the service was bad.
                unresolved.append(entry)
    network = data.site_network(complaint.site)
    probes = [
        probe
        for probe in data.probes
        if probe.network == network
        and probe.loss_pct >= loss_threshold
        and interval.intersect((probe.start, probe.end), (start, end))
    ]
    basis = []
    if playback:
        basis.append("playback_bad_evidence")
    if probes:
        basis.append("network_probe")
    return {
        "complaint": complaint.id,
        "site": complaint.site,
        "network": network,
        "occurred": complaint.occurred.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "playback": playback,
        "unresolved_playback": unresolved,
        "probes": [
            {
                "id": probe.id,
                "network": probe.network,
                "start": probe.start.isoformat(),
                "end": probe.end.isoformat(),
                "loss_pct": probe.loss_pct,
            }
            for probe in probes
        ],
        "basis": basis,
        "sufficient": bool(basis),
        "note": (
            "仅时间接近或质量未知的播放不构成关联依据；关联不足时应单独立案调查，"
            "不得为减少派单强行合并。"
        ),
    }


def shared_network_candidates(
    data: SourceData,
    as_of: datetime,
    *,
    loss_threshold: float = DEFAULT_LOSS_THRESHOLD,
    window_minutes: float = DEFAULT_LINK_WINDOW_MINUTES,
) -> list[dict]:
    """High-loss probe periods and the sites/complaints that overlap them."""

    candidates = []
    for probe in data.probes:
        if probe.loss_pct < loss_threshold or probe.start > as_of:
            continue
        sites = sorted({site.site for site in data.sites.values() if site.network == probe.network})
        complaints = []
        for complaint in data.complaints:
            if complaint.received > as_of or complaint.site not in sites:
                continue
            # A shared-fault candidate needs a probe that actually covers the
            # complaint instant; a probe merely running "nearby" is not basis.
            if not interval.covers_point((probe.start, probe.end), complaint.occurred):
                continue
            complaints.append(complaint.id)
        if len(sites) < 2:
            continue
        candidates.append(
            {
                "network": probe.network,
                "probe": probe.id,
                "start": probe.start.isoformat(),
                "end": probe.end.isoformat(),
                "loss_pct": probe.loss_pct,
                "sites": sites,
                "complaints": sorted(complaints),
                "shared": True,
                "note": "共享链路候选；各站点影响时段、等级与证据缺口仍分别核算。",
            }
        )
    return candidates


