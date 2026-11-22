"""Command-line interface for the service-experience operations app.

All state lives in the workspace directory given as the first positional
argument.  Source input directories are read-only; results, workflow state,
handovers and reports are written to the workspace only.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import service
from .service import WorkflowError
from .timeutil import format_ts, now_utc, parse_ts


def _print(payload: Any, *, text: bool = False, title: str | None = None) -> None:
    if text:
        if title:
            print(f"== {title} ==")
        print(_render(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _render(payload: Any) -> str:
    sites = payload.get("sites") if isinstance(payload, dict) else None
    if (
        isinstance(sites, list)
        and sites
        and all(isinstance(site, dict) and "verdict" in site for site in sites)
    ):
        lines = []
        for site in payload["sites"]:
            lines.append(
                f"{site['site']:<4} 等级 {site.get('tier')}  坏 {site.get('bad_minutes')} 分钟"
                f" / 容忍 {site.get('tolerance_bad_minutes')}  未知 {site.get('unknown_minutes')}"
                f"  维护排除 {site.get('maintenance_excluded_minutes')}  -> {site.get('verdict')}"
            )
        overall = payload.get("overall") or {}
        if overall:
            lines.append(f"整体: {overall.get('conclusion')}")
            if overall.get("aggregate_masks_site_breach"):
                lines.append("注意: 整体达标数值掩盖了个别站点超限，不能据此出具达标结论。")
        return "\n".join(lines) if lines else json.dumps(payload, ensure_ascii=False, indent=2)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _actor(args: argparse.Namespace) -> str:
    return args.actor or "unknown"


def _as_of(args: argparse.Namespace) -> str:
    if getattr(args, "as_of", None):
        return format_ts(parse_ts(args.as_of, field="as_of"))
    return format_ts(now_utc())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="svcops",
        description="窄带视频服务体验运营：播放证据核算、整改派单、恢复确认与月度发布。",
    )
    parser.add_argument("workspace", help="工作目录（状态与结果写在这里，可独立并存）")
    parser.add_argument("--actor", default="duty", help="操作人/角色标识，写入审计事件")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化工作目录并导入只读资料来源")
    init.add_argument("--input", default="inputs", help="只读资料来源目录")
    init.add_argument("--text", action="store_true")

    imp = sub.add_parser("import", help="增量导入相同契约的新资料来源")
    imp.add_argument("--input", default="inputs")
    imp.add_argument("--text", action="store_true")

    month = sub.add_parser("month", help="核算某月各站点服务消耗与结论")
    month.add_argument("--month", required=True, help="YYYY-MM")
    month.add_argument("--as-of", dest="as_of")
    month.add_argument("--text", action="store_true")

    dash = sub.add_parser("dashboard", help="值班总览：合同、整改、投诉、发布进度")
    dash.add_argument("--month", required=True)
    dash.add_argument("--as-of", dest="as_of")
    dash.add_argument("--text", action="store_true")

    show = sub.add_parser("incident-show", help="查看整改单的证据、责任、观察与合同影响")
    show.add_argument("--incident", required=True)
    show.add_argument("--month", required=True)
    show.add_argument("--as-of", dest="as_of")
    show.add_argument("--text", action="store_true")

    listing = sub.add_parser("incidents", help="列出整改单")
    listing.add_argument("--status")
    listing.add_argument("--text", action="store_true")

    open_inc = sub.add_parser("incident-open", help="把相关事件归入整改单")
    open_inc.add_argument("--title", required=True)
    open_inc.add_argument("--sites", required=True, help="逗号分隔站点")
    open_inc.add_argument("--network")
    open_inc.add_argument("--discipline", default="network", choices=sorted(service.DISCIPLINES))
    open_inc.add_argument("--complaints", default="", help="逗号分隔投诉号")
    open_inc.add_argument("--impact-start", dest="impact_start")
    open_inc.add_argument("--impact-end", dest="impact_end")
    open_inc.add_argument("--note")
    open_inc.add_argument("--text", action="store_true")

    shared = sub.add_parser("incident-from-shared", help="按链路探测建立共享故障整改单")
    shared.add_argument("--network", required=True)
    shared.add_argument("--complaints", default="")
    shared.add_argument("--title")
    shared.add_argument("--as-of", dest="as_of")
    shared.add_argument("--text", action="store_true")

    single = sub.add_parser("incident-from-complaint", help="关联依据不足时单独立案调查投诉")
    single.add_argument("--complaint", required=True)
    single.add_argument("--title")
    single.add_argument("--as-of", dest="as_of")
    single.add_argument("--text", action="store_true")

    discipline = sub.add_parser("set-discipline", help="设置站点修复责任类型")
    discipline.add_argument("--incident", required=True)
    discipline.add_argument("--site", required=True)
    discipline.add_argument("--discipline", required=True, choices=sorted(service.DISCIPLINES))

    comps = sub.add_parser("complaints", help="投诉列表及关联依据")
    comps.add_argument("--as-of", dest="as_of")
    comps.add_argument("--text", action="store_true")

    link = sub.add_parser("complaint-link", help="凭依据把投诉关联到整改单")
    link.add_argument("--complaint", required=True)
    link.add_argument("--incident", required=True)
    link.add_argument("--as-of", dest="as_of")

    close_c = sub.add_parser("complaint-close", help="关闭投诉工单（不是服务恢复）")
    close_c.add_argument("--complaint", required=True)
    close_c.add_argument("--note", default="")
    close_c.add_argument("--text", action="store_true")

    plan = sub.add_parser("plan", help="为站点责任安排一次修复（可覆盖同一共享故障的多个站点）")
    plan.add_argument("--incident", required=True)
    group = plan.add_mutually_exclusive_group(required=True)
    group.add_argument("--site")
    group.add_argument("--sites", help="逗号分隔，一个共享工作安排覆盖多个站点")
    plan.add_argument("--discipline", required=True, choices=sorted(service.DISCIPLINES))
    plan.add_argument("--assignee", required=True)
    plan.add_argument("--start", required=True)
    plan.add_argument("--minutes", type=float, default=30.0)
    plan.add_argument("--scope", default="")
    plan.add_argument("--text", action="store_true")

    start = sub.add_parser("action-start", help="开始执行修复")
    start.add_argument("--action", required=True)

    complete = sub.add_parser("action-complete", help="处置者回填原因、完成时间与修复范围")
    complete.add_argument("--action", required=True)
    complete.add_argument("--site", required=True)
    complete.add_argument("--cause", required=True)
    complete.add_argument("--scope", required=True)
    complete.add_argument("--actual-minutes", dest="actual_minutes", type=float)
    complete.add_argument("--completed-at", dest="completed_at")
    complete.add_argument("--text", action="store_true")

    observe = sub.add_parser("observe", help="检查修复后的有效观察是否满足合同恢复条件")
    observe.add_argument("--incident", required=True)
    observe.add_argument("--site", required=True)
    observe.add_argument("--since", required=True)
    observe.add_argument("--as-of", dest="as_of", required=True)
    observe.add_argument("--text", action="store_true")

    close_site = sub.add_parser("close-site", help="在满足恢复条件后关闭站点责任")
    close_site.add_argument("--incident", required=True)
    close_site.add_argument("--site", required=True)
    close_site.add_argument("--since", required=True)
    close_site.add_argument("--as-of", dest="as_of", required=True)
    close_site.add_argument("--text", action="store_true")

    reopen = sub.add_parser("reopen-site", help="重新开启站点责任")
    reopen.add_argument("--incident", required=True)
    reopen.add_argument("--site", required=True)
    reopen.add_argument("--reason", required=True)

    split = sub.add_parser("split-incident", help="拆分被误判为共享的故障")
    split.add_argument("--incident", required=True)
    split.add_argument("--sites", required=True)
    split.add_argument("--title")
    split.add_argument("--as-of", dest="as_of")
    split.add_argument("--text", action="store_true")

    work = sub.add_parser("worklist", help="按本月剩余容忍、课程时段与等级排序的待办")
    work.add_argument("--month", required=True)
    work.add_argument("--as-of", dest="as_of")
    work.add_argument("--text", action="store_true")

    sched = sub.add_parser("schedule", help="按人员覆盖排出修复时段")
    sched.add_argument("--month", required=True)
    sched.add_argument("--as-of", dest="as_of")
    sched.add_argument("--after", required=True)
    sched.add_argument("--minutes", type=float, default=30.0)
    sched.add_argument("--incident", action="append", dest="incidents")
    sched.add_argument("--apply", action="store_true", help="实际落单")
    sched.add_argument("--risk-note", dest="risk_note", help="负责人承接覆盖不足风险")
    sched.add_argument("--text", action="store_true")

    handover = sub.add_parser("handover-add", help="保存交接说明")
    handover.add_argument("--note", required=True)
    handover.add_argument("--incident")
    handover.add_argument("--next-step", dest="next_step")
    handover.add_argument("--shift")

    handover_list = sub.add_parser("handovers", help="查看交接记录")
    handover_list.add_argument("--incident")
    handover_list.add_argument("--text", action="store_true")

    rep = sub.add_parser("report-create", help="生成月度草稿（保留数据截止点与争议范围）")
    rep.add_argument("--month", required=True)
    rep.add_argument("--as-of", dest="as_of", required=True)
    rep.add_argument("--kind", default="monthly")
    rep.add_argument("--supersedes")
    rep.add_argument("--note")
    rep.add_argument("--text", action="store_true")

    pub = sub.add_parser("report-publish", help="发布月报给收件人")
    pub.add_argument("--report", required=True)
    pub.add_argument("--to", required=True, help="逗号分隔收件人")
    pub.add_argument("--note")
    pub.add_argument("--text", action="store_true")

    reports = sub.add_parser("reports", help="月报与发布进度")
    reports.add_argument("--month")
    reports.add_argument("--text", action="store_true")

    diff = sub.add_parser("report-diff", help="比较两版报告的差异")
    diff.add_argument("--base", required=True)
    diff.add_argument("--other", required=True)
    diff.add_argument("--text", action="store_true")

    supp = sub.add_parser("report-supplement", help="为已发布月报生成迟到更正补充")
    supp.add_argument("--month", required=True)
    supp.add_argument("--as-of", dest="as_of", required=True)
    supp.add_argument("--note")
    supp.add_argument("--text", action="store_true")

    ack = sub.add_parser("report-ack", help="收件人确认收到指定版本")
    ack.add_argument("--report", required=True)
    ack.add_argument("--recipient", required=True)
    ack.add_argument("--text", action="store_true")

    return parser


def run(args: argparse.Namespace) -> Any:
    command = args.command
    if command == "init":
        return service.init_workspace(args.workspace, args.input, actor=_actor(args))
    connection = service.open_workspace(args.workspace)
    try:
        if command == "import":
            return service.import_inputs(connection, args.input, actor=_actor(args))

        if command == "month":
            _, analysis, _ = service.month_data(connection, args.month, _as_of(args))
            return analysis

        if command == "dashboard":
            return service.dashboard(connection, month=args.month, as_of=_as_of(args))

        if command == "incidents":
            return service.list_incidents(connection, status=args.status)

        if command == "incident-show":
            return service.incident_detail(
                connection, args.incident, month=args.month, as_of=_as_of(args)
            )

        if command == "incident-open":
            incident = service.open_incident(
                connection,
                title=args.title,
                sites=args.sites,
                actor=_actor(args),
                network=args.network,
                discipline=args.discipline,
                complaints=[item for item in args.complaints.split(",") if item.strip()],
                note=args.note,
                impact_start=args.impact_start,
                impact_end=args.impact_end,
            )
            return {"incident": incident}

        if command == "incident-from-shared":
            incident = service.open_incident_from_shared(
                connection,
                network=args.network,
                actor=_actor(args),
                as_of=_as_of(args),
                complaints=[item for item in args.complaints.split(",") if item.strip()],
                title=args.title,
            )
            return {"incident": incident}

        if command == "incident-from-complaint":
            incident = service.open_incident_from_complaint(
                connection,
                complaint_id=args.complaint,
                actor=_actor(args),
                as_of=_as_of(args),
                title=args.title,
            )
            return {"incident": incident}

        if command == "set-discipline":
            service.set_incident_site_discipline(
                connection, args.incident, args.site, args.discipline, actor=_actor(args)
            )
            return {"ok": True}

        if command == "complaints":
            return service.complaint_list(connection, as_of=_as_of(args))

        if command == "complaint-link":
            return service.link_complaint(
                connection,
                complaint_id=args.complaint,
                incident_id=args.incident,
                actor=_actor(args),
                as_of=_as_of(args),
            )

        if command == "complaint-close":
            return service.close_complaint(
                connection, complaint_id=args.complaint, actor=_actor(args), note=args.note
            )

        if command == "plan":
            return service.plan_action(
                connection,
                incident_id=args.incident,
                site=args.site,
                sites=[item for item in (args.sites or "").split(",") if item.strip()],
                discipline=args.discipline,
                assignee=args.assignee,
                start=args.start,
                minutes=args.minutes,
                scope=args.scope,
                actor=_actor(args),
            )

        if command == "action-start":
            return service.start_action(connection, action_id=args.action, actor=_actor(args))

        if command == "action-complete":
            return service.complete_action(
                connection,
                action_id=args.action,
                site=args.site,
                actor=_actor(args),
                cause=args.cause,
                repair_scope=args.scope,
                actual_minutes=args.actual_minutes,
                completed_at=args.completed_at,
            )

        if command == "observe":
            return service.observation_check(
                connection,
                incident_id=args.incident,
                site=args.site,
                since=args.since,
                as_of=args.as_of,
                actor=_actor(args),
            )

        if command == "close-site":
            return service.close_incident_site(
                connection,
                incident_id=args.incident,
                site=args.site,
                since=args.since,
                as_of=args.as_of,
                actor=_actor(args),
            )

        if command == "reopen-site":
            return service.reopen_incident_site(
                connection,
                incident_id=args.incident,
                site=args.site,
                actor=_actor(args),
                reason=args.reason,
            )

        if command == "split-incident":
            return service.split_incident(
                connection,
                incident_id=args.incident,
                sites=args.sites,
                actor=_actor(args),
                title=args.title,
                as_of=_as_of(args),
            )

        if command == "worklist":
            items = service.worklist(connection, month=args.month, as_of=_as_of(args))
            return {"month": args.month, "items": items}

        if command == "schedule":
            plan = service.build_schedule(
                connection,
                month=args.month,
                as_of=_as_of(args),
                after=args.after,
                minutes=args.minutes,
                incidents=args.incidents,
            )
            if args.apply:
                plan["applied"] = service.apply_schedule(
                    connection, schedule=plan, actor=_actor(args), risk_note=args.risk_note
                )
            return plan

        if command == "handover-add":
            return {
                "handover": service.add_handover(
                    connection,
                    actor=_actor(args),
                    note=args.note,
                    incident_id=args.incident,
                    next_step=args.next_step,
                    shift=args.shift,
                )
            }

        if command == "handovers":
            return service.list_handovers(connection, incident_id=args.incident)

        if command == "report-create":
            return service.create_report(
                connection,
                month=args.month,
                as_of=args.as_of,
                actor=_actor(args),
                kind=args.kind,
                supersedes=args.supersedes,
                note=args.note,
            )

        if command == "report-publish":
            return service.publish_report(
                connection,
                report_id=args.report,
                actor=_actor(args),
                recipients=[item for item in args.to.split(",") if item.strip()],
                note=args.note,
            )

        if command == "reports":
            return service.report_progress(connection, month=args.month)

        if command == "report-diff":
            return service.diff_reports(connection, base_id=args.base, other_id=args.other)

        if command == "report-supplement":
            return service.supplement_report(
                connection, month=args.month, as_of=args.as_of, actor=_actor(args), note=args.note
            )

        if command == "report-ack":
            return service.acknowledge_report(
                connection,
                report_id=args.report,
                recipient=args.recipient,
                actor=_actor(args),
            )

        raise WorkflowError(f"未知命令: {command}")
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalise_actor(argv))
    try:
        payload = run(args)
    except (WorkflowError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    _print(payload, text=getattr(args, "text", False))
    return 0


def _normalise_actor(argv: list[str] | None) -> list[str]:
    """Allow ``--actor`` anywhere on the command line.

    ``argparse`` only defines it on the top-level parser, so an operator who
    appends it after the subcommand would otherwise get a usage error.  Pull it
    to the front while leaving every other token untouched.
    """

    if argv is None:
        argv = sys.argv[1:]
    tokens = list(argv)
    actor: str | None = None
    cleaned: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--actor":
            if index + 1 >= len(tokens):
                raise SystemExit("--actor 需要一个值")
            actor = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--actor="):
            actor = token.split("=", 1)[1]
            index += 1
            continue
        cleaned.append(token)
        index += 1
    if actor is not None:
        cleaned = ["--actor", actor, *cleaned]
    return cleaned


if __name__ == "__main__":
    sys.exit(main())
