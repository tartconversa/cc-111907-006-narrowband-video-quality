"""End-to-end CLI test: operators drive the same workspace through the shell."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from svcops.cli import main

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="svcops-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = str(self.tmp / "ws")

    def call(self, *arguments: str) -> dict | list:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(list(arguments))
        self.assertEqual(code, 0, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_duty_to_manager_flow(self):
        init = self.call(
            "--actor", "duty", self.workspace, "init", "--input", str(ROOT / "inputs")
        )
        self.assertGreater(init["new_rows"], 0)
        self.assertEqual(init["findings"], [])

        month = self.call(
            "--actor", "duty", self.workspace, "month", "--month", "2026-11",
            "--as-of", "2026-11-23T00:00:00Z",
        )
        sites = {site["site"]: site for site in month["sites"]}
        self.assertFalse(sites["S1"]["within_tolerance"])
        self.assertEqual(month["overall"]["tier1_sites_in_breach"], ["S1"])

        incident = self.call(
            "--actor", "duty", self.workspace, "incident-from-shared",
            "--network", "N1", "--complaints", "C1,C2",
            "--as-of", "2026-11-23T00:00:00Z",
        )["incident"]

        plan = self.call(
            "--actor", "lead", self.workspace, "plan", "--incident", incident,
            "--sites", "S1,S2", "--discipline", "network", "--assignee", "A",
            "--start", "2026-11-17T08:00:00Z", "--scope", "共享链路修复",
        )
        self.call("--actor", "A", self.workspace, "action-start", "--action", plan["action"])
        done = self.call(
            "--actor", "A", self.workspace, "action-complete", "--action", plan["action"],
            "--site", "S1", "--cause", "交换机端口", "--scope", "更换端口",
            "--completed-at", "2026-11-17T08:30:00Z",
        )
        self.assertEqual(done["action_status"], "partially_completed")

        waiting = self.call(
            "--actor", "duty", self.workspace, "observe", "--incident", incident,
            "--site", "S1", "--since", "2026-11-17T08:30:00Z",
            "--as-of", "2026-11-23T00:00:00Z",
        )
        self.assertFalse(waiting["sufficient"])
        self.assertIn("等候确认", waiting["status"])

        self.call(
            "--actor", "duty", self.workspace, "handover-add",
            "--incident", incident, "--note", "S1 等新播放证据", "--next-step", "导入补充片段",
        )
        detail = self.call(
            "--actor", "duty", self.workspace, "incident-show", "--incident", incident,
            "--month", "2026-11", "--as-of", "2026-11-23T00:00:00Z",
        )
        self.assertEqual(detail["handovers"][0]["note"], "S1 等新播放证据")
        statuses = {site["site"]: site["status"] for site in detail["sites"]}
        self.assertEqual(statuses["S1"], "awaiting_observation")
        self.assertEqual(statuses["S2"], "open")

        report = self.call(
            "--actor", "cm", self.workspace, "report-create", "--month", "2026-11",
            "--as-of", "2026-11-23T00:00:00Z",
        )
        published = self.call(
            "--actor", "cm", self.workspace, "report-publish",
            "--report", report["id"], "--to", "client-a,client-b",
        )
        self.assertEqual(published["as_of"], "2026-11-23T00:00:00+00:00")
        self.call(
            "--actor", "client-a", self.workspace, "report-ack",
            "--report", report["id"], "--recipient", "client-a",
        )
        progress = self.call("--actor", "cm", self.workspace, "reports", "--month", "2026-11")
        self.assertEqual(progress[0]["acknowledged"], ["client-a"])
        self.assertEqual(progress[0]["pending"], ["client-b"])

    def test_actor_may_follow_the_subcommand(self):
        self.call(self.workspace, "init", "--input", str(ROOT / "inputs"), "--actor", "duty")
        month = self.call(
            self.workspace, "month", "--month", "2026-11",
            "--as-of", "2026-11-23T00:00:00Z", "--actor", "night-shift",
        )
        self.assertEqual(len(month["sites"]), 3)

    def test_business_refusal_returns_error_exit_code(self):
        self.call("--actor", "duty", self.workspace, "init", "--input", str(ROOT / "inputs"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main([
                "--actor", "duty", self.workspace, "complaint-link",
                "--complaint", "C3", "--incident", "INC-9999",
                "--as-of", "2026-11-23T00:00:00Z",
            ])
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(stdout.getvalue()))


if __name__ == "__main__":
    unittest.main()
