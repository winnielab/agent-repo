# RepoPilot Stage 3 基础设施实现

日期：2026-10-01

## 1. 本轮边界

本轮实现 Stage 3 的基础设施任务卡 S3-01、S3-02、S3-03、S3-04，以及
S3-06 的 provider-neutral runner。默认执行仍保持 `max_concurrency=1`；尚未把
Stage 2 的完整 Analyze/Explore/Plan/Review/Evaluate 流程迁移到可恢复 Runtime，
因此不宣称 `repopilot resume RUN_ID` 或完整并行 solve 已交付。

## 2. 参考设计

- OpenAI Agents API 将系统分为 Harness、Environment 和 Application Server；
  RepoPilot 对应保持 AgentRuntime、ExecutionEnvironment、Workflow/Coordinator
  三层边界，不把调度、工作区生命周期或恢复逻辑放入模型循环。
  资料：[OpenAI Agents API Architecture](https://developers.openai.com/api/docs/guides/agents-api/architecture)。
- DeepSeek Harness 将持久 Session Event 与进程内 Agent Event 分开，并把
  subagent 与 workflow 作为可替换 capability seam。RepoPilot 因此把 journal
  作为恢复权威事实，把 EventBus 保留为实时观测；Coordinator 只持有 Worker
  callable，不依赖具体模型、Provider 或 Tool。
  资料：[DeepSeek Harness Architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)、
  [Subagent subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/subagent.md)、
  [Workflow subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/workflow.md)。

## 3. 已实现组件

### TaskGraph lease 与生命周期

`TaskRecord` 新增 lease owner/expiry、attempt、workspace/base commit、patch hash、
result commit、merge/verification 状态和 agent session identity。`claim_ready` 在
文件锁内 reload、recover、claim 和持久化，避免两个本地进程领取同一任务；过期
lease 进入 `RETRYABLE`，不会静默伪装成新任务。

状态扩展为：

```text
PENDING/RETRYABLE → CLAIMED → RUNNING → PRODUCED
                                      → MERGING → VERIFYING → COMPLETED
任何活动状态 → FAILED；失败依赖继续传播为 BLOCKED
```

### AgentCoordinator

- 从 TaskGraph 原子领取 ready task。
- `max_concurrency` 可配置，默认 1。
- 每个 assignment 有独立 worker id、lease owner、attempt 和稳定 run id。
- Worker 异常转为该 Task 的失败结果，不取消无依赖的其他 Worker。
- Coordinator 只调用 Worker contract，不持有 Prompt、Provider、Tool 或 Context。

### Task-bound Worktree

`TaskWorkspaceProvider` 只允许当前 lease owner 取得 workspace，绑定精确 base
commit，并以 task/attempt 形成 workspace identity。`TaskWorkspaceLease` 将 patch
原子写入 artifact 目录并计算 SHA-256；失败路径不会自动删除 worktree。

### MergeManager

- 校验每个 patch 的 SHA-256 与 target HEAD/base commit。
- 按 base-coordinate hunk 区分 disjoint、same-file non-overlap 与 overlap conflict。
- 所有 patch 先在一次 `git apply --check` 中预检，再一次性 apply，避免部分合并。
- 冲突时保留原始 patch；成功后调用统一 verifier，并返回 merge classification、
  changed files 和 tests。

### Workflow Runtime / Journal / Checkpoint

- journal 为 append-only JSONL，每条记录包含 sequence、previous checksum 和当前
  SHA-256；replay 检查序号、run id 和完整 hash chain。
- checkpoint 使用临时文件、`fsync`、`os.replace` 原子发布，并指向 journal 的
  sequence/checksum。
- Step 使用稳定 `run_id:step_key` idempotency key；已完成 Step 在 resume 时跳过。
- 单 run 使用非阻塞文件锁，拒绝并发 resume。
- crash 时处于 RUNNING 的 Step 先记录 interrupted，再重新进入 pending。

### Evaluation Runner

- 统一 `BenchmarkTask` 与 `EvaluationRecord`。
- 每题保存独立 record，重复运行可按 task + variant 恢复。
- 汇总文件全部从真实 record 计算，输出 `summary.json`、`tasks.jsonl`、
  `metrics.csv`、`report.md`。
- 已包含 resolve/test/regression/localization、iterations、tool calls、tokens、cost、
  runtime 和 human intervention 指标。

## 4. 验证

新增测试覆盖：

- lease 持久化、过期恢复与 attempt 增长；
- 两个独立 Worker 的并发上限与单 Worker 故障隔离；
- disjoint patch 合并和 same-file overlap 的无污染拒绝；
- workflow 失败后 resume，已完成 Step 不重复执行；
- journal 篡改检测；
- Evaluation Runner 的真实汇总产物与 record resume。

本轮最终产品回归：`133 passed`；`compileall` 与 CLI smoke test 通过。

## 5. 尚未完成，不能提前宣称

> 后续状态（2026-10-01）：第 1、4、5 项所述的端到端 wave scheduler、独立
> worker runtime/worktree、批量 merge/verify/commit 已在 `--workflow stage3`
> 中完成，并用五个真实 SWE-bench 任务验证。实现、缺陷修复和复测结果见
> `RepoPilot_Stage3_Coordinator实现与五项复测_20261001.md`。本节保留的是基础设施
> 快照当时的边界，不应再被解释为当前代码状态。

1. Stage 2 `solve_issue` 尚未改为 task-bound worktree + wave merge 的完整 Stage 3
   composition root；当前单 worktree 流程仍是默认生产路径。
2. 尚未提供真正继续业务步骤的 `repopilot resume RUN_ID` CLI。底层 Runtime 已有
   resume 语义，但 CLI 需要先保存并重建 solve invocation、Provider config 和
   phase handler registry。
3. GitHub Integration 尚未实现；在本地 merge/resume 闭环稳定前不创建远端 PR。
4. Evaluation Runner 已能生成统一产物，但还没有完成六组真实 ablation，也没有
   50~100 题新评测结果。
5. 并发 solve 仍需 wave scheduler：同一 ready frontier 从共同 base 执行，批量
   merge/verify/commit 后再释放下一层依赖。不能直接让 Stage 2 共享 ToolContext、
   UsageTracker、WorkingMemory 或 worktree。

## 6. 下一实施顺序

1. 将 Analyze/Explore/Plan 与 ready frontier 包装成 Workflow Step，保存可重建的
   run manifest。
2. Coordinator 每个 frontier 创建 fresh Runtime/Context/ToolContext/worktree。
3. 同一 frontier 的 WorkerResult 批量交给 MergeManager，统一测试后再完成 Task。
4. 保存 merge commit、TaskGraph、预算和 Reviewer/Evaluator 结果，再开放 CLI resume。
5. `max_concurrency=1` 通过 E2E 后，使用两个 disjoint task 提升到 2。
6. 最后接 GitHub draft/approval 与真实 ablation。
