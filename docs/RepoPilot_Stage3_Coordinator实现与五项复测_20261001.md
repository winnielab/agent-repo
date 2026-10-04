# RepoPilot Stage 3 Coordinator 实现与五项复测

日期：2026-10-01

## 1. 结论

本轮已经交付真正的 Stage 3 composition root，而不是在 Stage 2 串行流程外套一层
“多 Agent”名称。命令行使用 `--workflow stage3` 后，任务由 TaskGraph、
AgentCoordinator、独立 worker worktree、原子合并、合并后统一验证和有界修复共同
驱动。Stage 2 仍是默认 workflow，不受本次入口变更影响。

修复后五项最新有效结果为 **4/5 official resolved（80%）**、**5/5 非空补丁**、
**0/5 基础设施失败**。这五条记录来自真实 Stage 3 Coordinator，但为开发中针对
已定位缺陷的逐项复测，预算并不完全相同，因此是工程验证集合，不是一次冻结同配置
排行榜。统一 800k 首轮结果另行保留为 2/5，不能用最终聚合覆盖。

## 2. Coordinator 的真实执行语义

```text
Analyze / Explore / Plan（根工作树，只执行一次）
                    │
                    ▼
          TaskGraph ready frontier
                    │
       AgentCoordinator（有界并发）
          ┌─────────┴─────────┐
          ▼                   ▼
  worker A isolated WT  worker B isolated WT
  fresh Runtime/Tools   fresh Runtime/Tools
  Usage/Memory/Event    Usage/Memory/Event
          └─────────┬─────────┘
                    ▼
       persist patch + SHA-256（即使达到限额）
                    │
       pre-merge hygiene / overlap detection
                    │
                    ▼
       atomic apply → unified test → commit wave
                    │
          成功后才释放依赖任务
                    ▼
        Review / Evaluate / bounded repair
                    │
                    ▼
       从原始 base 到最终 wave HEAD 的补丁
```

关键不变量如下：

1. Coordinator 只处理 assignment、lease 和 WorkerResult，不持有模型、Prompt、工具或
   ToolContext。
2. 同一 frontier 的 worker 从共同 base 开始，但拥有彼此隔离的 worktree、
   AgentRuntime、ToolContext、UsageTracker、WorkingMemory 和 JSONL 事件流。
3. worker 已经修改代码后即使以 `max_tokens` 或 provider 错误结束，补丁也先持久化为
   `produced`，随后仍可接受合并验证；“Agent 没有优雅结束”不再等于“丢弃代码”。
4. frontier 补丁先做 hash/base 校验、卫生检查和整批 `git apply --check`；整批应用后
   统一测试，并把已应用候选 commit 成可审计、可继续 repair 的稳定 base。只有验证成功
   的任务会完成并释放依赖。
5. 下游任务只在前一波完成 merge、verify、commit 且验证成功后变为 ready，因此不会
   读取尚未合并或验证失败的 worker 私有状态。
6. 最终报告从原始 base commit 到最新 wave HEAD 生成，已 commit 的中间波次不会从
   最终 diff 中消失。

## 3. 实测暴露并修复的设计缺陷

### 3.1 失败/限额后补丁丢失

旧 Stage 2 路径在 worker 返回失败后先分支退出，再采集工作树；Django 的真实编辑因
随后达到 token 上限而被当成空补丁。本轮同时修复 Stage 2 和 Stage 3：worker 返回后
先采集/持久化变更，再解释终止状态，并增加对应端到端回归测试。

### 3.2 预算按并发槽而不是待执行任务分配

第一版把剩余 token 除以 `max_concurrency`。但一个 frontier 可能有三个任务、并发槽
只有两个，排队的第三个仍会执行，导致整个 wave 超支。修复后初始工作按所有
`PENDING/RETRYABLE` 任务分摊，修复波次按当前 ready 集分摊，并预留 finalization、
evaluation 和 repair 预算。Explorer 也最多使用扣除固定保留额后预算的三分之一，
防止默认 200k 配置在进入 worker 前就耗尽实现额度。该修复同时解决了依赖链后段
只剩 1 token 的问题。

### 3.3 同文件并行冲突只有拒绝，没有恢复路径

MergeManager 正确检测到了 Seaborn 三个 worker 对同一文件的重叠修改，但原设计只
拒绝整波，无法继续。现在冲突补丁继续作为有界审计产物保留，并自动创建一个从干净
共同 base 出发的单 worker consolidation task；最多执行一次冲突收敛，防止无限循环。

### 3.4 补丁卫生检查发生得太晚

Pylint 的早期运行把 `tcprobe_*.py` 临时探针合入最终补丁。现在卫生检查位于 merge
之前；违规 artifact 保留供审计但绝不进入目标工作树，并自动创建只允许生产代码的
clean consolidation task。规则也从仅匹配 `probe_*` 扩展到文件名中嵌入 probe 标记
的情况。

### 3.5 合并后的证据身份不稳定

worker-local mutation revision 不能直接与另一个 worktree 或最终目标 revision 比较。
现在只聚合实际已 merge worker 的 command/verification evidence，并将其重新绑定到
持久化的最终目标 revision，避免 evaluator 把私有工作树序号当成全局事实。

### 3.6 lease 领取加锁，但后续状态迁移仍可能覆盖新状态

早期实现只在 `claim_ready` 和 `renew_lease` 中执行跨进程 reload-and-lock。另一个
TaskGraph 实例若持有旧快照，`mark_running`、workspace 绑定或完成/失败迁移可能把
刚持久化的 lease 字段覆盖掉。最终实现让所有图结构和生命周期写操作都在文件锁内
重新加载后校验并迁移，并用两个同时存在的 TaskGraph 实例验证旧快照不能破坏 lease。

## 4. 自动化验证

新增/扩展测试覆盖：

- Coordinator 接受 `produced` partial patch；
- 两个隔离 worker 的 Stage 3 合并端到端；
- worker 达到限额后仍保存补丁；
- 同文件重叠补丁自动创建 consolidation task；
- 临时探针补丁在 merge 前拒绝并由干净任务收敛；
- `tcprobe_*` 一类嵌入式 probe 文件名的卫生规则；
- 双 TaskGraph 实例持有不同快照时的原子状态迁移；
- 默认总预算下 Explorer 必须为实现与 repair 保留额度；
- 原 Stage 2 repair 修改后达到限额仍保留 diff。

最终产品回归为 `141 passed`；`compileall`、`git diff --check` 和 CLI help smoke test
均通过。

## 5. 真实 SWE-bench 复测方法

- 数据集：SWE-bench Verified。
- 冻结清单：`benchmarks/swebench_verified_medium_v1_gold_valid_semantic_v1.json`。
- 样本：前五个 Gold preflight 可用任务，ordinal 为 1、2、4、7、8。
- 模型：`deepseek-chat`。
- workflow：`stage3`。
- Coordinator 并发上限：2。
- 命令超时：300 秒；每任务运行时上限：1800 秒；repair 上限：2。
- 每题先运行 Gold preflight，再从干净 base materialize，盲解后把非空补丁送入官方
  SWE-bench harness。

### 统一预算首轮

五题都使用总 token 800k、worker 上限 350k。结果为 2/5：Astropy、Pytest
resolved；Django、Pylint unresolved；Seaborn 因重叠冲突成为 empty patch。该轮直接
暴露了预算、冲突恢复和补丁卫生三类缺陷，原始摘要保存在：

`/Users/ayc/agent/repopilot-swebench-results/stage3-coordinator-first5-v2/summary.json`

### 修复后最新有效记录

| 任务 | 总/worker token 上限 | 波次 | 并发峰值 | 内部测试 | 官方结果 |
|---|---:|---:|---:|---:|---:|
| `astropy__astropy-7671` | 800k / 350k | 2 | 1 | failed | resolved |
| `django__django-11211` | 1.2m / 400k | 1 | 1 | passed | resolved |
| `mwaskom__seaborn-3069` | 1.4m / 320k | 3 | 2 | passed | resolved |
| `pylint-dev__pylint-4604` | 1.6m / 450k | 5 | 1 | passed | unresolved* |
| `pytest-dev__pytest-8399` | 800k / 350k | 2 | 1 | failed | resolved |

`*` Pylint 的最终补丁只包含 `pylint/checkers/variables.py`，卫生修复有效。官方 harness
在收集任务测试时因为 test patch 导入 base checkout 不存在的
`pylint.constants.IS_PYPY` 而中断，归类为
`ambiguous_failure/no_tests_collected`。按官方计分仍是 unresolved；这不是
infrastructure_failure，但由于断言没有开始执行，也不能据此确认或否定候选补丁语义。

## 6. 最终聚合指标

| 指标 | 结果 |
|---|---:|
| official resolved | 4/5 (80%) |
| 非空补丁 | 5/5 (100%) |
| Agent 内部配置测试通过 | 3/5 (60%) |
| 基础设施失败 | 0/5 |
| 总 token | 4,605,970 |
| 平均 token | 921,194 |
| 总工具调用 | 537 |
| 平均工具调用 | 107.4 |
| 总端到端时间 | 1,877.39 秒 |
| 平均端到端时间 | 375.48 秒 |

Astropy、Pytest 官方通过而内部失败，说明内部 Gate 仍有 false negative；Pylint 则
同时存在内部语义证据不足和官方测试收集歧义。`tests_passed` 只能用于诊断，最终结果
必须以官方 Oracle 为准。

历史 Stage 3 serial baseline 是 1/5，Stage 2 同五题历史记录是 4/5，本次最新有效
集合也是 4/5。但这三组的预算和代码版本不同，不能宣称“Coordinator 使准确率提升
60 个百分点”或“已经优于 Stage 2”；当前可以确认的是 Stage 3 的隔离、并发、合并、
冲突恢复和补丁卫生已经在真实任务中工作。

## 7. 产物

- 最终聚合：`/Users/ayc/agent/repopilot-swebench-results/stage3-coordinator-first5-final/summary.json`
- 逐题记录：`/Users/ayc/agent/repopilot-swebench-results/stage3-coordinator-first5-final/tasks.jsonl`
- 人类可读报告：`/Users/ayc/agent/repopilot-swebench-results/stage3-coordinator-first5-final/report.md`
- 指标 CSV：`/Users/ayc/agent/repopilot-swebench-results/stage3-coordinator-first5-final/metrics.csv`
- 记录来源与各题 orchestration 元数据：
  `/Users/ayc/agent/repopilot-swebench-results/stage3-coordinator-first5-final/provenance.json`

## 8. 仍未完成的边界

1. CLI 尚未提供 `repopilot resume RUN_ID`；底层 journal/checkpoint 能力尚未组成可重建
   provider、handler 和 solve invocation 的公开恢复入口。
2. 尚未接入 GitHub PR 发布/审批闭环。
3. 这五题不足以证明质量 Gate，需要固定同一代码、预算、模型参数和样本，做 Stage2
   与 Stage3 的正式 A/B；随后再扩展到 50–100 题。
4. 内部验证环境应能可靠执行任务级语义 probe，并将“环境/收集失败”与“断言失败”
   分开建模，减少 false negative 和 UNKNOWN。
