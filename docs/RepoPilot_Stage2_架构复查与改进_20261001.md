# RepoPilot Stage 2 架构复查、改进与 16 项验收

日期：2026-10-01。

## 1. 结论先行

RepoPilot 的初始分层方向是正确的：稳定 Agent Loop、Provider/Tool/Environment 解耦、隔离 worktree、结构化任务与计划、Reviewer 和 GoalEvaluator 分离。这些边界使项目能够从 Stage 1 单 Agent 闭环演进到 Stage 2，而不需要推翻 Runtime。

Stage 2 未通过验收的原因不是组件数量不足，也不是没有多 Agent，而是“从问题描述到候选补丁语义”的证据链仍有断点。统一冻结版在 16 个 Gold-valid SWE-bench Verified 中型任务上得到 7/16 resolved（43.75%），低于 50% 目标；同时 7/16 的内部状态与官方 Oracle 不一致。

本轮改进不提前用并发掩盖质量问题，先收紧四个通用闭环：

1. 当前阶段与未来阶段显式分区。
2. Planner 局部坏字段只做局部恢复，不再整份退化。
3. 将“依赖事实”与“候选补丁语义”拆成两级反证。
4. 语义证据绑定补丁 revision，补丁再次修改后旧证据自动失效。

多 Agent 作为 Stage 3 的执行与隔离能力设计，不作为 Stage 2 正确率的替代品。

## 2. 从最初设计复查

### 2.1 保留的正确决策

最初三阶段计划中的以下原则应继续作为架构约束：

- `AgentRuntime` 只负责模型、工具、结果循环，不承载仓库业务逻辑。
- 工具通过 Registry 装配，Agent 只声明角色提示与工具集合。
- Provider 通过统一 `LLMClient` 隔离。
- 命令只通过 `ExecutionEnvironment` 执行。
- 任务在 Git worktree 内修改，源仓库保持不变。
- Permission、Hook、Event、Usage 围绕工具边界工作。
- Reviewer 检查补丁风险，GoalEvaluator 检查用户目标，两者不能合并。
- TaskGraph、WorkingMemory 和 Workflow/Resume 是不同层级。

这些决策与当前公开 Agent Harness 的共同方向一致：Harness、执行环境与应用编排应分离；工具、会话、模型适配和协调能力应通过清晰接口组合，而不是不断向核心循环添加分支。

### 2.2 初始设计中需要修正的部分

#### Runtime 仍知道具体工具语义

虽然设计要求 Runtime 与具体工具解耦，原实现仍硬编码 `edit_file/write_file/delete_file` 和 `run_tests`。本轮改成由工具声明 `mutates_workspace`、`validates_workspace` 能力，Runtime 从 Registry 查询。以后新增 patch 工具或验证工具不再修改 Agent Loop。

#### TaskSpecification 只描述行为，没有描述行为何时生效

原 `BehaviorCase` 能区分 temporal，但无法表达“现在警告、未来改变”。Planner 因而可能同时实现互斥阶段，或者仅凭 Issue 中的未来提案提前改变当前版本。

本轮增加 `temporal_scope=always|current|future`。Explorer 必须用 checkout 的版本文件、release note、changelog 或当前测试建立 `active_temporal_phase`；证据不足时降级为 `unknown`，并采取保守策略：未来行为不提前启用。

#### Plan 校验是 all-or-nothing

原实现只要遗漏一个 behavior key，就把整份计划替换成通用 fallback。这样会丢掉已经正确的 root cause、源码证据和实现步骤。

本轮改成 partial recovery：

- 保留合法 summary、root cause、invariant、step 和 evidence。
- 删除未知 evidence、非法依赖和重复 key。
- 只把遗漏的 active behavior 补入可执行步骤。
- 只补缺失的 dependency check 或 candidate semantic check。
- 只有 JSON 完全不可解析或恢复后仍不合法时才使用整份 fallback。

#### “做了 probe”不等于“证明补丁语义”

原 `HypothesisCheck` 把两种不同命题混在一起：

- dependency fact：当前 API、调用链或版本中存在什么。
- candidate semantics：候选修改是否对代表性输入保持了正确含义。

例如 formatter 返回 `1e6` 是依赖事实，但不能证明把 `1e6` 拼到每个 label 后仍表示正确数值。新的 `kind=dependency_fact|candidate_semantics` 明确区分两者；每个 active behavior 必须被 candidate semantic check 覆盖。

#### 验证证据没有生命周期

原命令证据只是一段日志。Repair 再次修改源码后，旧 probe 仍可能被 Evaluator 当成当前候选的证明。

本轮引入 `mutation_revision` 和 `record_evidence`：

- 每次成功写入、编辑或删除都会增加 revision。
- focused Bash probe 可用 `evidence_for` 关联 check key。
- `record_evidence` 保存 supported、contradicted 或 inconclusive；候选语义若要标记 supported，必须关联同一 revision 上成功且以 `evidence_for` 标记的 Bash probe，不能只提交自然语言断言。
- candidate semantic evidence 必须发生在至少一次修改之后。
- Evaluator 只接受与当前 revision 一致的 supported evidence。
- 缺失、矛盾或过期证据形成结构化 Repair 反馈。
- Worker 完成门也检查这些证据；缺失或过期时继续执行反证步骤，不能以“测试已过”直接结束。

## 3. 参考 Harness 后的组件设计

### 3.1 Codex 可借鉴的边界

OpenAI 官方 Agents API 架构把系统拆成三块：Harness 负责模型和工具循环及会话，Environment 负责命令和文件，Application Server 负责提交任务、接收事件和管理自托管环境生命周期。RepoPilot 对应关系为：

| Codex 架构概念 | RepoPilot 当前/目标组件 |
|---|---|
| Harness | `AgentRuntime` + Agent role + Context |
| Environment | Local/Docker environment + worktree |
| Application orchestration | `SolveIssueWorkflow`，未来 `WorkflowRuntime` |
| Session progress | EventBus；未来 durable journal + live events |
| Tools | ToolRegistry + PermissionManager |

直接借鉴的是边界，不是托管形态。RepoPilot 仍是本地、Provider-neutral Harness。

资料：[OpenAI Agents API Architecture](https://developers.openai.com/api/docs/guides/agents-api/architecture)。

### 3.2 DeepSeek Harness 可借鉴的能力缝隙

DeepSeek Harness 将 session、system prompt、tools、agent、agent loop 和 scope 拆成可替换插件；持久 Session Event 与实时 Agent Event 分开；subagent provider、workflow engine 和 agent teams 都是独立 capability seam。

RepoPilot 不需要复制 Cordis，但应继承以下原则：

- 核心循环可替换能力，不知道具体 Provider、Tool 或 Subagent 后端。
- 持久事实与进程内控制事件分离。
- 子 Agent 创建、继续、取消、发现和销毁有显式生命周期。
- 工作流拥有子 Agent 生命周期；模型只是提出委派，不拥有资源回收。
- fresh child 和 history-seeded child 是不同语义，不能用同一个布尔参数含糊表示。
- 多 Agent 共享的是任务协议和有限 handoff，不共享无限父会话。

资料：[DeepSeek Harness Architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)、[Subagent capability family](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/README.md)、[Workflow capability family](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/workflow/README.md)。

## 4. 改进后的 Stage 2 数据流

```text
Issue
  ↓
TaskAnalyzer
  ├─ acceptance criteria
  └─ behavior matrix + temporal_scope
  ↓
RepositoryExplorer (read-only, bounded)
  ├─ definition/caller/state/test/dependency evidence
  └─ checkout temporal phase + cited version evidence
  ↓
Planner
  ├─ active/deferred behavior partition
  ├─ root cause + earliest invariant
  ├─ dependency_fact checks
  ├─ candidate_semantics checks
  └─ partial-plan recovery
  ↓
CoderWorker
  ├─ inspect / dependency falsification
  ├─ edit → mutation_revision + 1
  ├─ semantic probe + record_evidence
  ├─ configured tests
  └─ completion gate: current revision evidence complete
  ↓
Reviewer + GoalEvaluator
  ├─ tests/review/diff/source contracts
  ├─ deferred future behavior guard
  ├─ current revision semantic evidence
  └─ structured counterexample
  ↓ fail/unknown
Repair task → edit invalidates stale evidence → re-verify
```

## 5. 多 Agent 目标设计

### 5.1 何时拆分

只在 TaskGraph 中存在真正独立、可合并的 ready tasks 时并行。单文件缺陷、同一因果链的探索/编码/验证不拆成多个写 Agent。Explorer、Reviewer、Evaluator 是角色边界，不等于必须启动独立写进程。

### 5.2 核心对象

```text
AgentCoordinator
  ├─ TaskClaimStore       原子 claim / lease / heartbeat / expiry
  ├─ ChildAgentProvider   fresh / forked / external
  ├─ WorkspaceProvider    task-bound worktree
  ├─ MergeManager         patch identity / conflicts / merge
  ├─ VerificationGate     merge 后统一测试与目标验收
  └─ WorkflowJournal      durable events / snapshot / resume lock
```

`TaskRecord` 需要增加：

- `lease_owner`、`lease_expires_at`、`attempt`。
- `workspace_id`、`base_commit`。
- `result_patch_sha256`、`result_commit`。
- `merge_status`、`verification_status`。
- `parent_task_id`、`agent_session_id`。

### 5.3 生命周期

```text
PENDING
  → CLAIMED (lease acquired)
  → RUNNING (child + worktree ready)
  → PRODUCED (patch and evidence persisted)
  → MERGING
  → VERIFYING
  → COMPLETED

任何阶段失败：保存 patch/evidence → FAILED 或 RETRYABLE
lease 过期：只有 journal 确认无活动执行后才能重新 claim
```

### 5.4 隔离和合并

- 每个写 Worker 使用独立 worktree 与独立 ToolContext。
- Usage/Event/WorkingMemory 不使用可变共享实例。
- read-only Agent 可以读取同一不可变 base snapshot。
- MergeManager 先验证 base commit 和 patch hash。
- disjoint files 可顺序应用；same-file 必须做 hunk overlap 和语义复审。
- 合并失败保留所有原始 patch，不清理工作区。
- 合并成功后统一运行 configured tests、Reviewer 和 GoalEvaluator。
- Worker 自己测试通过不代表合并结果通过。

### 5.5 会话和 handoff

- 默认 fresh child，只接收 TaskRecord、有限 repository evidence、预算和父 run identity。
- 只有任务明确需要父轨迹时才 fork history。
- child 输出结构化 `WorkerResult`：status、summary、evidence、patch、tests、next step、blocker。
- 不用完整 transcript 作为 handoff；workspace 和 journal 才是权威状态。
- follow-up、interrupt、cancel、dispose 分开建模。

### 5.6 持久事件与实时事件

持久事件：task claimed、workspace created、patch produced、merge result、test result、budget consumed、terminal status。它们可重放并用于 resume。

实时事件：token stream、当前 tool、临时进度、steering。进程退出后可以丢失，不能作为恢复依据。

## 6. 代码改动映射

| 机制 | 位置 |
|---|---|
| temporal scope | `task/specification.py` |
| checkout phase evidence | `repo/explorer.py`、`agents/explorer.py` |
| behavior partition / partial recovery | `planning/plan.py`、`planning/planner.py` |
| revision-aware evidence | `tools/evidence.py`、`tools/editing.py`、`tools/shell.py` |
| capability-based Runtime | `tools/registry.py`、`core/runtime.py` |
| semantic/deferred evaluation | `agents/evaluator.py` |
| pre-completion evidence gate | `core/runtime.py`、`workflows/solve_issue.py` |
| patch hygiene | `agents/contracts.py` |
| coder protocol | `agents/worker.py`、`prompts/coder.md` |
| workflow assembly | `workflows/solve_issue.py` |

## 7. 验证协议

### 7.1 产品测试

```bash
uv run pytest -q
uv run python -m compileall -q repopilot scripts tests
uv run python -m repopilot --help
```

改进前基线为 116 passed；正式 16 题所用冻结版为 120 passed；评测后再补齐完成门、probe provenance、临时 probe 清理、Analyzer 字段恢复和 phase version-evidence guard 后，当前开发树为 125 passed。

### 7.2 16 项 SWE-bench

数据集固定使用：

```text
benchmarks/swebench_verified_medium_v1_gold_valid_semantic_v1.json
instance id hash: 08bc47d89f8b3815bf027c775a70fa8308e44843a9b778648e5a72c1c7e3fa47
```

协议：

- 16 个任务不替换，保持原 ordinal。
- 每题先跑 Gold；Gold 失败不调用模型。
- exact prepared image 必须为 `linux/amd64`。
- Agent 只看到 problem statement、base checkout 和公开测试命令。
- 不向 Agent 提供 gold patch、test patch 或 hints。
- 单 worker 串行，避免 16 GB 机器资源竞争。
- 每个非空 patch 都交给官方 SWE-bench Oracle。
- 内部 resolved 只作诊断，官方 Oracle 是最终状态。
- 新结果写入独立目录，不覆盖旧 7/16 基线。

验收目标：

- 产品测试全绿。
- 16/16 有独立记录，0 未解释基础设施失败。
- 16/16 非空 patch 并接受官方评测，或明确记录真实 empty patch。
- official resolved 至少 8/16，达到 Stage 2 的 50% Gate。
- 同时报告内部/官方 mismatch，不用内部状态替代 official score。

## 8. 本轮结果

正式结果：**7/16 resolved（43.75%），Stage 2 Gate 未通过**。与旧冻结基线的总分相同；题目层面新增通过 `mwaskom__seaborn-3069`，但 `django__django-11333` 从通过回退为未通过。16/16 Gold 预检通过，16/16 产生非空 patch 并接受官方评测，0 个基础设施失败。

评测冻结目录：`/Users/ayc/agent/repopilot-agent-freezes/repopilot-semantic-v2-20261001`。

官方结果目录：`/Users/ayc/agent/repopilot-swebench-results/stage2-medium-v2-semantic-evidence`。

可复现性摘要：冻结源码树（排除 `.venv`/cache）SHA-256 为 `7e376b0f8399a13647bfff8b28cb6b9edb17cefe53cdcc11ede607c1e29b0b73`；结果 `summary.json` SHA-256 为 `e5faf56442b99beb94113e042dbd42d95b048aeab2d5f79db249a6aa1b368ccb`；manifest 文件 SHA-256 为 `91dd0dc898fcfc7b4daf5d3ef52ee2d0bd173516ad40c681269b6bd7e9aadf29`。前述 `08bc...` 是有序 instance-id 集合 hash，不是 manifest 文件 hash。

| Ordinal | Instance | Official | Internal | 备注 |
|---:|---|---|---|---|
| 1 | `astropy__astropy-7671` | resolved | failed | 官方通过，内部测试环境/Reviewer 假阴性 |
| 2 | `django__django-11211` | resolved | resolved | 一致通过 |
| 4 | `mwaskom__seaborn-3069` | resolved | failed | 相对旧基线新增通过；内部语义判定假阴性 |
| 7 | `pylint-dev__pylint-4604` | unresolved | failed | 探针已否定候选，但 Worker 仍结束 |
| 8 | `pytest-dev__pytest-8399` | resolved | failed | 官方通过；内部生成版本文件缺失，且临时 probe 泄漏 |
| 9 | `scikit-learn__scikit-learn-14053` | resolved | resolved | 一致通过 |
| 10 | `sphinx-doc__sphinx-9602` | unresolved | failed | 只处理 bool，且 `Text` 节点标记方式错误 |
| 11 | `sympy__sympy-23824` | resolved | failed | 官方通过；二次修改使内部证据正确过期 |
| 12 | `astropy__astropy-13236` | unresolved | failed | Analyzer 因单字段不一致整份回退，阶段选择错误 |
| 13 | `django__django-11333` | unresolved | resolved | 修了部分 caller，没有恢复 `get_resolver` 自身缓存不变量 |
| 14 | `matplotlib__matplotlib-23412` | resolved | failed | 官方通过；内部图像 probe 假阴性，临时 probe 泄漏 |
| 15 | `mwaskom__seaborn-3187` | unresolved | failed | 当前 formatter 时点的 offset 为 0，反证已失败却未继续修复 |
| 16 | `psf__requests-6028` | unresolved | failed | Repair 后只剩无语义的引号改动，目标代码路径选错 |
| 18 | `pylint-dev__pylint-6386` | unresolved | failed | 修改 argparse Action 层，遗漏参数预处理协议 |
| 19 | `pytest-dev__pytest-7324` | unresolved | failed | 把 `True/False/None` 当常量，破坏它们作为 marker 名的语义 |
| 20 | `scikit-learn__scikit-learn-10908` | unresolved | resolved | 直接 `dict(vocabulary)`，没有复用 vocabulary 正规化路径 |

内部状态只有 4/16 resolved。内部与官方共 7/16 不一致：5 个假阴性（1、4、8、11、14），2 个假阳性（13、20）。这说明内部 Gate 的 recall 有所增强，但 precision 和测试环境可信度仍不足；不能用“更严格”替代“更准确”。

资源记录：6,473,289 input tokens、202,375 output tokens、总墙钟 5,321.56 秒；成本字段由 Provider 返回为空，不能臆测费用。

### 8.1 失败归类

1. **规格恢复与阶段证据仍过脆**：Astropy 13236 的 Analyzer 正确输出 current/future scope，却因 dimension 未同步为 temporal 而整份 fallback；Explorer 又只凭旧分支仍存在来判断当前阶段，没有版本证据。评测后已改为字段级规范化，并要求非 unknown phase 至少引用一条 `kind=version` 的 checkout/release 证据，否则降级为 unknown。
2. **最早不变量/目标位置选择错误**：Django 11333、Requests 6028、Pylint 6386 修改了 caller 或表层 Action，而非拥有语义的入口。
3. **表示语义丢失**：Sphinx 9602、Pytest 7324、scikit-learn 10908 对 Literal、标识符和 vocabulary 类型做了过窄转换。
4. **反证不能约束执行**：Pylint 4604、Seaborn 3187 已得到候选失败证据，但冻结版只在末端 Evaluator 拒绝。评测后已将 revision-current evidence 前移为 Worker 完成门。
5. **证据声明缺少来源校验**：冻结版允许模型把自然语言断言登记为 supported。评测后 candidate semantics 的 supported 必须关联同 revision、成功且带 `evidence_for` 的 Bash probe。
6. **内部环境与判定假阴性**：5 个官方 resolved 被内部拒绝；生成版本文件、Reviewer 规则和自制 probe 需分别校准。
7. **补丁卫生**：2 个官方通过补丁带有临时 probe 文件。评测后通用文件名规则已覆盖 `probe_*`。

### 8.2 冻结版与当前开发树的边界

7/16 只代表评测开始前冻结的 `repopilot-semantic-v2-20261001`。运行中/复盘后新增的五项修正——Worker 完成门、probe provenance、`probe_*` 卫生规则、Analyzer temporal 字段恢复、phase version-evidence guard——已通过产品测试，但**尚未重跑这 16 题**，不得计入本次官方分数或用于宣称 Gate 通过。

## 9. Stage Gate 决策规则

- `resolved >= 8/16`：Stage 2 质量 Gate 通过，可以进入 Stage 3 Coordinator 的串行实现，再逐步开启并发。
- `resolved < 8/16`：Stage 2 仍未通过。保留本轮产物，按失败类别继续修语义闭环；不得用多 Agent 吞吐或旧简单集分数替代。
- 无论分数如何，先实现 journal、lease、task-bound worktree 和 merge verification，再把 `max_concurrency` 提升到 2。
