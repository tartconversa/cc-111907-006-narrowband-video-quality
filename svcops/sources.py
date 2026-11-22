"""Loading, revision resolution and identity rules for the synthetic sources.

The source files are read-only.  Every load produces a :class:`SourceData`
snapshot plus structured data-quality findings that the analyst must see.
Nothing here invents quality, defaults a null to zero, or merges facts solely
because they look similar.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .timeutil import TimeFormatError, parse_ts

KNOWN_FILES = (
    "sites",
    "contracts",
    "segments",
    "probes",
    "complaints",
    "windows",
    "staff",
)


@dataclass(frozen=True)
class Finding:
    """A data-quality problem retained for the operator instead of dropped."""

    code: str
    message: str
    source: str
    identity: str
    severity: str = "warning"

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "identity": self.identity,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class Site:
    site: str
    network: str
    contract: str
    region: str


@dataclass(frozen=True)
class Contract:
    contract: str
    min_quality: float
    monthly_bad_minutes: float
    restore_minutes: float
    tier: int


@dataclass(frozen=True)
class Staff:
    staff: str
    skills: frozenset[str]
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Window:
    id: str
    site: str
    type: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Probe:
    id: str
    network: str
    start: datetime
    end: datetime
    loss_pct: float


@dataclass(frozen=True)
class Complaint:
    id: str
    site: str
    occurred: datetime
    received: datetime
    symptom: str


@dataclass(frozen=True)
class Segment:
    id: str
    revision: int
    session: str
    site: str
    network: str
    start: datetime
    end: datetime
    quality: float | None
    received: datetime


@dataclass
class SourceData:
    sites: dict[str, Site] = field(default_factory=dict)
    contracts: dict[str, Contract] = field(default_factory=dict)
    staff: list[Staff] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    complaints: list[Complaint] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    digests: dict[str, str] = field(default_factory=dict)

    def site_contract(self, site: str) -> Contract | None:
        record = self.sites.get(site)
        return self.contracts.get(record.contract) if record else None

    def site_network(self, site: str) -> str | None:
        record = self.sites.get(site)
        return record.network if record else None


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _as_float(value: Any, field_name: str, identity: str, source: str, findings: list[Finding]) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        findings.append(Finding("invalid-number", f"{field_name} is not numeric", source, identity, "error"))
        return None
    return float(value)


def _as_int(value: Any, field_name: str, identity: str, source: str, findings: list[Finding]) -> int | None:
    number = _as_float(value, field_name, identity, source, findings)
    if number is None:
        return None
    return int(number)


def _as_text(value: Any, field_name: str, identity: str, source: str, findings: list[Finding]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        findings.append(Finding("invalid-text", f"{field_name} is empty or not text", source, identity, "error"))
        return None
    return value.strip()


def _timestamp(value: Any, name: str, identity: str, source: str, findings: list[Finding]) -> datetime | None:
    try:
        return parse_ts(value, field=name)
    except TimeFormatError as exc:
        findings.append(Finding("invalid-time", str(exc), source, identity, "error"))
        return None


def resolve_segment_revisions(rows: Iterable[Segment], findings: list[Finding], source: str) -> list[Segment]:
    """Resolve ``id`` versions.

    A newer revision replaces the older one for the same source identity (id).
    Equal revisions with differing content are a source-version conflict.  There
    is no "newer" fact to choose between them, so neither is discarded: their
    intervals are retained and any overlapping part is classified as disputed
    by the analysis layer instead of silently picking the better value.
    """

    by_id: dict[str, dict[int, list[Segment]]] = {}
    for row in rows:
        by_id.setdefault(row.id, {}).setdefault(row.revision, []).append(row)

    resolved: list[Segment] = []
    for identity, revisions in by_id.items():
        newest = max(revisions)
        # A newer revision explicitly replaces every older one.
        candidates = revisions[newest]
        distinct = {_segment_key(c) for c in candidates}
        if len(distinct) > 1:
            findings.append(
                Finding(
                    "source-version-conflict",
                    f"revision {newest} has {len(candidates)} conflicting records for {identity};"
                    " overlapping quality marked disputed",
                    source,
                    identity,
                    "error",
                )
            )
            resolved.extend(candidates)
            continue
        if len(candidates) > 1:
            findings.append(
                Finding(
                    "duplicate-delivery",
                    f"revision {newest} of {identity} was delivered {len(candidates)} times; counted once",
                    source,
                    identity,
                )
            )
        resolved.append(candidates[0])
    resolved.sort(key=lambda r: (r.start, r.end, r.id))
    return resolved


def _segment_key(row: Segment) -> tuple:
    return (
        row.session,
        row.site,
        row.network,
        row.start,
        row.end,
        row.quality,
        row.received,
    )


def load_sources(input_dir: str | Path) -> SourceData:
    root = Path(input_dir)
    raw: dict[str, list[dict]] = {}
    digests: dict[str, str] = {}
    for name in KNOWN_FILES:
        path = root / f"{name}.json"
        if not path.exists():
            continue
        digests[path.name] = _digest(path)
        payload = _read_json(path)
        if not isinstance(payload, list):
            raw.setdefault(name, [])
            continue
        raw[name] = [row for row in payload if isinstance(row, dict)]
    return build_sources(raw, digests)


def build_sources(raw: dict[str, list[dict]], digests: dict[str, str] | None = None) -> SourceData:
    """Build the typed snapshot from already-validated JSON rows.

    Workspaces persist rows in this shape so an incremental import can merge new
    facts without re-reading the read-only input directory.
    """

    data = SourceData()
    data.digests = dict(digests or {})
    for name, rows in raw.items():
        if not isinstance(rows, list):
            data.findings.append(Finding("invalid-file", "expected a JSON array", name, name, "error"))
            raw[name] = []
    _load_sites(data, raw.get("sites", []))
    _load_contracts(data, raw.get("contracts", []))
    _load_staff(data, raw.get("staff", []))
    _load_windows(data, raw.get("windows", []))
    _load_probes(data, raw.get("probes", []))
    _load_complaints(data, raw.get("complaints", []))
    _load_segments(data, raw.get("segments", []))
    _cross_check(data)
    return data


def _load_sites(data: SourceData, rows: list[dict]) -> None:
    for row in rows:
        identity = str(row.get("site", ""))
        site = _as_text(row.get("site"), "site", identity, "sites", data.findings)
        network = _as_text(row.get("network"), "network", identity, "sites", data.findings)
        contract = _as_text(row.get("contract"), "contract", identity, "sites", data.findings)
        region = _as_text(row.get("region"), "region", identity, "sites", data.findings)
        if not (site and network and contract and region):
            continue
        if site in data.sites:
            data.findings.append(Finding("duplicate-site", "site identity repeated", "sites", site, "error"))
            continue
        data.sites[site] = Site(site, network, contract, region)


def _load_contracts(data: SourceData, rows: list[dict]) -> None:
    for row in rows:
        identity = str(row.get("contract", ""))
        contract = _as_text(row.get("contract"), "contract", identity, "contracts", data.findings)
        min_quality = _as_float(row.get("min_quality"), "min_quality", identity, "contracts", data.findings)
        bad = _as_float(row.get("monthly_bad_minutes"), "monthly_bad_minutes", identity, "contracts", data.findings)
        restore = _as_float(row.get("restore_minutes"), "restore_minutes", identity, "contracts", data.findings)
        tier = _as_int(row.get("tier"), "tier", identity, "contracts", data.findings)
        if not contract or None in (min_quality, bad, restore, tier):
            continue
        if not (0 <= min_quality <= 100) or bad < 0 or restore < 0 or tier < 1:
            data.findings.append(
                Finding("invalid-range", "contract thresholds out of range", "contracts", identity, "error")
            )
            continue
        if contract in data.contracts:
            data.findings.append(Finding("duplicate-contract", "contract identity repeated", "contracts", contract, "error"))
            continue
        data.contracts[contract] = Contract(contract, min_quality, bad, restore, tier)


def _load_staff(data: SourceData, rows: list[dict]) -> None:
    for row in rows:
        identity = str(row.get("staff", ""))
        staff = _as_text(row.get("staff"), "staff", identity, "staff", data.findings)
        skills_raw = row.get("skills")
        skills = {s for s in skills_raw if isinstance(s, str)} if isinstance(skills_raw, list) else set()
        start = _timestamp(row.get("start"), "start", identity, "staff", data.findings)
        end = _timestamp(row.get("end"), "end", identity, "staff", data.findings)
        if not staff or start is None or end is None:
            continue
        if start >= end:
            data.findings.append(Finding("invalid-window", "staff availability ends before it starts", "staff", identity, "error"))
            continue
        data.staff.append(Staff(staff, frozenset(skills), start, end))
    data.staff.sort(key=lambda s: (s.staff, s.start))


def _load_windows(data: SourceData, rows: list[dict]) -> None:
    for row in rows:
        identity = str(row.get("id", ""))
        window_id = _as_text(row.get("id"), "id", identity, "windows", data.findings)
        site = _as_text(row.get("site"), "site", identity, "windows", data.findings)
        kind = _as_text(row.get("type"), "type", identity, "windows", data.findings)
        start = _timestamp(row.get("start"), "start", identity, "windows", data.findings)
        end = _timestamp(row.get("end"), "end", identity, "windows", data.findings)
        if not (window_id and site and kind and start and end):
            continue
        if kind not in {"course", "maintenance"}:
            data.findings.append(Finding("invalid-window-type", f"unknown window type {kind}", "windows", identity, "error"))
            continue
        if start >= end:
            data.findings.append(Finding("invalid-window", "window ends before it starts", "windows", identity, "error"))
            continue
        data.windows.append(Window(window_id, site, kind, start, end))


def _load_probes(data: SourceData, rows: list[dict]) -> None:
    for row in rows:
        identity = str(row.get("id", ""))
        probe_id = _as_text(row.get("id"), "id", identity, "probes", data.findings)
        network = _as_text(row.get("network"), "network", identity, "probes", data.findings)
        start = _timestamp(row.get("start"), "start", identity, "probes", data.findings)
        end = _timestamp(row.get("end"), "end", identity, "probes", data.findings)
        loss = _as_float(row.get("loss_pct"), "loss_pct", identity, "probes", data.findings)
        if not (probe_id and network and start and end) or loss is None:
            continue
        if start >= end:
            data.findings.append(Finding("invalid-window", "probe ends before it starts", "probes", identity, "error"))
            continue
        if loss < 0 or loss > 100:
            data.findings.append(Finding("invalid-range", "loss_pct outside 0..100", "probes", identity, "error"))
            continue
        data.probes.append(Probe(probe_id, network, start, end, loss))
    data.probes.sort(key=lambda p: (p.start, p.id))


def _load_complaints(data: SourceData, rows: list[dict]) -> None:
    for row in rows:
        identity = str(row.get("id", ""))
        complaint_id = _as_text(row.get("id"), "id", identity, "complaints", data.findings)
        site = _as_text(row.get("site"), "site", identity, "complaints", data.findings)
        occurred = _timestamp(row.get("occurred"), "occurred", identity, "complaints", data.findings)
        received = _timestamp(row.get("received"), "received", identity, "complaints", data.findings)
        symptom = _as_text(row.get("symptom"), "symptom", identity, "complaints", data.findings)
        if not (complaint_id and site and occurred and received and symptom):
            continue
        if received < occurred:
            data.findings.append(
                Finding("invalid-time-order", "complaint received before it occurred", "complaints", identity, "error")
            )
            continue
        data.complaints.append(Complaint(complaint_id, site, occurred, received, symptom))
    data.complaints.sort(key=lambda c: (c.received, c.id))


def _load_segments(data: SourceData, rows: list[dict]) -> None:
    parsed: list[Segment] = []
    for row in rows:
        identity = str(row.get("id", ""))
        segment_id = _as_text(row.get("id"), "id", identity, "segments", data.findings)
        revision = _as_int(row.get("revision"), "revision", identity, "segments", data.findings)
        session = _as_text(row.get("session"), "session", identity, "segments", data.findings)
        site = _as_text(row.get("site"), "site", identity, "segments", data.findings)
        network = _as_text(row.get("network"), "network", identity, "segments", data.findings)
        start = _timestamp(row.get("start"), "start", identity, "segments", data.findings)
        end = _timestamp(row.get("end"), "end", identity, "segments", data.findings)
        received = _timestamp(row.get("received"), "received", identity, "segments", data.findings)
        quality_value = row.get("quality")
        if quality_value is None:
            quality = None
        else:
            quality = _as_float(quality_value, "quality", identity, "segments", data.findings)
            if quality is not None and not (0 <= quality <= 100):
                data.findings.append(
                    Finding("invalid-range", "quality outside 0..100", "segments", identity, "error")
                )
                continue
        if not (segment_id and revision is not None and session and site and network):
            continue
        if start is None or end is None or received is None:
            continue
        if start >= end:
            data.findings.append(Finding("invalid-window", "segment ends before it starts", "segments", identity, "error"))
            continue
        parsed.append(Segment(segment_id, revision, session, site, network, start, end, quality, received))
    data.segments = resolve_segment_revisions(parsed, data.findings, "segments")


def _cross_check(data: SourceData) -> None:
    for segment in data.segments:
        site = data.sites.get(segment.site)
        if site is None:
            data.findings.append(
                Finding("unknown-site", f"segment references unknown site {segment.site}", "segments", segment.id, "error")
            )
        elif segment.network != site.network:
            data.findings.append(
                Finding(
                    "network-mismatch",
                    f"segment link {segment.network} is not the site default {site.network}",
                    "segments",
                    segment.id,
                )
            )
    for site in data.sites.values():
        if site.contract not in data.contracts:
            data.findings.append(
                Finding("unknown-contract", f"site {site.site} references unknown contract", "sites", site.site, "error")
            )
    for window in data.windows:
        if window.site not in data.sites:
            data.findings.append(
                Finding("unknown-site", f"window references unknown site {window.site}", "windows", window.id, "error")
            )
    for complaint in data.complaints:
        if complaint.site not in data.sites:
            data.findings.append(
                Finding("unknown-site", f"complaint references unknown site {complaint.site}", "complaints", complaint.id, "error")
            )
