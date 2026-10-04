# RepoPilot Stage 3 前五项 SWE-bench 评测

## 1. 评测口径

- 日期：2026-10-01
- 数据集：`SWE-bench Verified`
- 冻结清单：`benchmarks/swebench_verified_medium_v1_gold_valid_semantic_v1.json`
- 样本：冻结清单中前 5 个 Gold preflight 可用任务（ordinal 1、2、4、7、8）
- 模型：`deepseek-chat`
- 每任务最大时间：1200 秒
- 命令超时：300 秒
- 最大 repair 次数：2
- 代理总 token 上限：200,000（当前默认值）
- 评分：使用 SWE-bench 官方 harness 和已准备的 linux/amd64 镜像

本次是 **Stage 3 serial baseline**：使用了 Stage 3 评测记录与报表层，但当前 `scripts/run_swebench_serial.py` 的端到端解题路径仍调用现有 `solve_issue` 组装根，尚未切换到 Stage 3 Coordinator 的多 worker 并行执行。因此本结果不能解读为“完整 Stage 3 并行架构的最终能力”。

## 2. 总体结果

| 指标 | 结果 |
|---|---:|
| Gold preflight | 5/5 |
| 官方 resolved | 1/5 (20%) |
| 非空补丁 | 2/5 (40%) |
| 非空补丁中 resolved | 1/2 (50%) |
| 空补丁 | 3/5 (60%) |
| 基础设施失败 | 0/5 |
| Agent 内部显示测试通过 | 3/5 (60%) |
| 非空补丁且 Agent 自测通过 | 0/5 (0%) |
| 总 token | 879,312 |
| 总工具调用 | 167 |
| 总端到端时间 | 875.98 秒 |
| 平均端到端时间 | 175.20 秒/项 |

`test_pass_rate=60%` 不能作为解题成功率：这 3 个“通过”全部对应空补丁，只能说原始 checkout 的配置命令通过。因此评测层新增了 `candidate_test_pass_rate`，本次为 0%。

## 3. 逐项结果

| 任务 | 官方结果 | 补丁 | 核心观察 |
|---|---|---|---|
| `astropy__astropy-7671` | unresolved | 非空 | 将 `LooseVersion` 替换为 `_compare_versions`，但没有定义或导入该 helper，导致 import 阶段 `NameError`。 |
| `django__django-11211` | empty patch | 报表为空 | repair 中编辑工具确实成功修改过 `fields.py`，但随后触发 token 上限；失败分支没有重新采集 diff，修改被遗失。 |
| `mwaskom__seaborn-3069` | empty patch | 空 | 21 次搜索、5 次读取，未执行编辑；探索占用了过多预算。 |
| `pylint-dev__pylint-4604` | empty patch | 空 | 44 次工具调用后仍未编辑；mutation checkpoint 后工具被限制，Agent 仍尝试读取/Bash，无法前进。 |
| `pytest-dev__pytest-8399` | resolved | 非空 | 将 xunit fixture 的生成名称改为下划线开头，官方 harness 判定通过。Agent 内部因 `_pytest._version` 环境导入错误判为失败，是内部 false negative。 |

## 4. 根因分析

### 4.1 token 预算与现有 workflow 不匹配

5 个任务的 worker 全部以 `limit_reached:max_tokens` 结束。当前总上限为 200k，而 explorer 可使用 120k；analyzer、explorer 和 planner 同时共享全局计数器，导致 coder/repair 开始时剩余预算过少。

工具轨迹也证明了这一点：Astropy/Django/Seaborn/Pylint/Pytest 分别执行了 23/26/29/36/26 次搜索或读取类操作，但五项合计只有 4 次编辑工具调用。当前的主要瓶颈是“过度探索，太晚修改”。

### 4.2 repair 失败后的补丁丢失

`solve_issue.py` 在 repair worker 失败时直接 `break`，而 `capture_workspace()` 位于成功分支之后。Django 轨迹中有成功的 `file_changed` 事件（字符数从 26,689 变为 26,684），但报告仍使用 repair 之前的空 diff，之后工作树被清理。这是可复现的编排缺陷，不是 SWE-bench 基础设施错误。

### 4.3 Agent 内部验证与官方 Oracle 不一致

Pytest 任务的官方结果为 resolved，但 Agent 自测因已知环境导入问题失败。这说明不能用 Agent 内部 `tests_passed` 代替 SWE-bench 官方结果；同时，baseline-aware 逻辑需要区分“环境启动错误”和“补丁回归”。

### 4.4 Astropy 缺少最低成本的静态健全性检查

Astropy 补丁引用未定义符号，一次简单 import/编译检查就能拦截。现有证据流程成本较高，却没有确保“修改文件可导入、新符号已定义”这类基本不变式。

## 5. 与上一轮 Stage 2 的对照

历史运行 `stage2-medium-v2-semantic-evidence` 在同五项上为 4/5 resolved（80%），本次为 1/5（20%）。但两者不是等预算对照：

| 运行 | resolved | 总 token | 工具调用 | 端到端时间 |
|---|---:|---:|---:|---:|
| Stage 3 serial baseline | 1/5 | 879,312 | 167 | 875.98s |
| Stage 2 historical | 4/5 | 2,280,849 | 342 | 1,354.77s |

历史运行每项输入 token 约 436k–450k，显著高于本次整个 workflow 的 200k 上限。因此 20% 与 80% 的差异不能归因于 Stage 3 架构本身；当前首先证明的是默认预算配置发生了影响成功率的回归。

## 6. 优先级建议

1. **P0：保存所有失败/限额分支中的最新 diff。** 在 worker 返回后、判断 success 前先执行 `capture_workspace()`，并增加“repair 修改后 limit_reached 仍产出 partial patch”的集成测试。
2. **P0：重新分配 token 预算。** 给 coder 和 repair 设置硬保留额，将 explorer 从 120k 下调为总预算的有界比例，并在已定位到候选文件后提前结束探索。
3. **P0：将 Stage 3 Coordinator 接入 SWE-bench 端到端入口。** 否则目前只能验证 Stage 3 基础设施和报表，不能验证多 worker 协作效果。
4. **P1：增加快速健全性门禁。** 对 Python 补丁先运行 compile/import 或目标测试，避免 Astropy 这类未定义符号进入官方评测。
5. **P1：修正工具限制后的 prompt。** mutation checkpoint 触发后，明确告知模型只能执行哪些工具，避免重复提交必然被拒绝的 Bash/read 调用。
6. **P1：进行等预算 A/B 复测。** 固定同一 manifest、模型、镜像、命令和 token 上限，再比较 Stage 2 serial 与完整 Stage 3 coordinator；否则无法得出架构增益结论。

## 7. 产物

- 原始运行摘要：`/Users/ayc/agent/repopilot-swebench-results/stage3-first5-v1/summary.json`
- Stage 3 指标摘要：`/Users/ayc/agent/repopilot-swebench-results/stage3-first5-v1/stage3-evaluation/summary.json`
- Stage 3 Markdown 报告：`/Users/ayc/agent/repopilot-swebench-results/stage3-first5-v1/stage3-evaluation/report.md`
- 逐项结果：`/Users/ayc/agent/repopilot-swebench-results/stage3-first5-v1/stage3-evaluation/tasks.jsonl`
- 指标 CSV：`/Users/ayc/agent/repopilot-swebench-results/stage3-first5-v1/stage3-evaluation/metrics.csv`

## 8. 代码回归检查

Stage 3 评测层新增了非空补丁和有效候选自测指标，全部工程测试结果为：`133 passed`。
