"""Business workflows: intake, incidents, work orders, closure, reports.

The service keeps persisted workflow state in SQLite while play/quality facts
are always recomputed from the imported source rows.  Consequently a late
session correction changes analysis and priorities, but it never deletes a
repair that is already planned or executing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import db
from .analysis import (
    BAD,
    DEFAULT_LINK_WINDOW_MINUTES,
    DEFAULT_LOSS_THRESHOLD,
    DISPUTED,
    UNKNOWN,
    analyse_site,
    build_month_analysis,
    build_session_timelines,
    complaint_evidence,
    late_facts,
    restore_evidence,
    shared_network_candidates,
    site_priority,
)
from .interval import intersect, total_minutes
from .sources import SourceData, build_sources
from .timeutil import format_ts, now_utc, parse_ts

OPEN_SITE_STATUSES = {"open", "reopened", "awaiting_observation"}
CLOSED_SITE_STATUSES = {"closed"}
DISCIPLINES = {"network", "player", "site"}
ACTION_ACTIVE = {"planned", "in_progress"}


class WorkflowError(RuntimeError):
    """A user-facing refusal with a reason the operator can act on."""


# ---------------------------------------------------------------------------
# workspace helpers
# ---------------------------------------------------------------------------


def open_workspace(workspace: str | Path) -> db.sqlite3.Connection:
    return db.connect(workspace, create=False)


def init_workspace(workspace: str | Path, input_dir: str | Path, *, actor: str = "system") -> dict:
    root = db.initialise(workspace)
    connection = db.connect(root, create=True)
    try:
        result = import_inputs(connection, input_dir, actor=actor)
    finally:
        connection.close()
    return {"workspace": str(root), **result}


def import_inputs(connection: sqlite3.Connection, input_dir: str | Path, *, actor: str) -> dict:
    incoming = _load_from_disk(input_dir)
    rows_by_kind: dict[str, list[dict]] = {kind: [] for kind in db.KIND_KEYS}
    for kind in db.KIND_KEYS:
        path = Path(input_dir) / f"{kind}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                rows_by_kind[kind] = [row for row in payload if isinstance(row, dict)]
    result = db.import_rows(connection, rows_by_kind, incoming.digests, actor=actor, input_dir=str(input_dir))
    # Validate the merged workspace, not just the partial incoming file: an
    # incremental import may legitimately contain only one source kind.
    merged = source_data(connection)
    db.record_event(
        connection,
        actor=actor,
        kind="sources.imported",
        detail={**result, "input_dir": str(input_dir)},
    )
    connection.commit()
    return {
        **result,
        "findings": [finding.as_dict() for finding in merged.findings],
        "digests": dict(merged.digests),
    }


def _load_from_disk(input_dir: str | Path) -> SourceData:
    from .sources import load_sources

    return load_sources(input_dir)


def source_data(connection: sqlite3.Connection) -> SourceData:
    return build_sources(db.load_rows(connection), db.source_digests(connection))


def month_data(
    connection: sqlite3.Connection, month: str, as_of: str | datetime
) -> tuple[SourceData, dict, datetime]:
    moment = parse_ts(as_of, field="as_of") if isinstance(as_of, str) else parse_ts(as_of)
    data = source_data(connection)
    return data, build_month_analysis(data, month, moment), moment


def _get_incident(connection: sqlite3.Connection, incident_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        raise WorkflowError(f"整改单不存在: {incident_id}")
    return row


def _incident_sites(connection: sqlite3.Connection, incident_id: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM incident_sites WHERE incident_id = ? ORDER BY site", (incident_id,)
        )
    )


def _site_row(connection: sqlite3.Connection, incident_id: str, site: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM incident_sites WHERE incident_id = ? AND site = ?", (incident_id, site)
    ).fetchone()
    if row is None:
        raise WorkflowError(f"{incident_id} 不包含站点 {site}")
    return row


def _parse_sites(text: str | Iterable[str]) -> list[str]:
    if isinstance(text, str):
        values = [part.strip() for part in text.replace(";", ",").split(",")]
    else:
        values = [str(part).strip() for part in text]
    sites = sorted({value for value in values if value})
    if not sites:
        raise WorkflowError("至少需要一个站点")
    return sites


# ---------------------------------------------------------------------------
# incidents
# ---------------------------------------------------------------------------


def open_incident(
    connection: sqlite3.Connection,
    *,
    title: str,
    sites: str | Iterable[str],
    actor: str,
    network: str | None = None,
    discipline: str = "network",
    origin_type: str = "manual",
    origin_ref: str | None = None,
    complaints: Iterable[str] = (),
    note: str | None = None,
    impact_start: str | None = None,
    impact_end: str | None = None,
) -> str:
    site_list = _parse_sites(sites)
    data = source_data(connection)
    for site in site_list:
        if site not in data.sites:
            raise WorkflowError(f"未知站点: {site}")
    if discipline not in DISCIPLINES:
        raise WorkflowError(f"未知责任类型: {discipline}")
    if network:
        mismatched = [site for site in site_list if data.site_network(site) != network]
        if mismatched:
            raise WorkflowError(f"站点不在链路 {network}: {', '.join(mismatched)}")
    # Validate every requested complaint before writing anything, so a refused
    # link cannot leave a half-created incident in the transaction.
    complaint_list = list(complaints)
    verdicts = {}
    for complaint_id in complaint_list:
        complaint = next((row for row in data.complaints if row.id == complaint_id), None)
        if complaint is None:
            raise WorkflowError(f"投诉不存在: {complaint_id}")
        if complaint.site not in site_list:
            raise WorkflowError(f"投诉 {complaint_id} 的站点 {complaint.site} 不在整改单范围内")
        evidence = complaint_evidence(data, complaint, now_utc())
        if not evidence["sufficient"]:
            raise WorkflowError(
                f"投诉 {complaint_id} 关联依据不足，不能并入共享整改；请单独立案调查"
            )
        verdicts[complaint_id] = evidence
    incident_id = db.next_id(connection, "incidents", "INC")
    at = format_ts(now_utc())
    connection.execute(
        "INSERT INTO incidents(id, kind, title, network, status, origin_type, origin_ref, notes,"
        " opened_at, opened_by, created_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
        (incident_id, "shared" if len(site_list) > 1 else "site", title, network, origin_type, origin_ref, note, at, actor, at),
    )
    for site in site_list:
        contract = data.site_contract(site)
        connection.execute(
            "INSERT INTO incident_sites(incident_id, site, impact_start, impact_end, tier, discipline,"
            " status, opened_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (
                incident_id,
                site,
                format_ts(parse_ts(impact_start, field="impact_start")) if impact_start else None,
                format_ts(parse_ts(impact_end, field="impact_end")) if impact_end else None,
                contract.tier if contract else None,
                discipline,
                at,
            ),
        )
    for complaint_id in complaint_list:
        _link_complaint(connection, complaint_id, incident_id, actor=actor, kind="initial", at=at)
        _set_complaint_status(connection, complaint_id, "linked", f"关联 {incident_id}", actor)
    db.record_event(
        connection,
        actor=actor,
        kind="incident.opened",
        detail={"title": title, "sites": site_list, "network": network, "origin": origin_type},
        incident_id=incident_id,
    )
    connection.commit()
    return incident_id


def open_incident_from_shared(
    connection: sqlite3.Connection,
    *,
    network: str,
    actor: str,
    as_of: str | datetime,
    complaints: Iterable[str] = (),
    title: str | None = None,
) -> str:
    moment = parse_ts(as_of, field="as_of") if isinstance(as_of, str) else parse_ts(as_of)
    data = source_data(connection)
    candidates = [
        candidate
        for candidate in shared_network_candidates(data, moment)
        if candidate["network"] == network
    ]
    if not candidates:
        raise WorkflowError(f"链路 {network} 没有达到阈值的共享故障探测证据")
    sites = candidates[0]["sites"]
    return open_incident(
        connection,
        title=title or f"{network} 共享链路故障",
        sites=sites,
        actor=actor,
        network=network,
        origin_type="shared_network",
        origin_ref=candidates[0]["probe"],
        complaints=complaints,
    )


def open_incident_from_complaint(
    connection: sqlite3.Connection,
    *,
    complaint_id: str,
    actor: str,
    as_of: str | datetime,
    window_minutes: float = DEFAULT_LINK_WINDOW_MINUTES,
    loss_threshold: float = DEFAULT_LOSS_THRESHOLD,
    title: str | None = None,
) -> str:
    moment = parse_ts(as_of, field="as_of") if isinstance(as_of, str) else parse_ts(as_of)
    data = source_data(connection)
    complaint = next((row for row in data.complaints if row.id == complaint_id), None)
    if complaint is None:
        raise WorkflowError(f"投诉不存在: {complaint_id}")
    if complaint.received > moment:
        raise WorkflowError("投诉在 as_of 之后才进入系统，不能在此之前使用")
    evidence = complaint_evidence(
        data, complaint, moment, window_minutes=window_minutes, loss_threshold=loss_threshold
    )
    if evidence["sufficient"]:
        raise WorkflowError(
            "投诉已有可核实关联依据，应并入对应整改单而不是单独立案"
        )
    incident_id = open_incident(
        connection,
        title=title or f"{complaint.site} 投诉单独调查 {complaint.id}",
        sites=[complaint.site],
        actor=actor,
        discipline="site" if complaint.symptom == "audio" else "network",
        origin_type="complaint",
        origin_ref=complaint.id,
        note="关联依据不足，单独立案调查。",
    )
    _set_complaint_status(connection, complaint_id, "investigating", f"单独立案 {incident_id}", actor)
    connection.commit()
    return incident_id


def list_incidents(connection: sqlite3.Connection, *, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM incidents"
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY opened_at, id"
    result = []
    for row in connection.execute(query, params):
        sites = _incident_sites(connection, row["id"])
        result.append(
            {
                **dict(row),
                "sites": [
                    {
                        "site": site["site"],
                        "status": site["status"],
                        "discipline": site["discipline"],
                        "tier": site["tier"],
                        "evidence_gap": site["evidence_gap"],
                    }
                    for site in sites
                ],
            }
        )
    return result


def incident_detail(
    connection: sqlite3.Connection,
    incident_id: str,
    *,
    month: str,
    as_of: str | datetime,
    window_minutes: float = DEFAULT_LINK_WINDOW_MINUTES,
    loss_threshold: float = DEFAULT_LOSS_THRESHOLD,
) -> dict:
    incident = _get_incident(connection, incident_id)
    data, analysis, moment = month_data(connection, month, as_of)
    site_records = {record["site"]: record for record in analysis["sites"]}
    sites = []
    for site_row in _incident_sites(connection, incident_id):
        site = site_row["site"]
        timeline = build_session_timelines(data, site, moment)
        analysis_record = site_records.get(site)
        action_rows = list(
            connection.execute(
                "SELECT a.*, s.discipline AS site_discipline, s.status AS site_status,"
                " s.cause AS site_cause, s.repair_scope AS site_scope, s.completed_at AS site_completed_at,"
                " s.completed_by AS site_completed_by, s.actual_minutes AS site_actual_minutes"
                " FROM action_sites s JOIN actions a ON a.id = s.action_id"
                " WHERE s.incident_id = ? AND s.site = ? ORDER BY a.planned_start, a.id",
                (incident_id, site),
            )
        )
        observations = [
            {**dict(row), "evidence": json.loads(row["evidence"])}
            for row in connection.execute(
                "SELECT * FROM observations WHERE incident_id = ? AND site = ? ORDER BY checked_at, id",
                (incident_id, site),
            )
        ]
        linked = []
        for link in connection.execute(
            "SELECT l.*, c.payload AS complaint_payload FROM complaint_links l"
            " LEFT JOIN source_rows c ON c.kind = 'complaints' AND c.identity = l.complaint_id"
            " WHERE l.incident_id = ? ORDER BY l.linked_at",
            (incident_id,),
        ):
            payload = json.loads(link["complaint_payload"] or "{}")
            if payload.get("site") != site:
                continue
            linked.append(
                {
                    "complaint_id": link["complaint_id"],
                    "incident_id": link["incident_id"],
                    "basis": link["basis"],
                    "link_kind": link["link_kind"],
                    "linked_at": link["linked_at"],
                    "linked_by": link["linked_by"],
                    "linked_after_close": link["linked_after_close"],
                    "site": payload.get("site"),
                    "occurred": payload.get("occurred"),
                    "received": payload.get("received"),
                    "symptom": payload.get("symptom"),
                }
            )
        evidence = _incident_site_evidence(data, site, site_row, moment, window_minutes, loss_threshold)
        sites.append(
            {
                "site": site,
                "status": site_row["status"],
                "discipline": site_row["discipline"],
                "tier": site_row["tier"],
                "impact_start": site_row["impact_start"],
                "impact_end": site_row["impact_end"],
                "evidence_gap": site_row["evidence_gap"],
                "complaints": linked,
                "evidence": evidence,
                "contract": analysis_record,
                "actions": [
                    {
                        "id": action["id"],
                        "assignee": action["assignee"],
                        "discipline": action["discipline"],
                        "planned_start": action["planned_start"],
                        "planned_end": action["planned_end"],
                        "status": action["status"],
                        "site_status": action["site_status"],
                        "cause": action["site_cause"],
                        "repair_scope": action["site_scope"],
                        "actual_minutes": action["site_actual_minutes"],
                        "completed_at": action["site_completed_at"],
                        "completed_by": action["site_completed_by"],
                    }
                    for action in action_rows
                ],
                "observations": observations,
                "pending_observation": site_row["status"] in OPEN_SITE_STATUSES,
                "sessions": [
                    timeline.as_dict()
                    for timeline in timeline
                    if _timeline_relevant(timeline, site_row)
                ],
            }
        )
    handovers = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM handovers WHERE incident_id = ? ORDER BY created_at, id", (incident_id,)
        )
    ]
    return {
        **dict(incident),
        "month": month,
        "as_of": moment.isoformat(),
        "sites": sites,
        "handovers": handovers,
        "events": [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM events WHERE incident_id = ? ORDER BY at, id", (incident_id,)
            )
        ],
    }


def _timeline_relevant(timeline, site_row: sqlite3.Row) -> bool:
    if site_row["impact_start"] and site_row["impact_end"]:
        return (
            timeline.span_start < parse_ts(site_row["impact_end"])
            and timeline.span_end > parse_ts(site_row["impact_start"])
        )
    return True


def _incident_site_evidence(
    data: SourceData,
    site: str,
    site_row: sqlite3.Row,
    moment: datetime,
    window_minutes: float,
    loss_threshold: float,
) -> dict:
    network = data.site_network(site)
    probes = [
        probe
        for probe in data.probes
        if probe.network == network
        and probe.loss_pct >= loss_threshold
        and probe.start <= moment
    ]
    timeline = build_session_timelines(data, site, moment)
    bad_sessions = []
    for entry in timeline:
        bad_minutes = entry.totals.get(BAD, 0.0)
        if bad_minutes:
            bad_sessions.append(
                {
                    "session": entry.session,
                    "bad_minutes": round(bad_minutes, 3),
                    "observed_minutes": round(total_minutes(entry.observed), 3),
                    "gaps": [
                        {"start": gap[0].isoformat(), "end": gap[1].isoformat()}
                        for gap in entry.gaps
                    ],
                    "other_sites": entry.other_sites,
                    "networks": entry.networks,
                }
            )
    return {
        "network": network,
        "probes": [
            {
                "id": probe.id,
                "start": probe.start.isoformat(),
                "end": probe.end.isoformat(),
                "loss_pct": probe.loss_pct,
            }
            for probe in probes
        ],
        "bad_sessions": bad_sessions[:50],
        "unknown_or_gap": {
            "sessions_with_gaps": sum(1 for entry in timeline if entry.gaps),
            "sampling_gap_minutes": round(sum(total_minutes(entry.gaps) for entry in timeline), 3),
            "unknown_minutes": round(
                sum(entry.totals.get(UNKNOWN, 0.0) + entry.totals.get(DISPUTED, 0.0) for entry in timeline),
                3,
            ),
        },
        "note": "探测是链路辅助证据，不替代站点播放体验；站点证据缺口单独保留。",
        "loss_threshold_pct": loss_threshold,
        "complaint_window_minutes": window_minutes,
    }


def set_incident_site_discipline(
    connection: sqlite3.Connection, incident_id: str, site: str, discipline: str, *, actor: str
) -> None:
    if discipline not in DISCIPLINES:
        raise WorkflowError(f"未知责任类型: {discipline}")
    _site_row(connection, incident_id, site)
    connection.execute(
        "UPDATE incident_sites SET discipline = ? WHERE incident_id = ? AND site = ?",
        (discipline, incident_id, site),
    )
    db.record_event(
        connection,
        actor=actor,
        kind="incident.site_discipline",
        detail={"site": site, "discipline": discipline},
        incident_id=incident_id,
    )
    connection.commit()


# ---------------------------------------------------------------------------
# complaints
# ---------------------------------------------------------------------------


def _set_complaint_status(connection: sqlite3.Connection, complaint_id: str, status: str, note: str, actor: str) -> None:
    connection.execute(
        "INSERT INTO complaints_state(complaint_id, status, note, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(complaint_id) DO UPDATE SET status = excluded.status, note = excluded.note,"
        " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (complaint_id, status, note, format_ts(now_utc()), actor),
    )


def _link_complaint(
    connection: sqlite3.Connection,
    complaint_id: str,
    incident_id: str,
    *,
    actor: str,
    kind: str,
    at: str | None = None,
    basis: str = "evidence",
) -> None:
    incident = _get_incident(connection, incident_id)
    linked_at = at or format_ts(now_utc())
    closed = incident["status"] == "closed"
    connection.execute(
        "INSERT OR REPLACE INTO complaint_links(complaint_id, incident_id, basis, link_kind,"
        " linked_at, linked_by, linked_after_close) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (complaint_id, incident_id, basis, kind, linked_at, actor, 1 if closed else 0),
    )
    if closed:
        db.record_event(
            connection,
            actor=actor,
            kind="complaint.linked_after_close",
            detail={"complaint": complaint_id, "incident": incident_id},
            incident_id=incident_id,
        )


def link_complaint(
    connection: sqlite3.Connection,
    *,
    complaint_id: str,
    incident_id: str,
    actor: str,
    as_of: str | datetime,
    window_minutes: float = DEFAULT_LINK_WINDOW_MINUTES,
    loss_threshold: float = DEFAULT_LOSS_THRESHOLD,
) -> dict:
    moment = parse_ts(as_of, field="as_of") if isinstance(as_of, str) else parse_ts(as_of)
    data = source_data(connection)
    complaint = next((row for row in data.complaints if row.id == complaint_id), None)
    if complaint is None:
        raise WorkflowError(f"投诉不存在: {complaint_id}")
    if complaint.received > moment:
        raise WorkflowError("投诉在 as_of 之后才进入系统，不能在此之前关联")
    incident = _get_incident(connection, incident_id)
    incident_sites = {row["site"] for row in _incident_sites(connection, incident_id)}
    if complaint.site not in incident_sites:
        raise WorkflowError(f"整改单 {incident_id} 不包含投诉站点 {complaint.site}")
    evidence = complaint_evidence(
        data, complaint, moment, window_minutes=window_minutes, loss_threshold=loss_threshold
    )
    if not evidence["sufficient"]:
        raise WorkflowError(
            "关联依据不足：投诉仅时间接近，不能为了少派单强行合并；请单独立案调查"
        )
    already = connection.execute(
        "SELECT 1 FROM complaint_links WHERE complaint_id = ? AND incident_id = ?",
        (complaint_id, incident_id),
    ).fetchone()
    if already:
        return {"complaint": complaint_id, "incident": incident_id, "changed": False, "evidence": evidence}
    _link_complaint(
        connection,
        complaint_id,
        incident_id,
        actor=actor,
        kind="evidence",
        basis="+".join(evidence["basis"]),
    )
    _set_complaint_status(connection, complaint_id, "linked", f"关联 {incident_id}", actor)
    reopened = _reopen_closed_site_after_late_complaint(
        connection, incident, complaint.site, actor=actor, reason=f"关闭后补交投诉 {complaint_id}"
    )
    db.record_event(
        connection,
        actor=actor,
        kind="complaint.linked",
        detail={"complaint": complaint_id, "basis": evidence["basis"], "reopened_site": reopened},
        incident_id=incident_id,
    )
    connection.commit()
    return {
        "complaint": complaint_id,
        "incident": incident_id,
        "changed": True,
        "evidence": evidence,
        "reopened_site": reopened,
    }


def _reopen_closed_site_after_late_complaint(
    connection: sqlite3.Connection, incident: sqlite3.Row, site: str, *, actor: str, reason: str
) -> bool:
    row = connection.execute(
        "SELECT * FROM incident_sites WHERE incident_id = ? AND site = ?", (incident["id"], site)
    ).fetchone()
    if row is None:
        return False
    changed = False
    if row["status"] in CLOSED_SITE_STATUSES:
        connection.execute(
            "UPDATE incident_sites SET status = 'reopened', reopened_at = ? WHERE incident_id = ? AND site = ?",
            (format_ts(now_utc()), incident["id"], site),
        )
        changed = True
    if incident["status"] == "closed":
        connection.execute(
            "UPDATE incidents SET status = 'open', closed_at = NULL WHERE id = ?", (incident["id"],)
        )
        changed = True
    if changed:
        db.record_event(
            connection,
            actor=actor,
            kind="incident.site_reopened",
            detail={"site": site, "reason": reason},
            incident_id=incident["id"],
        )
    return changed


def complaint_list(
    connection: sqlite3.Connection,
    *,
    as_of: str | datetime,
    window_minutes: float = DEFAULT_LINK_WINDOW_MINUTES,
    loss_threshold: float = DEFAULT_LOSS_THRESHOLD,
) -> list[dict]:
    moment = parse_ts(as_of, field="as_of") if isinstance(as_of, str) else parse_ts(as_of)
    data = source_data(connection)
    result = []
    for complaint in data.complaints:
        if complaint.received > moment:
            continue
        evidence = complaint_evidence(
            data, complaint, moment, window_minutes=window_minutes, loss_threshold=loss_threshold
        )
        state = connection.execute(
            "SELECT status, note FROM complaints_state WHERE complaint_id = ?", (complaint.id,)
        ).fetchone()
        links = [
            dict(row)
            for row in connection.execute(
                "SELECT incident_id, basis, link_kind, linked_after_close FROM complaint_links"
                " WHERE complaint_id = ? ORDER BY linked_at",
                (complaint.id,),
            )
        ]
        result.append(
            {
                "id": complaint.id,
                "site": complaint.site,
                "occurred": complaint.occurred.isoformat(),
                "received": complaint.received.isoformat(),
                "symptom": complaint.symptom,
                "status": state["status"] if state else "new",
                "note": state["note"] if state else None,
                "links": links,
                "evidence": evidence,
            }
        )
    return result


def close_complaint(connection: sqlite3.Connection, *, complaint_id: str, actor: str, note: str) -> dict:
    """Close the complaint *ticket*.

    This is explicitly not a service restoration.  Any linked incident keeps its
    own evidence and closure requirements; the ticket closure is recorded and
    the report still measures whether playback recovered.
    """

    data = source_data(connection)
    complaint = next((row for row in data.complaints if row.id == complaint_id), None)
    if complaint is None:
        raise WorkflowError(f"投诉不存在: {complaint_id}")
    _set_complaint_status(connection, complaint_id, "closed_ticket", note, actor)
    links = [
        dict(row)
        for row in connection.execute(
            "SELECT incident_id FROM complaint_links WHERE complaint_id = ?", (complaint_id,)
        )
    ]
    db.record_event(
        connection,
        actor=actor,
        kind="complaint.ticket_closed",
        detail={"complaint": complaint_id, "note": note, "service_restoration_claimed": False},
        incident_id=links[0]["incident_id"] if links else None,
    )
    connection.commit()
    return {
        "complaint": complaint_id,
        "ticket_status": "closed_ticket",
        "service_restoration_claimed": False,
        "linked_incidents": [link["incident_id"] for link in links],
        "note": "投诉工单关闭不等于服务恢复；整改单仍须满足修复后的有效观察。",
    }


# ---------------------------------------------------------------------------
# work orders and scheduling
# ---------------------------------------------------------------------------


def _staff_available(data: SourceData, assignee: str, start: datetime, end: datetime, discipline: str) -> bool:
    for row in data.staff:
        if (
            row.staff == assignee
            and discipline in row.skills
            and row.start <= start
            and row.end >= end
        ):
            return True
    return False


def _occupied(connection: sqlite3.Connection, assignee: str) -> list[tuple[datetime, datetime]]:
    """Person-time already committed.

    A completed repair still occupied its booked slot, so both the planned and
    the recorded actual interval are held; otherwise a later booking could be
    slipped into the gap between them.
    """

    intervals: list[tuple[datetime, datetime]] = []
    for row in connection.execute(
        "SELECT * FROM actions WHERE assignee = ? AND status IN ('planned', 'in_progress', 'completed')",
        (assignee,),
    ):
        intervals.append((parse_ts(row["planned_start"]), parse_ts(row["planned_end"])))
        if row["status"] == "completed" and row["actual_start"] and row["actual_end"]:
            intervals.append((parse_ts(row["actual_start"]), parse_ts(row["actual_end"])))
    return intervals


def plan_action(
    connection: sqlite3.Connection,
    *,
    incident_id: str,
    discipline: str,
    assignee: str,
    start: str | datetime,
    minutes: float = 30.0,
    scope: str = "",
    actor: str,
    site: str | None = None,
    sites: Iterable[str] | None = None,
) -> dict:
    """Plan one repair, optionally covering several sites of a shared fault.

    A shared fault may reuse a single work order.  The work order is only a
    scheduling container: each site keeps its own completion, cause and scope,
    so completing one responsibility never marks another site complete.
    """

    _get_incident(connection, incident_id)
    site_list = _parse_sites(list(sites) if sites else site) if (sites or site) else []
    if not site_list:
        raise WorkflowError("工作安排至少需要一个站点")
    for one_site in site_list:
        site_row = _site_row(connection, incident_id, one_site)
        if site_row["status"] == "closed":
            raise WorkflowError(f"{one_site} 的整改责任已关闭，不能在其上安排新修复")
    if discipline not in DISCIPLINES:
        raise WorkflowError(f"未知责任类型: {discipline}")
    if minutes <= 0:
        raise WorkflowError("修复时长必须为正数")
    start_dt = parse_ts(start, field="start") if isinstance(start, str) else parse_ts(start)
    end_dt = start_dt + timedelta(minutes=minutes)
    data = source_data(connection)
    if not _staff_available(data, assignee, start_dt, end_dt, discipline):
        raise WorkflowError(
            f"{assignee} 在 {format_ts(start_dt)} 起的 {minutes} 分钟没有 {discipline} 可用时段"
        )
    for busy_start, busy_end in _occupied(connection, assignee):
        if intersect((start_dt, end_dt), (busy_start, busy_end)):
            raise WorkflowError(
                f"{assignee} 已占用 {format_ts(busy_start)}..{format_ts(busy_end)}，不能重叠分派"
            )
    action_id = db.next_id(connection, "actions", "ACT")
    at = format_ts(now_utc())
    connection.execute(
        "INSERT INTO actions(id, discipline, assignee, planned_start, planned_end, planned_minutes,"
        " status, notes, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)",
        (action_id, discipline, assignee, format_ts(start_dt), format_ts(end_dt), minutes, scope or None, at, actor),
    )
    for one_site in site_list:
        connection.execute(
            "INSERT INTO action_sites(action_id, incident_id, site, discipline, status)"
            " VALUES (?, ?, ?, ?, 'planned')",
            (action_id, incident_id, one_site, discipline),
        )
    db.record_event(
        connection,
        actor=actor,
        kind="action.planned",
        detail={
            "assignee": assignee,
            "discipline": discipline,
            "start": format_ts(start_dt),
            "minutes": minutes,
            "sites": site_list,
        },
        incident_id=incident_id,
        action_id=action_id,
    )
    connection.commit()
    return {
        "action": action_id,
        "assignee": assignee,
        "start": format_ts(start_dt),
        "end": format_ts(end_dt),
        "sites": site_list,
    }


def start_action(connection: sqlite3.Connection, *, action_id: str, actor: str) -> dict:
    row = connection.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        raise WorkflowError(f"工作安排不存在: {action_id}")
    if row["status"] not in ACTION_ACTIVE:
        raise WorkflowError(f"{action_id} 当前状态 {row['status']}，不能开始")
    connection.execute(
        "UPDATE actions SET status = 'in_progress' WHERE id = ?", (action_id,)
    )
    connection.execute(
        "UPDATE action_sites SET status = 'in_progress' WHERE action_id = ?", (action_id,)
    )
    db.record_event(
        connection,
        actor=actor,
        kind="action.started",
        detail={"assignee": row["assignee"]},
        action_id=action_id,
    )
    connection.commit()
    return {"action": action_id, "status": "in_progress"}


def complete_action(
    connection: sqlite3.Connection,
    *,
    action_id: str,
    site: str,
    actor: str,
    cause: str,
    repair_scope: str,
    actual_minutes: float | None = None,
    completed_at: str | datetime | None = None,
) -> dict:
    action = connection.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    if action is None:
        raise WorkflowError(f"工作安排不存在: {action_id}")
    link = connection.execute(
        "SELECT * FROM action_sites WHERE action_id = ? AND site = ?", (action_id, site)
    ).fetchone()
    if link is None:
        raise WorkflowError(f"{action_id} 不包含站点 {site}")
    if link["status"] == "completed":
        raise WorkflowError(f"{site} 在该工作安排上已完成，不能重复计量")
    data = source_data(connection)
    finished = (
        parse_ts(completed_at, field="completed_at") if isinstance(completed_at, str) else
        (parse_ts(completed_at) if completed_at else now_utc())
    )
    actual = float(actual_minutes) if actual_minutes is not None else float(action["planned_minutes"])
    if actual <= 0:
        raise WorkflowError("实际耗时必须为正数")
    # A repair completion is not a service restoration.  Observation is checked
    # separately and only counts playback that began after this completion.
    as_of = max(now_utc(), finished)
    evidence = restore_evidence(data, site, finished, as_of)
    gap = None if evidence.sufficient else evidence.as_dict()["status"]
    actual_start = finished - timedelta(minutes=actual)
    connection.execute(
        "UPDATE action_sites SET status = 'completed', cause = ?, repair_scope = ?, completed_at = ?,"
        " completed_by = ?, actual_minutes = ? WHERE action_id = ? AND site = ?",
        (cause, repair_scope, format_ts(finished), actor, actual, action_id, site),
    )
    remaining = connection.execute(
        "SELECT COUNT(*) AS count FROM action_sites WHERE action_id = ? AND status != 'completed'",
        (action_id,),
    ).fetchone()["count"]
    if remaining == 0:
        connection.execute(
            "UPDATE actions SET status = 'completed', cause = ?, repair_scope = ?, actual_minutes = ?,"
            " actual_start = ?, actual_end = ?, completed_at = ?, completed_by = ? WHERE id = ?",
            (
                cause,
                repair_scope,
                actual,
                format_ts(actual_start),
                format_ts(finished),
                format_ts(finished),
                actor,
                action_id,
            ),
        )
    connection.execute(
        "UPDATE incident_sites SET status = 'awaiting_observation', evidence_gap = ?"
        " WHERE incident_id = ? AND site = ? AND status != 'closed'",
        (gap, link["incident_id"], site),
    )
    db.record_event(
        connection,
        actor=actor,
        kind="action.site_completed",
        detail={
            "site": site,
            "cause": cause,
            "repair_scope": repair_scope,
            "actual_minutes": actual,
            "completed_at": format_ts(finished),
        },
        incident_id=link["incident_id"],
        action_id=action_id,
    )
    connection.commit()
    return {
        "action": action_id,
        "site": site,
        "status": "completed",
        "actual_minutes": actual,
        "action_status": "completed" if remaining == 0 else "partially_completed",
        "note": "只完成本工作安排覆盖的站点责任；共享单号的其他站点不因此完成。",
    }


def observation_check(
    connection: sqlite3.Connection,
    *,
    incident_id: str,
    site: str,
    since: str | datetime,
    as_of: str | datetime,
    actor: str,
    record: bool = True,
) -> dict:
    incident = _get_incident(connection, incident_id)
    _site_row(connection, incident_id, site)
    since_dt = parse_ts(since, field="since") if isinstance(since, str) else parse_ts(since)
    as_of_dt = parse_ts(as_of, field="as_of") if isinstance(as_of, str) else parse_ts(as_of)
    if as_of_dt <= since_dt:
        raise WorkflowError("观察截止必须晚于观察起点")
    data = source_data(connection)
    evidence = restore_evidence(data, site, since_dt, as_of_dt)
    sufficient = evidence.sufficient
    outcome = "sufficient" if sufficient else "waiting"
    if record:
        connection.execute(
            "INSERT INTO observations(incident_id, site, checked_at, since, sufficient, evidence, outcome, actor)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                incident_id,
                site,
                format_ts(as_of_dt),
                format_ts(since_dt),
                1 if sufficient else 0,
                json.dumps(evidence.as_dict(), ensure_ascii=False),
                outcome,
                actor,
            ),
        )
        if not sufficient and incident["status"] != "closed":
            connection.execute(
                "UPDATE incident_sites SET status = 'awaiting_observation' WHERE incident_id = ? AND site = ?"
                " AND status != 'closed'",
                (incident_id, site),
            )
        db.record_event(
            connection,
            actor=actor,
            kind="observation.checked",
            detail={"site": site, "since": format_ts(since_dt), "as_of": format_ts(as_of_dt), "outcome": outcome},
            incident_id=incident_id,
        )
        connection.commit()
    return {
        "incident": incident_id,
        "site": site,
        **evidence.as_dict(),
        "outcome": outcome,
    }


def close_incident_site(
    connection: sqlite3.Connection,
    *,
    incident_id: str,
    site: str,
    since: str | datetime,
    as_of: str | datetime,
    actor: str,
) -> dict:
    incident = _get_incident(connection, incident_id)
    site_row = _site_row(connection, incident_id, site)
    if site_row["status"] == "closed":
        raise WorkflowError(f"{site} 已关闭")
    pending = connection.execute(
        "SELECT COUNT(*) AS count FROM action_sites WHERE incident_id = ? AND site = ?"
        " AND status != 'completed'",
        (incident_id, site),
    ).fetchone()["count"]
    if pending:
        raise WorkflowError(f"{site} 仍有 {pending} 项修复责任未完成，不能关闭")
    repairs = connection.execute(
        "SELECT completed_at FROM action_sites WHERE incident_id = ? AND site = ?"
        " AND status = 'completed' AND completed_at IS NOT NULL",
        (incident_id, site),
    ).fetchall()
    if not repairs:
        raise WorkflowError(f"{site} 没有已完成的修复，不能以观察证据关闭")
    last_repair = max(parse_ts(row["completed_at"]) for row in repairs)
    since_dt = parse_ts(since, field="since") if isinstance(since, str) else parse_ts(since)
    if since_dt < last_repair:
        raise WorkflowError(
            "观察起点早于最近一次修复完成时间，修复前的播放不能作为恢复证据；"
            f"请从 {format_ts(last_repair)} 起观察"
        )
    evidence = observation_check(
        connection,
        incident_id=incident_id,
        site=site,
        since=since,
        as_of=as_of,
        actor=actor,
    )
    if not evidence["sufficient"]:
        raise WorkflowError(
            f"恢复观察不足：{evidence['new_good_minutes']} 分钟新增良好播放，"
            f"需要 {evidence['required_good_minutes']} 分钟；只能等候确认"
        )
    report = {
        "closed_at": format_ts(now_utc()),
        "since": evidence["since"],
        "as_of": evidence["as_of"],
        "new_good_minutes": evidence["new_good_minutes"],
        "required_good_minutes": evidence["required_good_minutes"],
        "observation": evidence,
    }
    connection.execute(
        "UPDATE incident_sites SET status = 'closed', closed_at = ?, closure_report = ?"
        " WHERE incident_id = ? AND site = ?",
        (report["closed_at"], json.dumps(report, ensure_ascii=False), incident_id, site),
    )
    db.record_event(
        connection,
        actor=actor,
        kind="incident.site_closed",
        detail={"site": site, "new_good_minutes": evidence["new_good_minutes"]},
        incident_id=incident_id,
    )
    open_sites = connection.execute(
        "SELECT COUNT(*) AS count FROM incident_sites WHERE incident_id = ? AND status != 'closed'",
        (incident_id,),
    ).fetchone()["count"]
    if open_sites == 0:
        connection.execute(
            "UPDATE incidents SET status = 'closed', closed_at = ? WHERE id = ?",
            (report["closed_at"], incident_id),
        )
        db.record_event(
            connection, actor=actor, kind="incident.closed", detail={"sites": "all"}, incident_id=incident_id
        )
    else:
        connection.execute(
            "UPDATE incidents SET status = 'partially_closed' WHERE id = ? AND status = 'open'", (incident_id,)
        )
    connection.commit()
    return {"incident": incident_id, "site": site, "status": "closed", "report": report}


def reopen_incident_site(
    connection: sqlite3.Connection, *, incident_id: str, site: str, actor: str, reason: str
) -> dict:
    _get_incident(connection, incident_id)
    row = _site_row(connection, incident_id, site)
    if row["status"] != "closed":
        raise WorkflowError(f"{site} 当前状态 {row['status']}，不需要重新开启")
    connection.execute(
        "UPDATE incident_sites SET status = 'reopened', reopened_at = ? WHERE incident_id = ? AND site = ?",
        (format_ts(now_utc()), incident_id, site),
    )
    connection.execute(
        "UPDATE incidents SET status = 'open', closed_at = NULL WHERE id = ?", (incident_id,)
    )
    db.record_event(
        connection,
        actor=actor,
        kind="incident.site_reopened",
        detail={"site": site, "reason": reason},
        incident_id=incident_id,
    )
    connection.commit()
    return {"incident": incident_id, "site": site, "status": "reopened", "reason": reason}


def split_incident(
    connection: sqlite3.Connection,
    *,
    incident_id: str,
    sites: str | Iterable[str],
    actor: str,
    title: str | None = None,
    as_of: str | datetime,
) -> dict:
    """Split sites out of a presumed shared fault.

    Existing action links for the moved sites are *re-pointed*, never copied:
    completed field work keeps its duration and evidence, so it cannot become
    fresh workload.  Complaint links are re-evaluated and affected site
    responsibilities are re-opened for observation.
    """

    original = _get_incident(connection, incident_id)
    moved = _parse_sites(sites)
    remaining = [row["site"] for row in _incident_sites(connection, incident_id) if row["site"] not in moved]
    if not remaining:
        raise WorkflowError("不能拆出全部站点；至少保留一个站点在原整改单")
    for site in moved:
        _site_row(connection, incident_id, site)
    moment = parse_ts(as_of, field="as_of") if isinstance(as_of, str) else parse_ts(as_of)
    data = source_data(connection)
    new_id = db.next_id(connection, "incidents", "INC")
    at = format_ts(now_utc())
    connection.execute(
        "INSERT INTO incidents(id, kind, title, network, status, origin_type, origin_ref, notes, split_from,"
        " opened_at, opened_by, created_at) VALUES (?, ?, ?, ?, 'open', 'split', ?, ?, ?, ?, ?, ?)",
        (
            new_id,
            "site" if len(moved) == 1 else "group",
            title or f"{original['title']}（拆出）",
            original["network"],
            incident_id,
            f"由 {incident_id} 拆出",
            original["split_from"] or incident_id,
            at,
            actor,
            at,
        ),
    )
    for site in moved:
        old = _site_row(connection, incident_id, site)
        connection.execute(
            "INSERT INTO incident_sites(incident_id, site, impact_start, impact_end, tier, discipline,"
            " status, evidence_gap, opened_at) VALUES (?, ?, ?, ?, ?, ?, 'reopened', ?, ?)",
            (
                new_id,
                site,
                old["impact_start"],
                old["impact_end"],
                old["tier"],
                old["discipline"],
                old["evidence_gap"],
                at,
            ),
        )
        # re-point existing work, preserving actual duration and completion
        connection.execute(
            "UPDATE action_sites SET incident_id = ? WHERE incident_id = ? AND site = ?",
            (new_id, incident_id, site),
        )
        connection.execute(
            "DELETE FROM incident_sites WHERE incident_id = ? AND site = ?", (incident_id, site)
        )
        # complaints that belong to the moved site follow the site it actually concerns
        for link in list(
            connection.execute(
                "SELECT * FROM complaint_links WHERE incident_id = ?", (incident_id,)
            )
        ):
            payload = connection.execute(
                "SELECT payload FROM source_rows WHERE kind = 'complaints' AND identity = ?",
                (link["complaint_id"],),
            ).fetchone()
            if payload and json.loads(payload["payload"]).get("site") == site:
                connection.execute(
                    "UPDATE complaint_links SET incident_id = ? WHERE complaint_id = ? AND incident_id = ?",
                    (new_id, link["complaint_id"], incident_id),
                )
        db.record_event(
            connection,
            actor=actor,
            kind="incident.site_split",
            detail={"site": site, "from": incident_id, "to": new_id},
            incident_id=new_id,
        )
    # re-check complaint linkage for the moved sites at split time; a complaint
    # that loses its supporting evidence on the new incident is flagged, and a
    # complaint tied primarily to a *remaining* site is not carried along.
    recheck = []
    for complaint_id in {
        link["complaint_id"]
        for link in connection.execute(
            "SELECT complaint_id FROM complaint_links WHERE incident_id = ?", (new_id,)
        )
    }:
        complaint = next((row for row in data.complaints if row.id == complaint_id), None)
        if complaint is None or complaint.received > moment:
            continue
        evidence = complaint_evidence(data, complaint, moment)
        recheck.append(
            {
                "complaint": complaint.id,
                "site": complaint.site,
                "still_supported": evidence["sufficient"],
                "basis": evidence["basis"],
                "action": "keep" if evidence["sufficient"] else "investigate_separately",
            }
        )
    connection.execute(
        "UPDATE incidents SET status = 'open' WHERE id = ? AND status = 'closed'", (incident_id,)
    )
    db.record_event(
        connection,
        actor=actor,
        kind="incident.split",
        detail={"sites": moved, "new_incident": new_id, "recheck": recheck},
        incident_id=incident_id,
    )
    connection.commit()
    return {
        "original": incident_id,
        "new_incident": new_id,
        "sites": moved,
        "actions_relinked_not_duplicated": True,
        "complaint_recheck": recheck,
        "note": "拆开后各站点影响时段、等级与证据缺口分别保留；已完成的现场动作不复制成新工作量。",
    }


# ---------------------------------------------------------------------------
# prioritisation and scheduling
# ---------------------------------------------------------------------------


def worklist(
    connection: sqlite3.Connection,
    *,
    month: str,
    as_of: str | datetime,
    window_minutes: float = DEFAULT_LINK_WINDOW_MINUTES,
    loss_threshold: float = DEFAULT_LOSS_THRESHOLD,
) -> list[dict]:
    data, analysis, moment = month_data(connection, month, as_of)
    incidents = list(
        connection.execute(
            "SELECT * FROM incidents WHERE status IN ('open', 'partially_closed') ORDER BY id"
        )
    )
    responsibilities = []
    for incident in incidents:
        for site_row in _incident_sites(connection, incident["id"]):
            if site_row["status"] == "closed":
                continue
            record = next((item for item in analysis["sites"] if item["site"] == site_row["site"]), None)
            priority = site_priority(record) if record else {"score": 0, "reasons": ["站点不在本月分析"]}
            actions = [
                dict(row)
                for row in connection.execute(
                    "SELECT a.*, s.status AS site_status FROM action_sites s JOIN actions a"
                    " ON a.id = s.action_id WHERE s.incident_id = ? AND s.site = ?"
                    " ORDER BY a.planned_start, a.id",
                    (incident["id"], site_row["site"]),
                )
            ]
            completed_work = [a for a in actions if a["site_status"] == "completed"]
            responsibilities.append(
                {
                    "incident": incident["id"],
                    "incident_status": incident["status"],
                    "site": site_row["site"],
                    "site_status": site_row["status"],
                    "discipline": site_row["discipline"],
                    "tier": site_row["tier"],
                    "network": incident["network"],
                    "priority": priority,
                    "contract": record,
                    "actions": actions,
                    "needs_work": not actions or site_row["status"] == "reopened",
                    "requires_recheck": site_row["status"] == "reopened" and bool(completed_work),
                    "next_step": (
                        "重新核对影响与观察证据"
                        if site_row["status"] == "reopened" and completed_work
                        else "安排修复"
                        if not actions
                        else "执行修复"
                        if any(a["site_status"] in ACTION_ACTIVE for a in actions)
                        else "等待满足恢复条件的有效观察"
                    ),
                }
            )
    responsibilities.sort(key=lambda item: (-item["priority"]["score"], item["site"], item["incident"]))
    return responsibilities




def build_schedule(
    connection: sqlite3.Connection,
    *,
    month: str,
    as_of: str | datetime,
    after: str | datetime,
    minutes: float = 30.0,
    incidents: Iterable[str] | None = None,
) -> dict:
    """Greedy priority allocation; shows coverage and explicit risk gaps."""

    moment = parse_ts(as_of, field="as_of") if isinstance(as_of, str) else parse_ts(as_of)
    after_dt = parse_ts(after, field="after") if isinstance(after, str) else parse_ts(after)
    data = source_data(connection)
    items = [item for item in worklist(connection, month=month, as_of=moment) if item["needs_work"]]
    if incidents:
        allowed = set(incidents)
        items = [item for item in items if item["incident"] in allowed]
    proposed = _schedule_all(connection, data, items, after_dt, minutes)

    uncovered = [item for item in proposed if item.get("assignment") is None]
    covered = [item for item in proposed if item.get("assignment")]
    warnings = []
    if uncovered:
        uncovered_tiers = sorted({item["tier"] for item in uncovered if item["tier"] is not None})
        warnings.append(
            "人员覆盖不足：有 %d 项责任无法安排，涉及等级 %s；负责人必须明确承接风险"
            % (len(uncovered), uncovered_tiers)
        )
    if covered and uncovered:
        top_covered = max(item["tier"] or 99 for item in covered)
        dropped_lower = [item for item in uncovered if (item["tier"] or 99) > top_covered]
        if dropped_lower:
            warnings.append(
                "为覆盖高等级站点，低等级站点 %s 可能无法按时处理"
                % sorted({item["site"] for item in dropped_lower})
            )
    return {
        "month": month,
        "as_of": moment.isoformat(),
        "after": after_dt.isoformat(),
        "repair_minutes": minutes,
        "items": proposed,
        "covered": len(covered),
        "uncovered": len(uncovered),
        "requires_risk_ack": bool(uncovered),
        "warnings": warnings,
        "note": "一项确认的修复按其实际占用的人员时段排他；重叠分派会被拒绝。",
    }


@dataclass
class Slot:
    assignee: str
    start: datetime
    end: datetime


def _schedule_all(
    connection: sqlite3.Connection,
    data: SourceData,
    items: list[dict],
    after: datetime,
    minutes: float,
) -> list[dict]:
    occupied: dict[str, list[tuple[datetime, datetime]]] = {}
    proposed = []
    for item in items:
        discipline = item["discipline"]
        slot = _next_slot(connection, data, discipline, after, minutes, occupied)
        if slot is None:
            proposed.append(
                {
                    **item,
                    "assignment": None,
                    "uncovered_reason": "没有具备该技能且可用的完整人员时段",
                }
            )
            continue
        occupied.setdefault(slot.assignee, []).append((slot.start, slot.end))
        proposed.append(
            {
                **item,
                "assignment": {
                    "assignee": slot.assignee,
                    "start": slot.start.isoformat(),
                    "end": slot.end.isoformat(),
                    "minutes": minutes,
                },
            }
        )
    return proposed


def _next_slot(
    connection: sqlite3.Connection,
    data: SourceData,
    discipline: str,
    after: datetime,
    minutes: float,
    occupied: dict[str, list[tuple[datetime, datetime]]],
) -> "Slot | None":
    best: Slot | None = None
    for staff_row in data.staff:
        if discipline not in staff_row.skills:
            continue
        booked = occupied.setdefault(staff_row.staff, _occupied(connection, staff_row.staff))
        candidates = [max(after, staff_row.start)] + [end for _, end in booked]
        for candidate in sorted(candidates):
            end_dt = candidate + timedelta(minutes=minutes)
            if end_dt > staff_row.end:
                continue
            if any(intersect((candidate, end_dt), busy) for busy in booked):
                continue
            if best is None or candidate < best.start or (
                candidate == best.start and staff_row.staff < best.assignee
            ):
                best = Slot(staff_row.staff, candidate, end_dt)
    return best


def apply_schedule(
    connection: sqlite3.Connection,
    *,
    schedule: dict,
    actor: str,
    risk_note: str | None = None,
) -> dict:
    if schedule.get("requires_risk_ack") and not risk_note:
        raise WorkflowError(
            "存在无法覆盖的责任；负责人必须查看影响并明确承接风险（--risk-note）后才能落单"
        )
    created = []
    failures = []
    for item in schedule["items"]:
        assignment = item.get("assignment")
        if not assignment:
            continue
        existing = connection.execute(
            "SELECT 1 FROM action_sites s JOIN actions a ON a.id = s.action_id"
            " WHERE s.incident_id = ? AND s.site = ? AND s.status IN ('planned', 'in_progress', 'completed')",
            (item["incident"], item["site"]),
        ).fetchone()
        if existing:
            continue
        try:
            result = plan_action(
                connection,
                incident_id=item["incident"],
                site=item["site"],
                discipline=item["discipline"],
                assignee=assignment["assignee"],
                start=assignment["start"],
                minutes=assignment["minutes"],
                scope=f"按优先级排期，{item['priority']['reasons'][0]}",
                actor=actor,
            )
        except WorkflowError as exc:
            # One refusal must not strand the rest of the release; the operator
            # sees exactly which responsibility still needs attention.
            failures.append(
                {"incident": item["incident"], "site": item["site"], "reason": str(exc)}
            )
            continue
        created.append(result)
    if schedule.get("requires_risk_ack") and risk_note:
        uncovered = [
            {"incident": item["incident"], "site": item["site"], "reason": item.get("uncovered_reason")}
            for item in schedule["items"]
            if not item.get("assignment")
        ]
        connection.execute(
            "INSERT INTO risk_acks(action_id, incident_id, site, created_at, actor, note, impact)"
            " VALUES (NULL, NULL, NULL, ?, ?, ?, ?)",
            (
                format_ts(now_utc()),
                actor,
                risk_note,
                json.dumps({"uncovered": uncovered, "warnings": schedule["warnings"]}, ensure_ascii=False),
            ),
        )
        db.record_event(
            connection,
            actor=actor,
            kind="schedule.risk_accepted",
            detail={"note": risk_note, "uncovered": uncovered, "warnings": schedule["warnings"]},
        )
    connection.commit()
    return {
        "created": created,
        "failed": failures,
        "uncovered": [
            {"incident": item["incident"], "site": item["site"], "reason": item.get("uncovered_reason")}
            for item in schedule["items"]
            if not item.get("assignment")
        ],
        "risk_note": risk_note,
    }


# ---------------------------------------------------------------------------
# handover and reports
# ---------------------------------------------------------------------------


def add_handover(
    connection: sqlite3.Connection,
    *,
    actor: str,
    note: str,
    incident_id: str | None = None,
    next_step: str | None = None,
    shift: str | None = None,
) -> int:
    if incident_id:
        _get_incident(connection, incident_id)
    cursor = connection.execute(
        "INSERT INTO handovers(incident_id, created_at, actor, note, next_step, shift)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (incident_id, format_ts(now_utc()), actor, note, next_step, shift),
    )
    db.record_event(
        connection,
        actor=actor,
        kind="handover.recorded",
        detail={"note": note, "next_step": next_step, "shift": shift},
        incident_id=incident_id,
    )
    connection.commit()
    return int(cursor.lastrowid)


def list_handovers(connection: sqlite3.Connection, *, incident_id: str | None = None) -> list[dict]:
    if incident_id:
        rows = connection.execute(
            "SELECT * FROM handovers WHERE incident_id = ? ORDER BY created_at, id", (incident_id,)
        )
    else:
        rows = connection.execute("SELECT * FROM handovers ORDER BY created_at, id")
    return [dict(row) for row in rows]


def create_report(
    connection: sqlite3.Connection,
    *,
    month: str,
    as_of: str | datetime,
    actor: str,
    kind: str = "monthly",
    supersedes: str | None = None,
    note: str | None = None,
) -> dict:
    data, analysis, moment = month_data(connection, month, as_of)
    incidents = []
    for row in connection.execute("SELECT * FROM incidents ORDER BY id"):
        sites = [
            dict(site_row)
            for site_row in _incident_sites(connection, row["id"])
        ]
        incidents.append(
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "network": row["network"],
                "sites": [
                    {
                        "site": site["site"],
                        "status": site["status"],
                        "discipline": site["discipline"],
                        "impact_start": site["impact_start"],
                        "impact_end": site["impact_end"],
                        "evidence_gap": site["evidence_gap"],
                    }
                    for site in sites
                ],
            }
        )
    handovers = [
        dict(row)
        for row in connection.execute("SELECT * FROM handovers ORDER BY created_at, id")
    ]
    payload = {
        "analysis": analysis,
        "incidents": incidents,
        "handovers": handovers,
        "known_facts": {
            "segments": sorted(row.id for row in data.segments),
            "complaint_ids": sorted(row.id for row in data.complaints),
            "segments_received": {
                row.id: row.received.isoformat() for row in data.segments
            },
        },
        "generated_from": dict(data.digests),
    }
    version_row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM reports WHERE month = ?", (month,)
    ).fetchone()
    version = int(version_row["version"]) + 1
    report_id = f"RPT-{month}-{version:03d}"
    connection.execute(
        "INSERT INTO reports(id, month, version, kind, as_of, status, created_at, created_by,"
        " supersedes, payload, note) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)",
        (
            report_id,
            month,
            version,
            kind,
            moment.isoformat(),
            format_ts(now_utc()),
            actor,
            supersedes,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            note,
        ),
    )
    db.record_event(
        connection,
        actor=actor,
        kind="report.drafted",
        detail={"month": month, "version": version, "kind": kind, "as_of": moment.isoformat()},
        report_id=report_id,
    )
    connection.commit()
    return get_report(connection, report_id)


def get_report(connection: sqlite3.Connection, report_id: str) -> dict:
    row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        raise WorkflowError(f"月报不存在: {report_id}")
    recipients = [
        dict(recipient)
        for recipient in connection.execute(
            "SELECT * FROM report_recipients WHERE report_id = ? ORDER BY recipient", (report_id,)
        )
    ]
    return {**dict(row), "payload": json.loads(row["payload"]), "recipients": recipients}


def list_reports(connection: sqlite3.Connection, *, month: str | None = None) -> list[dict]:
    if month:
        rows = connection.execute("SELECT * FROM reports WHERE month = ? ORDER BY version", (month,))
    else:
        rows = connection.execute("SELECT * FROM reports ORDER BY month, version")
    result = []
    for row in rows:
        recipients = [
            dict(recipient)
            for recipient in connection.execute(
                "SELECT * FROM report_recipients WHERE report_id = ? ORDER BY recipient", (row["id"],)
            )
        ]
        result.append(
            {
                "id": row["id"],
                "month": row["month"],
                "version": row["version"],
                "kind": row["kind"],
                "as_of": row["as_of"],
                "status": row["status"],
                "created_at": row["created_at"],
                "published_at": row["published_at"],
                "supersedes": row["supersedes"],
                "recipients": recipients,
            }
        )
    return result


def publish_report(
    connection: sqlite3.Connection,
    *,
    report_id: str,
    actor: str,
    recipients: Iterable[str],
    note: str | None = None,
) -> dict:
    row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        raise WorkflowError(f"月报不存在: {report_id}")
    if row["status"] != "draft":
        raise WorkflowError(f"{report_id} 状态为 {row['status']}，只有草稿可以发布")
    recipient_list = sorted({recipient.strip() for recipient in recipients if recipient.strip()})
    if not recipient_list:
        raise WorkflowError("发布至少需要一个收件人")
    at = format_ts(now_utc())
    # Everyone who already holds an older version of this month is told that a
    # newer version exists, so recipients cannot silently compare different
    # conclusions without a stated link.
    older = [
        dict(item)
        for item in connection.execute(
            "SELECT DISTINCT id, version FROM reports WHERE month = ? AND version < ?"
            " AND status = 'published' ORDER BY version",
            (row["month"], row["version"]),
        )
    ]
    previously_reached = sorted(
        {
            recipient["recipient"]
            for recipient in connection.execute(
                "SELECT DISTINCT recipient FROM report_recipients WHERE report_id IN"
                " (SELECT id FROM reports WHERE month = ? AND version < ?)",
                (row["month"], row["version"]),
            )
        }
    )
    connection.execute(
        "UPDATE reports SET status = 'published', published_at = ?, published_by = ? WHERE id = ?",
        (at, actor, report_id),
    )
    for recipient in recipient_list:
        connection.execute(
            "INSERT OR IGNORE INTO report_recipients(report_id, recipient, published_at) VALUES (?, ?, ?)",
            (report_id, recipient, at),
        )
    if note:
        connection.execute(
            "UPDATE reports SET note = COALESCE(note || ' | ', '') || ? WHERE id = ?", (note, report_id)
        )
    db.record_event(
        connection,
        actor=actor,
        kind="report.published",
        detail={
            "recipients": recipient_list,
            "note": note,
            "as_of": row["as_of"],
            "supersedes": [item["id"] for item in older],
            "earlier_recipients_not_in_this_release": sorted(set(previously_reached) - set(recipient_list)),
        },
        report_id=report_id,
    )
    connection.commit()
    return get_report(connection, report_id)


def acknowledge_report(
    connection: sqlite3.Connection, *, report_id: str, recipient: str, actor: str
) -> dict:
    row = connection.execute(
        "SELECT * FROM report_recipients WHERE report_id = ? AND recipient = ?", (report_id, recipient)
    ).fetchone()
    if row is None:
        raise WorkflowError(f"{recipient} 不是 {report_id} 的收件人")
    if row["published_at"] != get_report(connection, report_id)["published_at"]:
        raise WorkflowError("月报已更新版本，请核对最新版本")
    db.record_event(
        connection,
        actor=actor,
        kind="report.acknowledged",
        detail={"recipient": recipient, "report": report_id},
        report_id=report_id,
    )
    connection.commit()
    return {"report": report_id, "recipient": recipient, "acknowledged": True}


def supplement_report(
    connection: sqlite3.Connection,
    *,
    month: str,
    as_of: str | datetime,
    actor: str,
    note: str | None = None,
) -> dict:
    published = connection.execute(
        "SELECT * FROM reports WHERE month = ? AND status = 'published' ORDER BY version DESC LIMIT 1",
        (month,),
    ).fetchone()
    if published is None:
        raise WorkflowError("该月没有已发布月报，不能发布补充")
    fresh = create_report(
        connection,
        month=month,
        as_of=as_of,
        actor=actor,
        kind="supplement",
        supersedes=published["id"],
        note=note,
    )
    diff = diff_reports(
        connection, base_id=published["id"], other_id=fresh["id"]
    )
    payload = fresh["payload"]
    payload["supplement"] = {
        "supersedes": published["id"],
        "base_as_of": published["as_of"],
        "changes": diff,
        "note": "迟到更正以关联补充形式发布；原版本保留当时的数据截止点与争议范围。",
    }
    connection.execute(
        "UPDATE reports SET payload = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), fresh["id"]),
    )
    connection.commit()
    return get_report(connection, fresh["id"])


def _site_snapshot(report: dict) -> dict[str, dict]:
    return {site["site"]: site for site in report["payload"]["analysis"]["sites"]}


def diff_reports(connection: sqlite3.Connection, *, base_id: str, other_id: str) -> dict:
    base = get_report(connection, base_id)
    other = get_report(connection, other_id)
    base_sites = _site_snapshot(base)
    other_sites = _site_snapshot(other)
    site_changes = []
    for site in sorted(set(base_sites) | set(other_sites)):
        before = base_sites.get(site)
        after = other_sites.get(site)
        if before is None or after is None:
            site_changes.append({"site": site, "before": before, "after": after})
            continue
        fields = {}
        for field in ("bad_minutes", "good_minutes", "unknown_minutes", "within_tolerance", "verdict"):
            if before.get(field) != after.get(field):
                fields[field] = {"before": before.get(field), "after": after.get(field)}
        if fields:
            site_changes.append({"site": site, "changes": fields})
    base_late = set(base["payload"]["analysis"].get("late_facts", {}).get("segments", []))
    other_late = set(other["payload"]["analysis"].get("late_facts", {}).get("segments", []))
    base_corrections = {
        (item["kind"], item["id"])
        for item in base["payload"]["analysis"].get("late_corrections", [])
    }
    other_corrections = {
        (item["kind"], item["id"])
        for item in other["payload"]["analysis"].get("late_corrections", [])
    }
    base_known = set(base["payload"].get("known_facts", {}).get("segments", []))
    other_known = set(other["payload"].get("known_facts", {}).get("segments", []))
    base_known_complaints = set(base["payload"].get("known_facts", {}).get("complaint_ids", []))
    other_known_complaints = set(other["payload"].get("known_facts", {}).get("complaint_ids", []))
    base_disputes = {(item["site"], item["session"], item["start"]) for item in base["payload"]["analysis"].get("disputes", [])}
    other_disputes = {(item["site"], item["session"], item["start"]) for item in other["payload"]["analysis"].get("disputes", [])}
    return {
        "base": base_id,
        "other": other_id,
        "base_as_of": base["as_of"],
        "other_as_of": other["as_of"],
        "sites": site_changes,
        "new_segments_after_base_as_of": sorted(other_known - base_known),
        "new_complaints_after_base_as_of": sorted(other_known_complaints - base_known_complaints),
        "new_late_segments": sorted(other_late - base_late),
        "new_corrections": sorted(list(item) for item in other_corrections - base_corrections),
        "new_disputes": [list(item) for item in sorted(other_disputes - base_disputes)],
        "overall": {
            "before": base["payload"]["analysis"]["overall"]["conclusion"],
            "after": other["payload"]["analysis"]["overall"]["conclusion"],
        },
    }


def report_progress(connection: sqlite3.Connection, *, month: str | None = None) -> list[dict]:
    reports = list_reports(connection, month=month)
    progress = []
    for report in reports:
        recipients = report["recipients"]
        progress.append(
            {
                "id": report["id"],
                "month": report["month"],
                "version": report["version"],
                "kind": report["kind"],
                "status": report["status"],
                "as_of": report["as_of"],
                "supersedes": report["supersedes"],
                "published_at": report["published_at"],
                "recipients": [recipient["recipient"] for recipient in recipients],
                "acknowledged": _acknowledged(connection, report["id"]),
                "pending": [
                    recipient["recipient"]
                    for recipient in recipients
                    if recipient["recipient"] not in set(_acknowledged(connection, report["id"]))
                ],
                "superseded_by": [
                    item["id"]
                    for item in connection.execute(
                        "SELECT id FROM reports WHERE month = ? AND version > ?"
                        " AND status = 'published' ORDER BY version",
                        (report["month"], report["version"]),
                    )
                ],
            }
        )
    return progress


def _acknowledged(connection: sqlite3.Connection, report_id: str) -> list[str]:
    return sorted(
        {
            json.loads(row["detail"]).get("recipient")
            for row in connection.execute(
                "SELECT detail FROM events WHERE report_id = ? AND kind = 'report.acknowledged'",
                (report_id,),
            )
            if json.loads(row["detail"]).get("recipient")
        }
    )


def dashboard(
    connection: sqlite3.Connection,
    *,
    month: str,
    as_of: str | datetime,
) -> dict:
    data, analysis, moment = month_data(connection, month, as_of)
    if not data.sites:
        raise WorkflowError("工作目录没有站点资料，请先 init 或 import")
    incidents = list_incidents(connection)
    complaints = complaint_list(connection, as_of=moment)
    work = worklist(connection, month=month, as_of=moment)
    reports = report_progress(connection, month=month)
    return {
        "month": month,
        "as_of": moment.isoformat(),
        "contract": analysis["overall"],
        "sites": analysis["sites"],
        "incidents": incidents,
        "complaints": complaints,
        "worklist": work,
        "reports": reports,
        "handovers": list_handovers(connection),
        "data_findings": analysis["findings"],
        "late_facts": analysis["late_facts"],
    }
