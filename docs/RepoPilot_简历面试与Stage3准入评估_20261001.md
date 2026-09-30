# RepoPilot 简历、面试介绍与 Stage 3 准入评估

日期：2026-10-01

适用范围：简历项目经历、技术面试项目介绍、阶段复盘、Stage 3 开发决策。

> 本文只基于现有代码、历史测试记录和真实评测产物进行整理。本次没有重新运行产品测试、DeepSeek 或 SWE-bench。

## 1. 项目定位

RepoPilot 是一个面向真实 GitHub Issue 的自主代码修复 Agent。它能够在隔离 Git worktree 中分析任务、按需理解仓库、生成结构化计划、修改代码、运行测试、复用独立 PR Reviewer、逐条评估验收标准，并在失败后执行有限次数的自动修复。

项目不是把现有 PR Review Agent 改名后继续堆功能，而是建立独立的通用 Agent Harness：

```text
Issue
  → TaskAnalyzer / BehaviorContract
  → RepositoryExplorer / Evidence Slice
  → Planner / TaskGraph / HypothesisCheck
  → CoderWorker / AgentRuntime / ToolRegistry
  → Tests / PRReviewAdapter / GoalEvaluator
  → RepairLoop / SolveReport
```

现有 PR Review Agent 被保留为领域组件，通过 Adapter 复用；模型、运行时、工具、执行环境、权限、上下文、规划和评审各自保持清晰边界。

## 2. 简历写法

### 2.1 项目标题

**RepoPilot：面向真实代码仓库的自主修复 Agent 与可复现评测系统**

### 2.2 一句话介绍

基于 Python 构建 provider-neutral Coding Agent Harness，支持任务分析、渐进式仓库理解、结构化任务图、隔离 worktree、工具权限、自动测试、PR Review 复用、验收评估与反例驱动修复，并使用 SWE-bench Verified 进行真实回归。

### 2.3 推荐简历要点

- 设计并实现通用 Coding Agent Runtime，将 LLM、工具注册、权限控制、执行环境、上下文压缩、Token/Runtime 预算和事件日志解耦，支持 DeepSeek 等 OpenAI-compatible provider。
- 建立任务自适应仓库理解链路，按“项目指令—定义—caller/state—邻近测试”加载证据；Planner 必须引用可验证 observation，避免全仓库上下文灌入和无证据规划。
- 将 Issue 转换为 positive、negative、boundary、temporal、preservation、failure-path 行为契约，并引入可证伪根因假设、独立 GoalEvaluator 和反例驱动 Repair Loop。
- 构建安全执行边界：所有修改位于独立 Git worktree；Bash/Test 经过 PermissionManager；工具层阻止越权测试修改；Patch Hygiene 拦截临时脚本、调试文件和非预期测试变更。
- 复用已有 PR Review Agent，并通过 Adapter 统一 timeout 和权威测试证据，避免 Reviewer 重复长测试或与 Coding Workflow 产生相反结论。
- 建立适配 16GB Apple Silicon 的串行 SWE-bench 流水线，包含 amd64 镜像检查、Gold preflight、固定 base commit、非空 patch、官方 Oracle、断点结果和内容寻址冻结。
- 完成 20 个中型任务真实统计；统一冻结版本对 16 个 Gold-valid 任务产生 16/16 可评分非空 patch、官方 resolved 7/16（43.75%）；产品自动化测试历史记录达到 116 passed。

### 2.4 简历指标如何解释

可以写：

> 完成 20 个 SWE-bench Verified 中型任务的真实串行评测，其中 16 个通过 Gold 环境门禁；统一冻结版本实现 16/16 非空 patch、7/16 官方 resolved，并保留 internal/official confusion matrix 用于诊断 false positive/negative。

不建议写：

- “SWE-bench 通过率 100%”；真实官方 resolved 不是 100%。
- “20 个任务全部成功”；20 个任务是执行 Gate 完成，不是全部解决。
- “v6 修复了三个历史失败 PR”；v6 验证了 Harness 改进，但三个任务官方仍 unresolved。
- “完全复现 Claude Code/Codex”；项目只借鉴了稳定循环、渐进式知识加载、压缩、workflow 和 goal gate 等设计原则。

## 3. 面试介绍

### 3.1 30 秒版本

> RepoPilot 是我做的自主代码修复 Agent。它接收真实 Issue，在隔离 worktree 中完成项目理解、规划、编辑、测试、评审和自动修复。我没有把能力都堆进一个 Prompt，而是拆成通用 Runtime、Tool/Permission、Explorer、Planner、Coder、Reviewer Adapter 和 GoalEvaluator。工程上重点解决了 Agent 只读不修改、Token 被理解阶段耗尽、遗留测试误报、长测试重复执行、临时文件污染 patch 和内部判断与官方 Oracle 不一致等问题，并用 20 个 SWE-bench Verified 中型任务做了真实评测。

### 3.2 两分钟版本

> 项目的起点是一个 PR Review Agent，它能审查 patch，但不能自主解决 Issue。我保留 Reviewer，通过 Adapter 接入新的 RepoPilot Harness，而不是让 Reviewer 同时承担编码、执行和决策。
>
> 第一层是稳定 Agent Runtime，只负责 model—tool call—result 循环，并统一管理权限、上下文、预算和事件。第二层是任务理解：Analyzer 把 Issue 变成行为契约，Explorer 只读取任务相关定义、caller、状态路径和测试，Planner 的每个步骤必须引用这些证据。第三层是闭环：Coder 在独立 worktree 中修改，测试结果交给 Reviewer 和独立 GoalEvaluator；如果验收项失败，Evaluator 返回 counterexample 和 invalidated assumption，Repair Loop 再次调用 Coder。
>
> 我遇到的最大问题不是模型不会生成代码，而是 Harness 会让一个看似合理的错误补丁通过。例如公开测试通过，但补丁改错了语义层；或者 Reviewer 固定 180 秒重跑测试导致内部失败；或者模型为了让测试变绿直接修改测试文件。我分别用分阶段预算、权威测试复用、工具边界守卫、Patch Hygiene、行为矩阵和可证伪假设处理这些问题。
>
> 最终 20 个任务都有真实统计，统一冻结版在 16 个 Gold-valid 任务上得到 7 个官方 resolved。这个结果没有达到 50% 质量目标，但执行可靠性已经稳定，剩余瓶颈被定位到时间阶段判断、候选语义反例和 Planner fallback。当前结构条件允许进入 Stage 3 的 Coordinator、task worktree、merge 和 resume 开发，但不应把 Stage 2 描述成质量完全达标。

### 3.3 STAR 表达

**Situation**：原有 PR Agent 只能审查 patch；Coding Agent 在真实仓库中会无界读取、耗尽 token、误判既存测试失败，并提交表面正确但隐藏语义错误的补丁。

**Task**：构建一个能够自主解决 Issue、保留旧 Reviewer、在 16GB Apple Silicon 上可重复评测，并能解释失败原因的 Agent Harness。

**Action**：

1. 拆分 Runtime、Tools、Environment、Permission、Context 和 Provider。
2. 用 BehaviorContract、RepositoryObservation、Plan 和 TaskGraph 建立结构化任务链。
3. 增加分阶段预算、no-mutation checkpoint、baseline-aware tests 和权威测试复用。
4. 增加独立 GoalEvaluator、反例 Repair、SourceContractGuard 和 PatchHygieneGuard。
5. 使用 Gold preflight、固定 base commit、官方 Oracle、内容哈希冻结和逐题结果文件保证评测可信。

**Result**：产品回归历史记录 116 passed；完成 20 项中型任务执行 Gate；统一冻结版 16/16 产生可评分 patch、7/16 官方 resolved；消除 empty-patch 和多数基础设施型失败，并将剩余问题归类为可继续开发的 Agent 设计问题。

## 4. 关键错误、改进与结果

| 问题 | 根因 | 通用措施 | 已验证结果 | 当前状态 |
|---|---|---|---|---|
| Coder 长时间只读、最终空 patch | Prompt 无法强制收敛 | Runtime 维护 mutation 状态，按轮次和 token 压力撤回只读工具 | homogeneous rerun 16/16 非空 patch | 已解决执行可靠性问题 |
| Explorer 耗尽主 Coder 预算 | 理解和编码共用一个上限 | Explorer 独立 iteration/tool/token ceiling | v6 使用固定 120k Explorer 预算 | 已实现 |
| Repair 配置存在但没有剩余 token | 单一全局预算 | implementation、repair、evaluation、finalization 分段预留 | Repair 能真实启动并记录 token pressure | 已实现，仍需按任务复杂度优化 |
| 遗留项目基线非全绿 | 只看进程 return code | 修改前后提取稳定 failure IDs，比较新增失败集合 | 能区分既存失败和候选回归 | 已实现 |
| Reviewer 重跑长测试并固定 180 秒 | 旧 Reviewer 协议与新 Workflow 不一致 | Adapter 复用权威测试结果并继承顶层 timeout | 避免重复固定超时 | 已实现 |
| 调试脚本或测试修改进入 patch | 只在模型 Prompt 中约束 | 编辑工具前置 test-path guard，Evaluator 最终 hygiene | v6 #12 三次测试编辑均被阻止 | 已真实验证 |
| Provider 返回 JSON 后附加文本 | 结构化解析要求整段只有 JSON | 解析首个完整 JSON，再由调用方做 schema 验证 | 消除 `Extra data` 类解析失败 | 已实现 |
| 冻结目录名与实际入口版本不一致 | 复制 `.venv` 保留绝对 shebang | 冻结时排除 `.venv`，重新 `uv sync`，校验 module path 和 shebang | v5 作废；v6 路径验证通过 | 已解决流程问题 |
| 多阶段提案选错当前阶段 | Analyzer 未确定 checkout 的 active phase | 建议增加 version/changelog/test-supported temporal phase | v6 #12 仍 unresolved | Stage 3 前后均可继续修复 |
| Probe 证明依赖事实但未证明补丁语义 | hypothesis check 粒度过弱 | 建议区分 dependency fact 与 candidate semantic property | v6 #15 内部 pass、官方 unresolved | 未解决 |
| Planner 少覆盖一个 behavior 后整份 fallback | all-or-nothing schema validation | 建议 partial-plan recovery，保留有效证据并只补缺失映射 | v6 #16 fallback 丢失具体根因 | 未解决 |

## 5. 当前代码状态

### 5.1 已存在的 Stage 2 核心

- `TaskSpecification` / `BehaviorCase`：结构化任务和行为矩阵。
- `RepositoryExplorer` / `RepositoryObservation`：任务相关证据切片。
- `Plan` / `PlanStep` / `HypothesisCheck`：因果计划和反证检查。
- `TaskGraph` / `TaskRecord`：持久化 DAG 和任务状态。
- `CoderWorker`：按 TaskRecord 建立独立模型 Context。
- `PRReviewAdapter`：只读复用旧 Reviewer。
- `GoalEvaluator`：逐项 PASS/FAIL/UNKNOWN。
- `solve_issue`：顺序执行 ready task，并在评估失败后自动创建 repair task。
- `WorkspaceManager`：Issue 级隔离 worktree。
- 串行 SWE-bench Runner：Gold、workspace、solve、official evaluation、summary。

### 5.2 当前尚不存在的 Stage 3 核心

代码中尚未发现以下正式组件：

- `AgentCoordinator` 和可配置并发 Worker 调度。
- TaskRecord 到独立 worktree/lease 的持久绑定。
- `MergeManager`、same-file 冲突分析和合并后验证。
- Workflow step snapshot、append-only journal、幂等 step key 和恢复锁。
- `repopilot resume RUN_ID`。
- GitHub Issue/PR Integration。
- 完整输出 `tasks.jsonl`、`metrics.csv`、`report.md` 的通用 Evaluation Runner。
- Coder Only 到 Full RepoPilot 的真实 ablation。

### 5.3 工作区状态

- 当前有 33 个 tracked 文件处于未提交修改状态，约 818 insertions / 55 deletions。
- 存在未跟踪 `.DS_Store`，本次未删除或修改。
- `docs/` 和 `reports/` 当前受 `.gitignore` 影响，文档虽保存在文件系统，但不会自然进入 Git 版本历史。
- 最近一次记录的产品测试为 2026-09-30 的 `116 passed`；本次按要求未重新测试。
- 有效冻结为 `repopilot-comprehension-v6-20260930`，其源码归档和评测结果可复核。

## 6. Stage 3 准入检查

实施指南要求进入 Stage 3 前证明六项能力：

| 准入条件 | 证据 | 判断 |
|---|---|---|
| TaskSpecification、Plan、TaskGraph 为结构化对象 | 已有独立 dataclass/schema 和持久化 DAG | 通过 |
| Coder 按 Task 工作 | `CoderWorker.run(TaskRecord, ...)`，workflow 顺序获取 ready task | 通过 |
| Reviewer 不修改代码 | Adapter 只消费 workspace/diff/tests 并输出结论 | 通过 |
| GoalEvaluator 按验收标准判断 | acceptance、behavior、hypothesis 和 deterministic guards | 通过 |
| RepairLoop 自动再次调用 Coder | evaluation 未通过时创建 repair task 并重新调用 Worker | 通过 |
| 20 个中型任务有真实统计 | 20 项执行 Gate 已完成，并保存逐题结果和官方 Oracle | 通过 |

结构化准入条件为 **6/6 通过**。

但 Stage 2 任务卡还提出 Resolve Rate 不低于 50% 的质量目标。统一冻结版本对 Gold-valid 任务的结果为 7/16，即 43.75%，因此质量目标为 **未通过**。

## 7. 是否可以进入第三阶段

结论：**可以进入 Stage 3 基础设施开发，但属于有条件 Go，不是 Stage 2 质量完全验收。**

可以开始的原因：

- Stage 3 所依赖的结构化对象、Task DAG、按任务 Worker、Review/Evaluate/Repair 闭环和真实统计都已经存在。
- 当前剩余的语义质量问题与 Coordinator、task-bound worktree、journal/resume 等工程能力可以正交推进。
- Stage 3 的隔离、合并和恢复能力反而能为后续语义迭代提供更可靠的实验基础。

不能直接全面并行的原因：

- 当前 workflow 是单 Issue worktree 和单 ToolContext，直接并发会引入工作区、UsageTracker、EventBus、WorkingMemory 和测试资源竞争。
- TaskRecord 尚未保存 worktree、lease、commit/patch 和 merge 状态。
- 当前工作区尚未提交，Stage 3 若直接大改会让 Stage 2 基线难以回滚和评审。
- 43.75% 低于质量目标，不能把并发吞吐提升包装成正确率提升。

## 8. Stage 3 开发前置动作

建议在真正修改 Stage 3 代码前完成以下非功能性动作：

1. 审阅并提交当前 33 个 tracked 文件，或至少建立新的内容寻址基线；不要在脏工作区上同时混入 Coordinator 大改。
2. 决定是否将 `docs/`、`reports/` 从 `.gitignore` 中拆出，使设计决策和实验报告进入版本历史。
3. 处理 `.DS_Store`：确认后删除并加入 ignore，避免污染后续状态判断。
4. 把 v6 freeze ID、归档哈希和最近一次 116 passed 写入 Stage 3 基线元数据。
5. 明确 Stage 3 初期默认 `max_concurrency=1`，先验证协调协议，再逐步升到大于 1。

## 9. 推荐 Stage 3 实施顺序

### 第一步：S3-01 AgentCoordinator，先保持串行

只实现调度协议，不立即并行：

- 从 TaskGraph 原子领取 ready task；
- 每个 Worker 使用新 Context；
- Worker 结果和失败独立记录；
- 配置 `max_concurrency`，默认 1；
- Coordinator 不包含模型提示词和仓库业务逻辑。

### 第二步：S3-02 Task-bound Worktrees

- TaskRecord 增加 workspace identity、lease owner、base commit、result commit/patch；
- 每个 Worker 只写自己的 checkout；
- lease 生命周期覆盖完整 Worker turn；
- 失败保留 worktree 和事件产物。

### 第三步：S3-03 MergeManager

- 先做文件集合和 base commit 校验；
- 区分 disjoint、same-file non-overlap、真实冲突；
- 合并失败不删除 Worker patch；
- 合并成功后进入统一测试/评估状态。

### 第四步：S3-04 Journal 与 Resume

- step key 幂等；
- append-only journal；
- snapshot 包含 TaskGraph、leases、Worker result、merge state 和预算；
- 同一 run 使用恢复锁；
- 增加 `repopilot resume RUN_ID`。

完成恢复语义后，再把 `max_concurrency` 从 1 提升到 2，验证两个互不相交任务。不要在没有 journal、lease 和 merge 状态时直接启用并行。

### 第五步：S3-06 Evaluation Runner 与 S3-07 Ablation

- 统一生成 `summary.json`、`tasks.jsonl`、`metrics.csv`、`report.md`；
- 运行 Coder Only、+Planner、+Context、+Reviewer、+Goal Loop、Full RepoPilot；
- 所有指标从事件和真实产物计算，禁止手填。

### 最后：S3-05 GitHub Integration

只有本地 workflow、恢复、合并和评测稳定后再接 GitHub；默认停在生成 branch/patch/PR body，人类批准后才创建 PR。

## 10. Stage 3 期间仍需并行跟踪的语义债务

Stage 3 是工程化阶段，但以下 Stage 2 质量债务不能被遗忘：

1. **Active temporal phase**：从 checkout version、changelog 和现有测试确定当前阶段。
2. **Candidate semantic property**：Probe 必须检验候选转换本身，而不只是依赖 API 事实。
3. **Partial-plan recovery**：结构化 Plan 少字段时保留有效部分，而不是整份 generic fallback。
4. **测试执行去重**：多个 PlanStep 不应各自重复同一长测试，再由 workflow 运行第三次。
5. **预算自适应**：按任务复杂度与长测试成本分配 Explorer、Worker、Repair 和 Evaluation 预算。

这些问题可以作为 Stage 3 Evaluation/Ablation 的观察维度，但不要在 S3-01 中混入修复，以免 Coordinator 任务失去边界。

## 11. 最终面试总结

> RepoPilot 当前已经从单一 Agent Loop 演进为有结构化任务、证据化项目理解、隔离执行、独立评审和目标评估的自主修复系统。我的核心贡献不是让模型“多写几次代码”，而是把不稳定行为转化为 Runtime、权限、预算、证据和评测协议。20 个真实任务证明执行链路已经可审计，但 43.75% 的官方 resolved 也说明语义正确性仍是主要瓶颈。依据实施指南，进入 Stage 3 的六项结构条件已经全部满足，因此可以开始 Coordinator 和 task-bound worktree；不过我会保持默认串行，先建立 lease、merge 和 resume，再开启并发，并继续用官方 Oracle 区分吞吐提升与正确率提升。
