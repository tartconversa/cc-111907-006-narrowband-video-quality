"""Business acceptance tests for the service-experience operations app.

Each test names the operational rule it protects.  Fixtures are built in
temporary directories from synthetic JSON so the read-only ``inputs/`` folder is
never modified.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from svcops import service
from svcops.analysis import (
    BAD,
    DISPUTED,
    GOOD,
    UNKNOWN,
    analyse_site,
    build_month_analysis,
    build_session_timelines,
    complaint_evidence,
    restore_evidence,
)
from svcops.sources import build_sources, load_sources
from svcops.timeutil import parse_ts

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
AS_OF = parse_ts("2026-12-05T00:00:00Z")


def iso(minutes: int, day: str = "2026-11-10T08:00:00Z") -> str:
    return (parse_ts(day) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def make_sources(
    *,
    sites=None,
    contracts=None,
    segments=None,
    probes=None,
    complaints=None,
    windows=None,
    staff=None,
) -> dict:
    return {
        "sites": sites
        if sites is not None
        else [{"site": "S1", "network": "N1", "contract": "K1", "region": "west"}],
        "contracts": contracts
        if contracts is not None
        else [
            {
                "contract": "K1",
                "min_quality": 90,
                "monthly_bad_minutes": 20,
                "restore_minutes": 30,
                "tier": 1,
            }
        ],
        "segments": segments or [],
        "probes": probes or [],
        "complaints": complaints or [],
        "windows": windows or [],
        "staff": staff or [],
    }


def segment(
    sid,
    start,
    end,
    quality,
    *,
    session=None,
    site="S1",
    network="N1",
    revision=1,
    received="2026-11-30T00:00:00Z",
):
    return {
        "id": sid,
        "revision": revision,
        "session": session or f"P-{sid}",
        "site": site,
        "network": network,
        "start": start,
        "end": end,
        "quality": quality,
        "received": received,
    }


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="svcops-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_inputs(self, payload: dict, name: str = "inputs") -> Path:
        root = self.tmp / name
        root.mkdir(parents=True, exist_ok=True)
        for kind, rows in payload.items():
            (root / f"{kind}.json").write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8"
            )
        return root

    def open_ws(self, payload: dict, name: str = "ws") -> "service.db.sqlite3.Connection":
        inputs = self.write_inputs(payload, name=f"{name}-inputs")
        workspace = self.tmp / name
        service.init_workspace(workspace, inputs, actor="test")
        connection = service.open_workspace(workspace)
        self.addCleanup(connection.close)
        return connection


# ---------------------------------------------------------------------------
# playback coverage and contract accounting
# ---------------------------------------------------------------------------


class CoverageTests(WorkspaceCase):
    def test_overlapping_segments_of_one_session_are_counted_once(self):
        payload = make_sources(
            segments=[
                segment("A", iso(0), iso(15), 95, session="P1"),
                segment("B", iso(5), iso(20), 95, session="P1"),
            ]
        )
        data = build_sources(payload)
        timeline = build_session_timelines(data, "S1", AS_OF)[0]
        self.assertEqual(round(timeline.totals[GOOD], 3), 20.0)
        self.assertEqual(len(timeline.pieces), 3)
        self.assertEqual(timeline.pieces[1].segments, ("A", "B"))

    def test_newer_revision_replaces_the_same_source_identity(self):
        payload = make_sources(
            segments=[
                segment("A", iso(0), iso(15), 50, revision=1),
                segment("A", iso(0), iso(15), 96, revision=2),
            ]
        )
        data = build_sources(payload)
        analysis = analyse_site(data, "S1", "2026-11", AS_OF)
        self.assertEqual(analysis.totals[BAD], 0.0)
        self.assertEqual(analysis.totals[GOOD], 15.0)

    def test_newer_revision_does_not_necessarily_extend_coverage(self):
        payload = make_sources(
            segments=[
                segment("A", iso(0), iso(15), 50, revision=1),
                segment("A", iso(5), iso(20), 96, revision=2),
            ]
        )
        data = build_sources(payload)
        timeline = build_session_timelines(data, "S1", AS_OF)[0]
        self.assertEqual(round(timeline.totals[GOOD], 3), 15.0)
        self.assertEqual(timeline.span_start, parse_ts(iso(5)))

    def test_same_revision_with_conflicting_quality_is_a_dispute_not_a_best_value(self):
        payload = make_sources(
            segments=[
                segment("A", iso(0), iso(15), 95, revision=2),
                segment("A", iso(0), iso(15), 40, revision=2),
            ]
        )
        data = build_sources(payload)
        analysis = analyse_site(data, "S1", "2026-11", AS_OF)
        self.assertEqual(analysis.totals[DISPUTED], 15.0)
        self.assertEqual(analysis.totals[GOOD], 0.0)
        self.assertEqual(analysis.totals[BAD], 0.0)
        codes = {finding.code for finding in data.findings}
        self.assertIn("source-version-conflict", codes)

    def test_unknown_quality_is_neither_good_nor_bad_and_is_disclosed(self):
        payload = make_sources(
            segments=[
                segment("A", iso(0), iso(15), None),
                segment("B", iso(15), iso(30), 95),
            ]
        )
        data = build_sources(payload)
        record = analyse_site(data, "S1", "2026-11", AS_OF).as_dict()
        self.assertEqual(record["unknown_minutes"], 15.0)
        self.assertEqual(record["good_minutes"], 15.0)
        self.assertEqual(record["bad_minutes"], 0.0)
        self.assertIn("未核实覆盖", record["verdict"])

    def test_a_sampling_gap_is_reported_and_not_treated_as_good(self):
        payload = make_sources(
            segments=[
                segment("A", iso(0), iso(15), 95, session="P1"),
                segment("B", iso(30), iso(45), 95, session="P1"),
            ]
        )
        data = build_sources(payload)
        analysis = analyse_site(data, "S1", "2026-11", AS_OF)
        record = analysis.as_dict()
        self.assertEqual(record["good_minutes"], 30.0)
        self.assertEqual(record["session_gap_minutes"], 15.0)
        evidence = build_month_analysis(data, "2026-11", AS_OF)["sites"][0]["evidence"]
        self.assertEqual(evidence["sampling_gap_minutes"], 15.0)

    def test_no_observed_playback_is_never_declared_healthy(self):
        payload = make_sources(segments=[])
        record = analyse_site(build_sources(payload), "S1", "2026-11", AS_OF).as_dict()
        self.assertEqual(record["observed_minutes"], 0.0)
        self.assertIn("不能视为良好", record["verdict"])

    def test_cross_site_session_keeps_per_site_timelines(self):
        payload = make_sources(
            sites=[
                {"site": "S1", "network": "N1", "contract": "K1", "region": "west"},
                {"site": "S2", "network": "N1", "contract": "K1", "region": "west"},
            ],
            segments=[
                segment("A", iso(0), iso(15), 95, session="P9", site="S1"),
                segment("B", iso(15), iso(30), 95, session="P9", site="S2"),
            ],
        )
        data = build_sources(payload)
        s1 = build_session_timelines(data, "S1", AS_OF)[0]
        s2 = build_session_timelines(data, "S2", AS_OF)[0]
        self.assertEqual(s1.totals[GOOD], 15.0)
        self.assertEqual(s2.totals[GOOD], 15.0)
        self.assertEqual(s1.other_sites, ["S2"])
        report = build_month_analysis(data, "2026-11", AS_OF)
        handoff = report["handoffs"][0]
        self.assertTrue(handoff["migration"])
        self.assertEqual(handoff["sites"], ["S1", "S2"])

    def test_link_switch_inside_one_site_is_flagged(self):
        payload = make_sources(
            segments=[
                segment("A", iso(0), iso(15), 95, session="P1", network="N1"),
                segment("B", iso(15), iso(30), 95, session="P1", network="N9"),
            ]
        )
        data = build_sources(payload)
        timeline = build_session_timelines(data, "S1", AS_OF)[0]
        self.assertTrue(timeline.as_dict()["link_switch"])
        self.assertEqual(timeline.totals[GOOD], 30.0)


class ContractTests(WorkspaceCase):
    def _sites(self):
        return [
            {"site": "S1", "network": "N1", "contract": "K1", "region": "west"},
            {"site": "S2", "network": "N1", "contract": "K2", "region": "west"},
        ]

    def _contracts(self):
        return [
            {"contract": "K1", "min_quality": 90, "monthly_bad_minutes": 20, "restore_minutes": 30, "tier": 1},
            {"contract": "K2", "min_quality": 80, "monthly_bad_minutes": 60, "restore_minutes": 20, "tier": 2},
        ]

    def test_maintenance_removes_only_its_own_minutes_and_restores_nothing(self):
        payload = make_sources(
            sites=self._sites(),
            contracts=self._contracts(),
            segments=[
                segment("A", iso(0), iso(15), 50, session="P1", site="S1"),
                segment("B", iso(15), iso(30), 50, session="P2", site="S1"),
                segment("C", iso(30), iso(45), 50, session="P3", site="S1"),
            ],
            windows=[
                {"id": "W1", "site": "S1", "type": "maintenance", "start": iso(10), "end": iso(35)}
            ],
        )
        data = build_sources(payload)
        analysis = analyse_site(data, "S1", "2026-11", AS_OF)
        # 45 raw bad minutes; maintenance removes 5 + 15 + 5 = 25 observed minutes
        self.assertEqual(round(analysis.totals[BAD], 3), 20.0)
        self.assertEqual(round(analysis.maintenance_excluded, 3), 25.0)
        # exactly 20 of a 20 minute tolerance is still within tolerance
        self.assertTrue(analysis.within_tolerance)

    def test_maintenance_exclusion_counts_parallel_sessions_once(self):
        # Two parallel sessions play the same wall-clock minutes, one of which is
        # inside maintenance.  The excluded figure must not double count.
        payload = make_sources(
            segments=[
                segment("A", iso(0), iso(20), 95, session="P1"),
                segment("B", iso(0), iso(20), 95, session="P2"),
            ],
            windows=[
                {"id": "M", "site": "S1", "type": "maintenance", "start": iso(5), "end": iso(15)}
            ],
        )
        analysis = analyse_site(build_sources(payload), "S1", "2026-11", AS_OF)
        self.assertEqual(analysis.maintenance_excluded, 10.0)
        self.assertEqual(analysis.totals[GOOD], 20.0)

    def test_aggregate_within_tolerance_is_flagged_when_one_site_breaches(self):
        payload = make_sources(
            sites=self._sites(),
            contracts=self._contracts(),
            segments=[
                # Combined 30 bad minutes is under the combined 80 tolerance, but
                # S1 alone is over its own 20 minute limit.
                segment("A", iso(0), iso(30), 50, site="S1", session="P1"),
            ],
        )
        report = build_month_analysis(build_sources(payload), "2026-11", AS_OF)
        overall = report["overall"]
        self.assertTrue(overall["aggregate_within_tolerance"])
        self.assertEqual(overall["sites_in_breach"], ["S1"])
        self.assertTrue(overall["aggregate_masks_site_breach"])
        self.assertEqual(overall["conclusion"], "不达标：存在站点超出自身合同容忍")

    def test_aggregate_pass_cannot_mask_a_site_over_its_own_limit(self):
        payload = make_sources(
            sites=self._sites(),
            contracts=self._contracts(),
            segments=[
                # S1: 90 bad minutes, limit 20 -> breach
                segment("A", iso(0), iso(45), 50, site="S1", session="P1"),
                segment("B", iso(0), iso(45), 50, site="S1", session="P2"),
                # S2: 0 bad minutes
                segment("C", iso(0), iso(45), 95, site="S2"),
            ],
        )
        data = build_sources(payload)
        report = build_month_analysis(data, "2026-11", AS_OF)
        s1 = next(site for site in report["sites"] if site["site"] == "S1")
        s2 = next(site for site in report["sites"] if site["site"] == "S2")
        self.assertFalse(s1["within_tolerance"])
        self.assertTrue(s2["within_tolerance"])
        self.assertEqual(report["overall"]["sites_in_breach"], ["S1"])
        self.assertEqual(report["overall"]["tier1_sites_in_breach"], ["S1"])
        # Same-contract rollup never pools the minutes.
        k1 = next(item for item in report["contracts"] if item["contract"] == "K1")
        self.assertEqual(k1["sites"][0]["bad_minutes"], 90.0)

    def test_restore_evidence_requires_new_valid_good_playback(self):
        payload = make_sources(
            segments=[
                # pre-existing good playback before the repair is not new evidence
                segment("A", iso(0), iso(60), 95, session="OLD", received=iso(70)),
                # repair at 08:60; new session after the repair
                segment("B", iso(70), iso(100), 95, session="NEW", received=iso(110)),
            ]
        )
        data = build_sources(payload)
        evidence = restore_evidence(data, "S1", parse_ts(iso(60)), AS_OF)
        self.assertEqual(round(evidence.new_good_minutes, 3), 30.0)
        self.assertTrue(evidence.sufficient)

    def test_restore_evidence_ignores_unknown_and_bad_playback(self):
        payload = make_sources(
            segments=[
                segment("A", iso(70), iso(100), None, session="NEW", received=iso(110)),
                segment("B", iso(100), iso(130), 50, session="NEW2", received=iso(140)),
            ]
        )
        data = build_sources(payload)
        evidence = restore_evidence(data, "S1", parse_ts(iso(60)), AS_OF)
        self.assertEqual(evidence.new_good_minutes, 0.0)
        self.assertEqual(evidence.unknown_minutes, 30.0)
        self.assertEqual(evidence.bad_minutes, 30.0)
        self.assertFalse(evidence.sufficient)

    def test_late_correction_does_not_change_an_earlier_as_of(self):
        payload = make_sources(
            segments=[
                segment("A", iso(0), iso(15), 95, received="2026-11-30T00:00:00Z"),
            ]
        )
        data = build_sources(payload)
        before = analyse_site(data, "S1", "2026-11", parse_ts("2026-11-29T00:00:00Z"))
        after = analyse_site(data, "S1", "2026-11", AS_OF)
        self.assertEqual(before.totals[GOOD], 0.0)
        self.assertEqual(after.totals[GOOD], 15.0)


# ---------------------------------------------------------------------------
# complaint linkage
# ---------------------------------------------------------------------------


class ComplaintTests(WorkspaceCase):
    def _payload(self, *, with_bad=True):
        return make_sources(
            sites=[
                {"site": "S1", "network": "N1", "contract": "K1", "region": "west"},
                {"site": "S2", "network": "N1", "contract": "K1", "region": "west"},
            ],
            probes=[
                {"id": "P1", "network": "N1", "start": iso(0), "end": iso(60), "loss_pct": 6.0}
            ],
            segments=[
                segment(
                    "A",
                    iso(5),
                    iso(20),
                    (50 if with_bad else 95),
                    session="P1",
                    site="S1",
                    received="2026-11-16T10:00:00Z",
                )
            ]
            if with_bad
            else [
                segment(
                    "A", iso(5), iso(20), 95, session="P1", site="S1", received="2026-11-16T10:00:00Z"
                )
            ],
            complaints=[
                {
                    "id": "C1",
                    "site": "S1",
                    "occurred": iso(10),
                    "received": "2026-11-16T12:00:00Z",
                    "symptom": "freeze",
                },
                {
                    "id": "C2",
                    "site": "S2",
                    "occurred": iso(12),
                    "received": "2026-11-16T12:30:00Z",
                    "symptom": "freeze",
                },
                {
                    "id": "C3",
                    "site": "S2",
                    "occurred": iso(50, "2026-11-20T08:00:00Z"),
                    "received": "2026-11-21T09:00:00Z",
                    "symptom": "audio",
                },
            ],
        )

    def test_complaint_is_linked_only_when_probe_covers_the_instant(self):
        data = build_sources(self._payload())
        c1 = next(row for row in data.complaints if row.id == "C1")
        c3 = next(row for row in data.complaints if row.id == "C3")
        self.assertTrue(complaint_evidence(data, c1, AS_OF)["sufficient"])
        evidence = complaint_evidence(data, c3, AS_OF)
        self.assertFalse(evidence["sufficient"])
        self.assertEqual(evidence["basis"], [])

    def test_unlinked_complaint_can_be_investigated_alone(self):
        connection = self.open_ws(self._payload())
        incident = service.open_incident_from_complaint(
            connection, complaint_id="C3", actor="duty", as_of=AS_OF
        )
        detail = service.incident_detail(connection, incident, month="2026-11", as_of=AS_OF)
        self.assertEqual(detail["origin_type"], "complaint")
        self.assertEqual([site["site"] for site in detail["sites"]], ["S2"])

    def test_linking_insufficient_complaint_to_shared_incident_is_refused(self):
        connection = self.open_ws(self._payload())
        incident = service.open_incident(
            connection, title="shared", sites=["S1", "S2"], actor="duty", network="N1"
        )
        with self.assertRaises(service.WorkflowError):
            service.link_complaint(
                connection, complaint_id="C3", incident_id=incident, actor="duty", as_of=AS_OF
            )
        rows = service.complaint_list(connection, as_of=AS_OF)
        c3 = next(row for row in rows if row["id"] == "C3")
        self.assertEqual(c3["links"], [])

    def test_ticket_closure_is_not_service_restoration(self):
        connection = self.open_ws(self._payload(with_bad=True))
        incident = service.open_incident_from_shared(
            connection, network="N1", actor="duty", as_of=AS_OF, complaints=["C1"]
        )
        result = service.close_complaint(
            connection, complaint_id="C1", actor="agent", note="回访已答复"
        )
        self.assertFalse(result["service_restoration_claimed"])
        detail = service.incident_detail(connection, incident, month="2026-11", as_of=AS_OF)
        # Ticket closed, but the site's repair responsibility is still open.
        self.assertEqual(detail["sites"][0]["status"], "open")
        records = service.complaint_list(connection, as_of=AS_OF)
        self.assertEqual(next(r for r in records if r["id"] == "C1")["status"], "closed_ticket")


# ---------------------------------------------------------------------------
# work orders, closure and splits
# ---------------------------------------------------------------------------


class WorkOrderTests(WorkspaceCase):
    def _payload(self, *, good_after=True):
        segments = [
            segment("A", iso(0), iso(30), 50, session="P1", received="2026-11-16T10:00:00Z"),
        ]
        if good_after:
            segments.append(
                segment(
                    "B",
                    "2026-11-17T08:10:00Z",
                    "2026-11-17T08:50:00Z",
                    95,
                    session="P2",
                    received="2026-11-17T09:00:00Z",
                )
            )
        return make_sources(
            sites=[
                {"site": "S1", "network": "N1", "contract": "K1", "region": "west"},
                {"site": "S2", "network": "N1", "contract": "K1", "region": "west"},
            ],
            segments=segments,
            windows=[],
            staff=[
                {
                    "staff": "A",
                    "skills": ["network"],
                    "start": "2026-11-17T08:00:00Z",
                    "end": "2026-11-17T12:00:00Z",
                },
                {
                    "staff": "B",
                    "skills": ["player", "site"],
                    "start": "2026-11-17T08:00:00Z",
                    "end": "2026-11-17T12:00:00Z",
                },
            ],
        )

    def _incident(self, connection):
        return service.open_incident(
            connection,
            title="shared",
            sites=["S1", "S2"],
            actor="duty",
            network="N1",
            complaints=[],
        )

    def test_shared_work_order_completes_sites_independently(self):
        connection = self.open_ws(self._payload())
        incident = self._incident(connection)
        plan = service.plan_action(
            connection,
            incident_id=incident,
            sites=["S1", "S2"],
            discipline="network",
            assignee="A",
            start="2026-11-17T08:00:00Z",
            minutes=30,
            actor="lead",
        )
        service.start_action(connection, action_id=plan["action"], actor="A")
        done = service.complete_action(
            connection,
            action_id=plan["action"],
            site="S1",
            actor="A",
            cause="port",
            repair_scope="replace port",
            completed_at="2026-11-17T08:10:00Z",
        )
        self.assertEqual(done["action_status"], "partially_completed")
        detail = service.incident_detail(connection, incident, month="2026-11", as_of=AS_OF)
        statuses = {site["site"]: site["status"] for site in detail["sites"]}
        self.assertEqual(statuses["S1"], "awaiting_observation")
        self.assertEqual(statuses["S2"], "open")

    def test_closure_requires_sufficient_post_repair_observation(self):
        connection = self.open_ws(self._payload(good_after=False))
        incident = service.open_incident(
            connection, title="one", sites=["S1"], actor="duty", discipline="network"
        )
        plan = service.plan_action(
            connection,
            incident_id=incident,
            site="S1",
            discipline="network",
            assignee="A",
            start="2026-11-17T08:00:00Z",
            actor="lead",
        )
        service.start_action(connection, action_id=plan["action"], actor="A")
        service.complete_action(
            connection,
            action_id=plan["action"],
            site="S1",
            actor="A",
            cause="c",
            repair_scope="s",
            completed_at="2026-11-17T08:05:00Z",
        )
        with self.assertRaises(service.WorkflowError) as raised:
            service.close_incident_site(
                connection,
                incident_id=incident,
                site="S1",
                since="2026-11-17T08:05:00Z",
                as_of=AS_OF,
                actor="duty",
            )
        self.assertIn("观察不足", str(raised.exception))

    def test_closure_succeeds_with_real_new_good_playback(self):
        connection = self.open_ws(self._payload(good_after=True))
        incident = service.open_incident(
            connection, title="one", sites=["S1"], actor="duty", discipline="network"
        )
        plan = service.plan_action(
            connection,
            incident_id=incident,
            site="S1",
            discipline="network",
            assignee="A",
            start="2026-11-17T08:00:00Z",
            actor="lead",
        )
        service.start_action(connection, action_id=plan["action"], actor="A")
        service.complete_action(
            connection,
            action_id=plan["action"],
            site="S1",
            actor="A",
            cause="c",
            repair_scope="s",
            completed_at="2026-11-17T08:05:00Z",
        )
        result = service.close_incident_site(
            connection,
            incident_id=incident,
            site="S1",
            since="2026-11-17T08:05:00Z",
            as_of=AS_OF,
            actor="duty",
        )
        self.assertEqual(result["status"], "closed")
        self.assertGreaterEqual(result["report"]["new_good_minutes"], 30.0)

    def test_observation_with_only_maintenance_playback_is_waiting(self):
        payload = self._payload(good_after=True)
        payload["windows"] = [
            {
                "id": "M1",
                "site": "S1",
                "type": "maintenance",
                "start": "2026-11-17T08:00:00Z",
                "end": "2026-11-17T09:00:00Z",
            }
        ]
        connection = self.open_ws(payload)
        incident = service.open_incident(
            connection, title="one", sites=["S1"], actor="duty", discipline="network"
        )
        plan = service.plan_action(
            connection,
            incident_id=incident,
            site="S1",
            discipline="network",
            assignee="A",
            start="2026-11-17T08:00:00Z",
            actor="lead",
        )
        service.start_action(connection, action_id=plan["action"], actor="A")
        service.complete_action(
            connection,
            action_id=plan["action"],
            site="S1",
            actor="A",
            cause="c",
            repair_scope="s",
            completed_at="2026-11-17T08:05:00Z",
        )
        check = service.observation_check(
            connection,
            incident_id=incident,
            site="S1",
            since="2026-11-17T08:05:00Z",
            as_of=AS_OF,
            actor="duty",
        )
        self.assertFalse(check["sufficient"])
        self.assertGreater(check["maintenance_excluded_minutes"], 0)

    def test_closure_rejects_observation_that_starts_before_the_repair(self):
        connection = self.open_ws(self._payload(good_after=True))
        incident = service.open_incident(
            connection, title="one", sites=["S1"], actor="duty", discipline="network"
        )
        plan = service.plan_action(
            connection,
            incident_id=incident,
            site="S1",
            discipline="network",
            assignee="A",
            start="2026-11-17T08:00:00Z",
            actor="lead",
        )
        service.start_action(connection, action_id=plan["action"], actor="A")
        service.complete_action(
            connection,
            action_id=plan["action"],
            site="S1",
            actor="A",
            cause="c",
            repair_scope="s",
            completed_at="2026-11-17T08:05:00Z",
        )
        with self.assertRaises(service.WorkflowError) as raised:
            service.close_incident_site(
                connection,
                incident_id=incident,
                site="S1",
                since="2026-11-16T00:00:00Z",  # pre-repair evidence
                as_of=AS_OF,
                actor="duty",
            )
        self.assertIn("观察起点早于最近一次修复", str(raised.exception))

    def test_scheduled_repairs_never_share_a_person_or_slot(self):
        payload = self._payload()
        payload["sites"] = [
            {"site": "S1", "network": "N1", "contract": "K1", "region": "west"},
            {"site": "S2", "network": "N1", "contract": "K1", "region": "west"},
            {"site": "S3", "network": "N1", "contract": "K1", "region": "west"},
        ]
        connection = self.open_ws(payload)
        incident = service.open_incident(
            connection, title="shared", sites=["S1", "S2", "S3"], actor="duty", network="N1"
        )
        schedule = service.build_schedule(
            connection,
            month="2026-11",
            as_of=AS_OF,
            after="2026-11-17T08:00:00Z",
            minutes=60,
            incidents=[incident],
        )
        slots = [
            (item["assignment"]["assignee"], item["assignment"]["start"], item["assignment"]["end"])
            for item in schedule["items"]
        ]
        self.assertEqual(len(slots), 3)
        # one person, 4-hour shift, 60-minute repairs: three fit without overlap
        self.assertEqual({slot[0] for slot in slots}, {"A"})
        starts = sorted(parse_ts(slot[1]) for slot in slots)
        self.assertEqual(starts[1] - starts[0], timedelta(minutes=60))
        self.assertEqual(starts[2] - starts[1], timedelta(minutes=60))

    def test_staffing_never_double_books_a_person(self):
        connection = self.open_ws(self._payload())
        incident = self.open_ws(self._payload(), name="ws2")
        incident_id = self._incident(connection)
        service.plan_action(
            connection,
            incident_id=incident_id,
            site="S1",
            discipline="network",
            assignee="A",
            start="2026-11-17T08:00:00Z",
            minutes=30,
            actor="lead",
        )
        with self.assertRaises(service.WorkflowError):
            service.plan_action(
                connection,
                incident_id=incident_id,
                site="S2",
                discipline="network",
                assignee="A",
                start="2026-11-17T08:15:00Z",
                minutes=30,
                actor="lead",
            )

    def test_schedule_reports_uncovered_work_and_requires_risk_acknowledgement(self):
        payload = self._payload()
        payload["staff"] = [
            {
                "staff": "A",
                "skills": ["network"],
                "start": "2026-11-17T08:00:00Z",
                "end": "2026-11-17T08:30:00Z",
            }
        ]
        connection = self.open_ws(payload)
        incident = self._incident(connection)
        schedule = service.build_schedule(
            connection,
            month="2026-11",
            as_of=AS_OF,
            after="2026-11-17T08:00:00Z",
            minutes=30,
            incidents=[incident],
        )
        self.assertTrue(schedule["requires_risk_ack"])
        self.assertEqual(schedule["uncovered"], 1)
        with self.assertRaises(service.WorkflowError):
            service.apply_schedule(connection, schedule=schedule, actor="lead")
        applied = service.apply_schedule(
            connection, schedule=schedule, actor="lead", risk_note="接受 S2 延后到下一班"
        )
        self.assertEqual(len(applied["created"]), 1)
        self.assertEqual(len(applied["uncovered"]), 1)

    def test_late_correction_preserves_an_executing_repair(self):
        connection = self.open_ws(self._payload(good_after=False))
        incident = service.open_incident(
            connection, title="one", sites=["S1"], actor="duty", discipline="network"
        )
        plan = service.plan_action(
            connection,
            incident_id=incident,
            site="S1",
            discipline="network",
            assignee="A",
            start="2026-11-17T08:00:00Z",
            actor="lead",
        )
        service.start_action(connection, action_id=plan["action"], actor="A")
        # a late correction rewrites the quality of the original evidence
        # A correction that arrives after the monthly snapshot is late relative
        # to that snapshot, but an executing repair is not rolled back.
        correction = self.write_inputs(
            {
                "segments": [
                    dict(
                        segment(
                            "A",
                            iso(0),
                            iso(30),
                            95,
                            session="P1",
                            revision=2,
                            received="2026-12-01T00:00:00Z",
                        )
                    )
                ]
            },
            name="correction",
        )
        service.import_inputs(connection, correction, actor="duty")
        actions = connection.execute("SELECT status FROM actions").fetchall()
        self.assertEqual([row["status"] for row in actions], ["in_progress"])
        analysis = build_month_analysis(
            service.source_data(connection), "2026-11", parse_ts("2026-11-30T00:00:00Z")
        )
        self.assertIn(
            "A", analysis["late_facts"]["segments"], "corrected identity should be visible as late fact"
        )
        self.assertGreaterEqual(
            connection.execute("SELECT COUNT(*) AS count FROM actions").fetchone()["count"], 1
        )


class SplitTests(WorkspaceCase):
    def _setup(self):
        payload = make_sources(
            sites=[
                {"site": "S1", "network": "N1", "contract": "K1", "region": "west"},
                {"site": "S2", "network": "N1", "contract": "K1", "region": "west"},
            ],
            probes=[
                {"id": "P1", "network": "N1", "start": iso(0), "end": iso(60), "loss_pct": 6.0}
            ],
            segments=[
                segment("A", iso(5), iso(20), 50, session="P1", site="S1", received="2026-11-16T10:00:00Z"),
                segment("B", iso(5), iso(20), 95, session="P2", site="S2", received="2026-11-16T10:00:00Z"),
            ],
            complaints=[
                {"id": "C1", "site": "S1", "occurred": iso(10), "received": "2026-11-16T11:00:00Z", "symptom": "freeze"},
                {"id": "C2", "site": "S2", "occurred": iso(10), "received": "2026-11-16T11:00:00Z", "symptom": "freeze"},
            ],
            staff=[
                {"staff": "A", "skills": ["network"], "start": iso(0), "end": iso(240)}
            ],
        )
        connection = self.open_ws(payload)
        incident = service.open_incident_from_shared(
            connection, network="N1", actor="duty", as_of=AS_OF, complaints=["C1", "C2"]
        )
        plan = service.plan_action(
            connection,
            incident_id=incident,
            sites=["S1", "S2"],
            discipline="network",
            assignee="A",
            start=iso(60),
            minutes=30,
            actor="lead",
        )
        service.start_action(connection, action_id=plan["action"], actor="A")
        service.complete_action(
            connection,
            action_id=plan["action"],
            site="S1",
            actor="A",
            cause="port",
            repair_scope="replace",
            actual_minutes=20,
            completed_at=iso(85),
        )
        return connection, incident, plan

    def test_split_repoints_work_without_duplicating_completed_field_work(self):
        connection, incident, plan = self._setup()
        before = connection.execute(
            "SELECT COUNT(*) AS count FROM actions"
        ).fetchone()["count"]
        result = service.split_incident(
            connection,
            incident_id=incident,
            sites=["S1"],
            actor="duty",
            as_of=AS_OF,
        )
        self.assertEqual(result["new_incident"], "INC-0002")
        after = connection.execute("SELECT COUNT(*) AS count FROM actions").fetchone()["count"]
        self.assertEqual(before, after, "splitting must not create new workload")
        moved = connection.execute(
            "SELECT * FROM action_sites WHERE incident_id = ? AND site = 'S1'",
            (result["new_incident"],),
        ).fetchone()
        self.assertEqual(moved["action_id"], plan["action"])
        self.assertEqual(moved["status"], "completed")
        self.assertEqual(moved["actual_minutes"], 20)
        # the site is re-opened for recheck even though the field action stays done
        new_site = connection.execute(
            "SELECT status FROM incident_sites WHERE incident_id = ? AND site = 'S1'",
            (result["new_incident"],),
        ).fetchone()
        self.assertEqual(new_site["status"], "reopened")

    def test_split_keeps_site_specific_impact_and_evidence_gaps(self):
        connection, incident, _ = self._setup()
        result = service.split_incident(
            connection,
            incident_id=incident,
            sites=["S2"],
            actor="duty",
            as_of=AS_OF,
        )
        detail = service.incident_detail(
            connection, result["new_incident"], month="2026-11", as_of=AS_OF
        )
        self.assertEqual([site["site"] for site in detail["sites"]], ["S2"])
        # S2 had good playback; its own contract figure and evidence are kept,
        # not inherited from S1's bad session.
        contract = detail["sites"][0]["contract"]
        self.assertEqual(contract["site"], "S2")
        self.assertEqual(contract["good_minutes"], 15.0)
        self.assertEqual(contract["bad_minutes"], 0.0)

    def test_split_rechecks_complaints_and_reopens_affected_responsibility(self):
        connection, incident, _ = self._setup()
        result = service.split_incident(
            connection,
            incident_id=incident,
            sites=["S2"],
            actor="duty",
            as_of=AS_OF,
        )
        recheck = {item["complaint"]: item for item in result["complaint_recheck"]}
        self.assertIn("C2", recheck)
        new_incident = next(
            row for row in service.list_incidents(connection) if row["id"] == result["new_incident"]
        )
        self.assertEqual(new_incident["sites"][0]["status"], "reopened")

    def test_late_complaint_on_a_closed_incident_reopens_only_that_site(self):
        connection, incident, _ = self._setup()
        # Finish and truly close S2: complete its site responsibility, import
        # fresh good playback after the repair, then close with observation.
        service.complete_action(
            connection,
            action_id=connection.execute(
                "SELECT action_id FROM action_sites WHERE site='S2'"
            ).fetchone()["action_id"],
            site="S2",
            actor="A",
            cause="port",
            repair_scope="replace",
            completed_at=iso(85),
        )
        good = self.write_inputs(
            {
                "segments": [
                    dict(
                        segment(
                            "G",
                            iso(100),
                            iso(150),
                            95,
                            session="PG",
                            site="S2",
                            received="2026-11-20T00:00:00Z",
                        )
                    )
                ]
            },
            name="good-after",
        )
        service.import_inputs(connection, good, actor="duty")
        service.close_incident_site(
            connection,
            incident_id=incident,
            site="S2",
            since=iso(85),
            as_of=AS_OF,
            actor="duty",
        )
        # a late complaint about S2 arrives after the site responsibility closed
        late_payload = self.write_inputs(
            {
                "complaints": [
                    {
                        "id": "C9",
                        "site": "S2",
                        "occurred": iso(15),
                        "received": "2026-12-04T00:00:00Z",
                        "symptom": "freeze",
                    }
                ]
            },
            name="late-complaint",
        )
        service.import_inputs(connection, late_payload, actor="duty")
        linked = service.link_complaint(
            connection, complaint_id="C9", incident_id=incident, actor="duty", as_of=AS_OF
        )
        self.assertTrue(linked["changed"])
        self.assertTrue(linked["reopened_site"])
        detail = service.incident_detail(connection, incident, month="2026-11", as_of=AS_OF)
        statuses = {site["site"]: site["status"] for site in detail["sites"]}
        self.assertEqual(statuses["S2"], "reopened")
        self.assertEqual(statuses["S1"], "awaiting_observation")


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


class ReportTests(WorkspaceCase):
    def _ws(self):
        payload = make_sources(
            segments=[segment("A", iso(0), iso(15), 50, received="2026-11-20T00:00:00Z")],
            complaints=[
                {"id": "C1", "site": "S1", "occurred": iso(5), "received": "2026-11-20T01:00:00Z", "symptom": "freeze"}
            ],
        )
        return self.open_ws(payload)

    def test_published_report_keeps_its_own_as_of_and_dispute_scope(self):
        connection = self._ws()
        report = service.create_report(
            connection, month="2026-11", as_of="2026-11-21T00:00:00Z", actor="cm"
        )
        published = service.publish_report(
            connection, report_id=report["id"], actor="cm", recipients=["a", "b"]
        )
        self.assertEqual(published["as_of"], "2026-11-21T00:00:00+00:00")
        later = service.supplement_report(
            connection, month="2026-11", as_of="2026-12-02T00:00:00Z", actor="cm"
        )
        original = service.get_report(connection, report["id"])
        self.assertEqual(original["as_of"], "2026-11-21T00:00:00+00:00")
        self.assertEqual(later["supersedes"], report["id"])
        self.assertIn("supplement", later["payload"])
        self.assertEqual(later["payload"]["supplement"]["base_as_of"], original["as_of"])

    def test_late_correction_appears_as_a_linked_supplement(self):
        connection = self._ws()
        report = service.create_report(
            connection, month="2026-11", as_of="2026-11-21T00:00:00Z", actor="cm"
        )
        service.publish_report(connection, report_id=report["id"], actor="cm", recipients=["cm-client"])
        correction = self.write_inputs(
            {
                "segments": [
                    dict(
                        segment(
                            "A", iso(0), iso(15), 95, revision=2, received="2026-12-01T00:00:00Z"
                        )
                    )
                ]
            },
            name="late-correction",
        )
        service.import_inputs(connection, correction, actor="duty")
        supplement = service.supplement_report(
            connection, month="2026-11", as_of="2026-12-03T00:00:00Z", actor="cm"
        )
        changes = supplement["payload"]["supplement"]["changes"]
        self.assertEqual(changes["sites"][0]["site"], "S1")
        self.assertIn("bad_minutes", changes["sites"][0]["changes"])
        # the supplement's own diff points at the original, published as-of
        self.assertEqual(changes["base_as_of"], "2026-11-21T00:00:00+00:00")

    def test_supplement_publication_flags_older_versions_and_recipients(self):
        connection = self._ws()
        first = service.create_report(
            connection, month="2026-11", as_of="2026-11-21T00:00:00Z", actor="cm"
        )
        service.publish_report(
            connection,
            report_id=first["id"],
            actor="cm",
            recipients=["client-a", "client-b"],
        )
        supplement = service.supplement_report(
            connection, month="2026-11", as_of="2026-12-03T00:00:00Z", actor="cm"
        )
        service.publish_report(
            connection,
            report_id=supplement["id"],
            actor="cm",
            recipients=["client-a"],
        )
        progress = {
            item["id"]: item for item in service.report_progress(connection, month="2026-11")
        }
        self.assertEqual(progress[first["id"]]["superseded_by"], [supplement["id"]])
        self.assertEqual(progress[supplement["id"]]["superseded_by"], [])
        # client-b got v1 but not v2: the gap is explicit, not silent.
        events = [
            json.loads(row["detail"])
            for row in connection.execute(
                "SELECT detail FROM events WHERE report_id = ? AND kind = 'report.published'",
                (supplement["id"],),
            )
        ]
        self.assertEqual(events[0]["supersedes"], [first["id"]])
        self.assertEqual(events[0]["earlier_recipients_not_in_this_release"], ["client-b"])

    def test_report_progress_tracks_recipient_acknowledgements(self):
        connection = self._ws()
        report = service.create_report(
            connection, month="2026-11", as_of="2026-11-21T00:00:00Z", actor="cm"
        )
        service.publish_report(
            connection, report_id=report["id"], actor="cm", recipients=["a", "b"]
        )
        service.acknowledge_report(
            connection, report_id=report["id"], recipient="a", actor="a"
        )
        progress = service.report_progress(connection, month="2026-11")[0]
        self.assertEqual(progress["acknowledged"], ["a"])
        self.assertEqual(progress["pending"], ["b"])


class HandoverTests(WorkspaceCase):
    def test_handover_is_visible_to_all_three_roles(self):
        connection = self.open_ws(
            make_sources(segments=[segment("A", iso(0), iso(15), 95)])
        )
        incident = service.open_incident(
            connection, title="s", sites=["S1"], actor="duty", discipline="network"
        )
        service.add_handover(
            connection,
            actor="duty",
            incident_id=incident,
            note="S1 证据已核，等待现场窗口",
            next_step="08:00 安排 A",
        )
        detail = service.incident_detail(connection, incident, month="2026-11", as_of=AS_OF)
        self.assertEqual(len(detail["handovers"]), 1)
        report = service.create_report(
            connection, month="2026-11", as_of=AS_OF.isoformat(), actor="cm"
        )
        self.assertEqual(len(report["payload"]["handovers"]), 1)


class ImportTests(WorkspaceCase):
    def test_repeated_delivery_is_not_new_business_activity(self):
        payload = make_sources(segments=[segment("A", iso(0), iso(15), 95)])
        inputs = self.write_inputs(payload, name="inputs")
        workspace = self.tmp / "ws"
        first = service.init_workspace(workspace, inputs, actor="duty")
        self.assertEqual(first["new_rows"], len(sum(payload.values(), [])))
        connection = service.open_workspace(workspace)
        self.addCleanup(connection.close)
        second = service.import_inputs(connection, inputs, actor="duty")
        self.assertEqual(second["new_rows"], 0)
        self.assertGreater(second["duplicate_rows"], 0)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) AS c FROM imports").fetchone()["c"], 2
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"], 0
        )

    def test_new_version_records_are_kept_alongside_originals(self):
        payload = make_sources(segments=[segment("A", iso(0), iso(15), 50)])
        inputs = self.write_inputs(payload, name="inputs")
        workspace = self.tmp / "ws"
        service.init_workspace(workspace, inputs, actor="duty")
        connection = service.open_workspace(workspace)
        self.addCleanup(connection.close)
        correction = self.write_inputs(
            {
                "segments": [
                    segment("A", iso(0), iso(15), 95, revision=2, received="2026-12-01T00:00:00Z")
                ]
            },
            name="correction",
        )
        result = service.import_inputs(connection, correction, actor="duty")
        self.assertEqual(result["new_rows"], 1)
        rows = connection.execute(
            "SELECT COUNT(*) AS c FROM source_rows WHERE kind = 'segments'"
        ).fetchone()["c"]
        self.assertEqual(rows, 2)
        analysis = analyse_site(service.source_data(connection), "S1", "2026-11", AS_OF)
        self.assertEqual(analysis.totals[GOOD], 15.0)

    def test_workspaces_are_isolated(self):
        payload = make_sources(segments=[segment("A", iso(0), iso(15), 95)])
        inputs = self.write_inputs(payload, name="inputs")
        ws_a = self.tmp / "a"
        ws_b = self.tmp / "b"
        service.init_workspace(ws_a, inputs, actor="a")
        service.init_workspace(ws_b, inputs, actor="b")
        connection = service.open_workspace(ws_a)
        self.addCleanup(connection.close)
        service.open_incident(connection, title="only-a", sites=["S1"], actor="a")
        other = service.open_workspace(ws_b)
        self.addCleanup(other.close)
        self.assertEqual(service.list_incidents(other), [])
        self.assertEqual(len(service.list_incidents(connection)), 1)
        self.assertEqual(
            connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )

    def test_timestamps_without_timezones_are_rejected(self):
        payload = make_sources(
            segments=[segment("A", "2026-11-10T08:00:00", iso(15), 95)]
        )
        data = build_sources(payload)
        self.assertEqual(data.segments, [])
        self.assertIn("invalid-time", {finding.code for finding in data.findings})


if __name__ == "__main__":
    unittest.main()
