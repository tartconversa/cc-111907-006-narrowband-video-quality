# 窄带视频服务的体验分层与整改派单

一个只依赖 Python 标准库的本地运营应用：把播放器会话、网络探测、站点合同和
投诉接成“值班判断 → 负责人安排修复 → 客户经理按月出具结论”的连续流程。
本仓库中的资料全部为合成记录，不接入真实视频，也不需要外部账号。

- 资料来源契约：`INPUT_CONTRACT.md`、`inputs/schema.json`
- 业务实现：`svcops/`
- 业务测试：`tests/test_business.py`、`tests/test_cli.py`
- 资料完整性检查：`tests/test_source_contract.py`

`inputs/` 始终只读；所有工作状态、人工操作、交接和月报都保存在调用者指定的
工作目录中。不同工作目录互不影响。

## 快速开始

```bash
# 1. 初始化工作目录并导入只读资料（只需要一次）
python -m svcops --actor duty ./work init --input inputs

# 2. 值班：查看当月各站点合同消耗（--text 输出便于阅读的摘要）
python -m svcops --actor duty ./work month --month 2026-11 \
    --as-of 2026-11-23T00:00:00Z --text

# 3. 值班：从高丢包链路建立共享故障整改单，并挂上依据充分的投诉
python -m svcops --actor duty ./work incident-from-shared \
    --network N1 --complaints C1,C2 --as-of 2026-11-23T00:00:00Z

# 4. 负责人：用一个共享工作安排覆盖两个站点，或分别安排
python -m svcops --actor lead ./work plan --incident INC-0001 \
    --sites S1,S2 --discipline network --assignee A \
    --start 2026-11-17T08:00:00Z --scope "更换交换机端口"

# 5. 处置者：开始并回填原因、完成时间、修复范围与实际耗时
python -m svcops --actor A ./work action-start --action ACT-0001
python -m svcops --actor A ./work action-complete --action ACT-0001 --site S1 \
    --cause "端口丢包" --scope "更换端口" \
    --actual-minutes 25 --completed-at 2026-11-17T08:25:00Z

# 6. 值班：用修复后新增的有效好播放检查恢复条件
python -m svcops --actor duty ./work observe --incident INC-0001 --site S1 \
    --since 2026-11-17T08:25:00Z --as-of 2026-11-23T00:00:00Z --text
python -m svcops --actor duty ./work close-site --incident INC-0001 --site S1 \
    --since 2026-11-17T08:25:00Z --as-of 2026-11-23T00:00:00Z

# 7. 客户经理：按当时数据截止点发布月报，迟到更正以关联补充发布
python -m svcops --actor cm ./work report-create --month 2026-11 \
    --as-of 2026-11-23T00:00:00Z
python -m svcops --actor cm ./work report-publish \
    --report RPT-2026-11-001 --to client-a,client-b
python -m svcops --actor cm ./work report-supplement --month 2026-11 \
    --as-of 2026-12-03T00:00:00Z
```

`--actor` 既可以写在子命令前，也可以作为全局参数放在任意位置；
每次操作都会写入 `events` 审计表。

## 三种使用者看到的是同一份事实

同一工作目录、同一份证据、同一套责任状态：

| 角色 | 主要入口 | 看到什么 |
|---|---|---|
| 值班员 | `month`、`dashboard`、`incidents`、`incident-show`、`worklist`、`handovers`、`handover-add` | 谁受影响、证据缺口、当前责任、后续观察、合同影响、交接记录 |
| 处置者/负责人 | `plan`、`action-start`、`action-complete`、`schedule`、`split-incident`、`reopen-site` | 人员时段、现场窗口、修复范围、完成回填、共享单号下的站点独立责任 |
| 客户经理 | `report-create`、`report-publish`、`reports`、`report-diff`、`report-supplement`、`report-ack` | 当时的数据截止点、争议范围、收件人确认、迟到更正补充 |

`incident-show` 同时返回：站点影响时段、合同核算、播放证据与缺口、探测证据、
投诉关联依据、当前工作安排、观察记录、审计事件和交接说明。

## 业务规则如何落地

**播放证据**

- 同一会话的相邻或重叠片段先做区间并集，绝不重复累计时长。
- `id` 的较新 `revision` 替代旧版本（版本只对同一 `id` 有先后关系）。
- 同一版本内容冲突、或不同片段在同一时刻给出互相冲突的已知质量，则该时段记为
  “争议”，不挑好值；来源冲突作为数据问题保留。
- `quality` 为空既不算好也不算坏，单独列为“未核实覆盖”；会话跨度内的采样缺口
  单独披露。没有观察到的时段不能推断为良好。
- 跨站点迁移、同站点链路切换都按会话身份解释并单独标记；各站点的时段仍然分别核算。

**合同核算**

- 只有合同月窗口内、非计划维护时段、可核实的坏播放消耗 `monthly_bad_minutes`。
- 计划维护只排除自身的实际区间，不提供恢复证据，也不会把维护外的坏体验一并抵消。
- 坏分钟按站点、按各自合同分别核算，不做站间调剂。月报的 `overall` 只是背景，
  并显式标记 `aggregate_masks_site_breach`；同合同的 `contracts` 汇总也不合并分钟。

**整改与派单**

- 共享链路故障可以关联多个站点，但每个站点的 `incident_sites` 行分别保存影响时段、
  等级、责任类型、证据缺口和状态。
- 一个工作安排可以覆盖多个站点共用（`plan --sites`），但完成动作按站点记录：
  `action-complete --site S1` 不会把 S2 标成完成。
- 人员可用时段、技能与已占用时段会被校验；重叠分派直接拒绝。一次已确认修复按实际
  占用时段排他，后续排期据此顺延。
- 排期按本月剩余容忍、受影响课程时段、等级排序；覆盖不足时 `requires_risk_ack` 为真，
  没有负责人的 `--risk-note` 不允许落单，且返回具体未覆盖站点与原因。

**恢复与关闭**

- 关闭站点责任前必须存在已完成修复，且观察起点不得早于最近一次修复完成时间。
- 恢复证据只计算修复后**新进入系统**（`received` 晚于修复）、质量良好、且在计划维护
  排除区间之外的播放；不足时返回“等候确认”，关闭被拒绝。
- 投诉工单关闭（`complaint-close`）明确标记 `service_restoration_claimed=false`，
  不改变整改单的恢复要求。测试专门区分这两件事。

**拆分与迟到事实**

- 关联依据不足的投诉可以 `incident-from-complaint` 单独立案；只有坏播放证据或覆盖
  时刻的高丢包探测才算依据，仅时间接近或质量未知不算。强行并入会被拒绝。
- `split-incident` 把误判为共享的站点拆出：已有工作安排**改指**新单，不复制；
  实际耗时、完成人、修复范围保留。受影响站点责任改为 `reopened`，并重新核对投诉依据。
- 整改单关闭后补交投诉，`complaint-link` 只重开该站点责任（及必要时的整改单），
  不复制已完成的现场动作。
- 有效会话更正会改变分析和待办排序（`late_facts`、`late_corrections`），但不会撤销
  正在执行的修复。

**月报**

- 每份月报保存生成时的 `as_of`、当时的争议与数据问题，内容持久化在 `reports.payload`。
- `report-supplement` 以新版本关联原版本，`supersedes` 指向旧版，并附带
  `sites / new_segments_after_base_as_of / new_complaints_after_base_as_of / new_corrections`
  差异，避免不同收件人看到无说明的不同结论。
- `reports` 展示每个收件人的确认与待确认状态。

## 工作目录与结果

```
./work/workspace.db     # SQLite：导入事实、整改、工作安排、观察、交接、月报、审计事件
```

- 资料来源以内容哈希幂等导入：重复投递同一事实只计为 duplicate，不新增业务活动。
- 新版本事实与旧版本并存，由分析层按版本规则解释。
- 删除或更换工作目录即可完全隔离；`inputs/` 永不写入。

## 环境与测试

使用 Python 3.14，无第三方依赖。

```bash
python -m unittest discover -s tests -v
```

- `tests/test_source_contract.py`：资料字段、时区与清单哈希。
- `tests/test_business.py`：覆盖重叠/修订/争议/未知质量、维护排除、按站点合同、
  共享故障拆分、独立完成、人员不重叠、恢复观察、投诉依据、迟到更正与月报版本。
- `tests/test_cli.py`：值班 → 负责人 → 客户经理的端到端命令行流程。

容器环境以 Dockerfile 中的公开基础镜像摘要锁定：

```bash
docker build -t local-svcops .
docker run --rm --network none local-svcops
```

`docker run` 默认只执行上述测试；业务演练请挂载工作目录后在容器内运行
`python -m svcops ...`。所有输入与操作均为本地合成数据，不涉及真实视频或真实个人。
