# RepoPilot Stage 3 剩余十一项 V4 Flash 评测与问题分析

日期：2026-10-04

## 1. 结论

本轮使用同一份 Stage 3 Coordinator 代码、同一组预算和同一个模型族，完成冻结清单中
剩余 11 个 Gold-valid、镜像可用的 SWE-bench Verified 任务。正式结果为：

- **6/11 official resolved（54.55%）**；
- 4 unresolved、1 empty patch；
- 11/11 Gold preflight 通过，0 个官方 infrastructure failure；
- 10/11 产出非空补丁并进入官方 Oracle；
- 硅基流动发生 24 次瞬时重试，但没有出现明确的余额耗尽，因此 **0 次切换到 DeepSeek
  官方 API**。

这批结果说明当前 Stage 3 已能解决真实任务，但质量仍没有稳定到可作为发布 Gate：内部
状态与官方 Oracle 只有 5/11 一致，Explorer 和 Planner 各有 8/11 进入降级/恢复路径，
5 个任务出现“只读定位任务被强制要求产出补丁”的任务类型冲突。大预算没有消除这些
结构性问题，反而把单题平均 token 推高到约 100.6 万。

## 2. 模型与 Provider 配置

主 Provider 使用硅基流动，模型 ID 固定为
`deepseek-ai/DeepSeek-V4-Flash`。该 ID 与硅基流动官方模型中心列出的 V4 Flash ID 一致：

- https://www.siliconflow.cn/models
- https://docs.siliconflow.cn/docs/api/chat-completions-post

备用 Provider 使用 DeepSeek 官方 API，模型 ID 固定为 `deepseek-v4-flash`。官方变更日志
说明旧 V4 Flash 入口可能路由到更新的 Flash 版本，因此若将来触发回退，API 名称仍是
用户指定的 V4 Flash，但不能把它解释成两个 Provider 背后的精确权重完全相同：

- https://api-docs.deepseek.com/updates/

回退策略不是“429 就切换”：

1. 429、503、504、`system is too busy` 和 socket timeout 只重试硅基流动；
2. 只有响应明确包含余额/credit/quota 已耗尽语义时，才永久切换到官方 API；
3. 本轮 24 次瞬时错误全部在硅基流动恢复，没有触发官方回退；
4. API key 只存在于权限为 `0600` 的临时环境文件中，不写入仓库、日志或本文。

正式运行前的真实 smoke test 返回的模型标识为
`deepseek-ai/DeepSeek-V4-Flash`，且 `fallback_active=false`。

## 3. 冻结评测协议

- 清单：`benchmarks/swebench_verified_medium_v1_gold_valid_semantic_v1.json`；
- 范围：清单第 7–16 个条目，对应原冻结 ordinal 9、10、11、12、13、14、15、16、
  18、19、20；
- workflow：`stage3`；
- Coordinator 最大并发：2；
- 总 token 上限：1,800,000/题；
- worker 上限：500,000；Explorer 上限：150,000；repair 保留：450,000；
- 最大迭代：70；最大工具调用：300；最大 repair：2；
- Agent solve 墙钟上限：2,400 秒；命令超时：300 秒；
- 每题均执行 Gold preflight，从干净 base commit 建工作区，Agent 不读取 gold/test patch，
  非空候选补丁最终交给官方 SWE-bench harness 评分。

结果根目录：

`/Users/ayc/agent/repopilot-swebench-results/stage3-coordinator-remaining11-v4flash-v1`

工作区与完整 JSONL 事件：

`/Users/ayc/agent/swebench-formal-workspaces/stage3-coordinator-remaining11-v4flash-v1`

## 4. 逐项结果

| ordinal | 任务 | 内部状态 | 官方结果 | 端到端时间 | input / output tokens | 主要诊断 |
|---:|---|---|---|---:|---:|---|
| 9 | `scikit-learn__scikit-learn-14053` | resolved | **resolved** | 19.0 分 | 837,067 / 36,583 | Explorer 降级；“Locate”任务泄漏 6 个 probe，卫生修复后得到正确补丁 |
| 10 | `sphinx-doc__sphinx-9602` | failed | **empty patch** | 40.9 分 | 442,411 / 55,739 | Explorer schema 失败；只读 Trace 任务被要求改代码，后续实现被阻塞，最终耗尽运行时 |
| 11 | `sympy__sympy-23824` | failed | **resolved** | 19.6 分 | 1,031,111 / 46,749 | 一文件补丁正确；内部因候选语义证据身份/时序问题误判失败 |
| 12 | `astropy__astropy-13236` | resolved | **unresolved** | 27.3 分 | 1,008,140 / 35,010 | 修改 `setup.cfg` 全局忽略自己新增的 warning；卫生检查未识别测试策略弱化 |
| 13 | `django__django-11333` | failed | **resolved** | 42.5 分 | 594,216 / 29,277 | 产品修复正确，但最终补丁泄漏 `tmp_probe.py`；内部达到运行时上限 |
| 14 | `matplotlib__matplotlib-23412` | failed | **resolved** | 18.8 分 | 1,576,064 / 66,025 | 产品补丁正确；linked worktree 缺 `_version` 生成物导致内部导入失败和错误 repair |
| 15 | `mwaskom__seaborn-3187` | resolved | **unresolved** | 38.6 分 | 978,797 / 41,835 | 定位任务失败后真正实现任务被阻塞；repair 改错抽象层，公开测试仍通过 |
| 16 | `psf__requests-6028` | failed | **unresolved** | 44.0 分 | 304,485 / 44,451 | Explorer 读取不存在的 `AGENTS.md` 后整段降级；修复偏离 stdlib URL 语义 |
| 18 | `pylint-dev__pylint-6386` | failed | **unresolved** | 13.3 分 | 1,599,660 / 56,805 | “Locate verbose”任务阻塞真实实现；repair 后探针仍证明 `-v` 行为错误 |
| 19 | `pytest-dev__pytest-7324` | failed | **resolved** | 25.6 分 | 1,580,256 / 82,853 | 修复语义正确；内部缺 `_pytest._version`，重复 repair，最终泄漏 `evidence.md` |
| 20 | `scikit-learn__scikit-learn-10908` | resolved | **resolved** | 8.3 分 | 595,734 / 24,593 | Explorer/Planner 正常，一项实现任务给出正确两行补丁；证据协议仍使 worker 用满 500k |

正式分数只按右侧官方结果统计。Django 和 Pytest 即使带无关文件仍被官方判定 resolved，
这说明 SWE-bench 正确性分数不能替代补丁可维护性和卫生 Gate。

## 5. 聚合指标

| 指标 | 结果 |
|---|---:|
| Gold-valid / Agent attempted | 11 / 11 |
| official resolved | 6 / 11（54.55%） |
| official unresolved | 4 / 11（36.36%） |
| empty patch | 1 / 11（9.09%） |
| 非空补丁 | 10 / 11（90.91%） |
| 官方 infrastructure failure | 0 |
| Agent 内部 resolved / failed | 4 / 7 |
| input tokens | 10,547,941 |
| output tokens | 519,920 |
| input + output | 11,067,861 |
| 平均 input + output / 题 | 1,006,169 |
| 工具调用 | 1,223（平均 111.2/题） |
| Agent solve 总时间 | 15,480.79 秒（4 小时 18 分） |
| 端到端总时间 | 17,872.16 秒（4 小时 57 分 52 秒） |
| 配置测试执行 | 25 次 |
| worker 启动 / wave | 26 / 26 |
| 补丁卫生拒绝 | 5 次 |
| 硅基流动瞬时重试 | 24 次，分布在 4 个任务 |
| 官方 API 回退 | 0 次 |

内部状态与官方 Oracle 的混淆矩阵：

| | 官方成功 | 官方未成功 |
|---|---:|---:|
| 内部 resolved | 2 | 2 |
| 内部 failed | 4 | 3 |

内部判断仅有 5/11 与官方结果一致。4 个 false negative 是 SymPy、Django、Matplotlib、
Pytest；2 个 false positive 是 Astropy、Seaborn。当前内部 `resolved` 不能当成官方成功，
内部 `failed` 也不能当成补丁必错。

## 6. 测试暴露的设计问题

### P0：TaskGraph 没有任务类型，定位任务与实现任务使用同一完成条件

至少 5 个任务生成了 `Locate...`、`Trace...` 一类只读步骤，但 worker 的成功条件仍是
“必须产生产品补丁”。定位 worker 因 `no_patch`、token/runtime 或探针卫生失败后，其
依赖的真正实现步骤直接变成 blocked。Sphinx 因此成为 empty patch，Seaborn、Pylint
因此错过正确实现，Pytest 和 scikit-learn-14053 只能依赖昂贵 repair 兜底。

应在 Task schema 中加入 `investigation`、`implementation`、`validation` 类型：

- investigation 以结构化 repository evidence 为产物，不要求 diff；
- implementation 才要求生产代码 mutation；
- validation 只允许测试/探针，不计为产品 mutation；
- 依赖释放按任务类型的产物契约判断，而不是统一按“是否有 patch”。

### P0：V4 Flash 结构化输出适配不稳定，错误字段会丢弃整份探索结果

11 项中 8 项 Explorer 降级：5 次缺少 `language`，其余分别是 observation kind 不支持、
模型引用不存在的 `AGENTS.md`、把目录当文件。Planner 也有 8/11 因未知 evidence 引用或
行为覆盖不全进入 recovered path。

这不是“模型完全没有理解仓库”：多数响应包含大量有效观察，只因一个字段不合契约就
整份作废。应做字段级容错、路径存在性过滤、语言确定性推断和一次小型 schema repair，
并让 Planner 只丢弃无效引用，而不是重建整份计划。

### P0：内部执行工作区不等价于官方测试工作区

Matplotlib 和 Pytest 的 linked worktree 缺少镜像/base workspace 中的 ignored/generated
`_version` 模块，内部测试在 import/collection 阶段失败；官方 Oracle 应用同一产品补丁
却 resolved。当前 `workspace_runtime_artifacts_hydrated` 事件存在，但水合范围不足。

应对 base workspace 与 linked worktree 做内容清单差分，白名单复制构建生成物、扩展
模块和必要 ignored artifacts；测试结果要区分 `environment_error`、`collection_error`
和 `assertion_failure`。前两者不能自动触发产品代码 repair。

### P0：补丁卫生依赖文件名正则，存在明显漏网与策略绕过

卫生 Gate 在 5 个任务中正确拒绝了 `probe_*.py`、`_probe_*.py`、`probe_tmp.py`，但最终
仍漏过 Django 的 `tmp_probe.py` 和 Pytest 的 `evidence.md`。Astropy 修改 `setup.cfg`
忽略自己新增的 warning，也未被识别为测试/质量策略弱化。

应把 probe 放到仓库外的临时目录；对所有新文件记录创建工具、任务类型和用途，而不是
仅匹配名称；默认拒绝根目录未知新文件；对 pytest/warnings/lint/coverage 配置变更执行
语义策略审查。

### P1：证据协议让模型重复“搬运证据”，且 revision 身份仍不稳定

worker 日志记录了 49 次 `missing_probe_provenance`。SymPy 在 repair 中已记录候选语义
证据，但最终 evaluator 仍使用旧的缺证据结论；scikit-learn-10908 的两行正确补丁也因
6 项 completion check 反复 deferred，最终 worker 用满 500k。

`record_evidence` 不应要求模型复述工具 provenance。Runtime 应自动把命令、返回码、
workspace revision 和受测候选绑定成不可变记录；合并后只做 revision 重映射和去重。

### P1：预算扩大没有带来相称收益，停止条件过晚

本轮共 16 个 worker 达到 `max_tokens`，3 个达到 `max_runtime_seconds`。输入 token 超过
一千万；Pylint 单题接近 160 万输入仍未解决，而最后的 scikit-learn 两行修复也消耗约
62 万 input+output。上下文反复携带大段历史、多个 reviewer 重复运行、测试在相同
revision 上重复执行，是主要放大器。

建议按 phase 设置硬预算和 latency SLO；缓存 `(commit, command, environment)` 测试；
正确补丁通过定向语义 probe 后进入一次最终 review，不再因非阻塞证据格式问题启动新
repair；把 cached input、provider wait 和 evaluator token 分开计量。

### P1：Permission/工具错误不能指导模型恢复

日志中有 106 次 `approval_denied`、25 次 `deny`、24 次 `command_failed`。V4 Flash 会
连续重试 bash，而错误只说 executable 不在 allowlist，没有告诉它应改用 `run_tests`、
单个允许命令或内置 probe 工具。

权限拒绝应返回 machine-readable `allowed_alternatives` 和可直接调用的等价工具；同一
规范化命令连续被拒两次后，Runtime 应阻止再次调用并注入恢复提示。

### P1：Reviewer/Evaluator 把环境约束和通用 PR 规范当成任务正确性

Pytest 的 reviewer 在生成文件缺失时仍给出“配置测试失败”和“生产代码未伴随测试”两项
阻塞意见，触发第二轮 repair；Requests 还要求不可用的精确 Python 3.8.12 运行证据。
这类要求不应覆盖 SWE-bench 的外部 hidden-test 协议和当前容器能力。

Reviewer 应接收环境诊断、baseline 和任务模式；SWE-bench 模式下，未修改公开测试不应
自动构成 P2，无法提供的运行时版本应标为 `environment_blocked`，并允许源码级证据。

### P2：Provider 可观测性仍停留在 stderr

24 次瞬时重试只存在于 `solve.log`，`result.json`/`summary.json` 没有 provider、精确模型、
请求次数、超时、等待时间、fallback 原因等字段。当前可证明没有回退，但无法准确量化
重试消耗了多少墙钟与 token。

应将 provider attempt 写入正式事件流，并让每个结果携带
`primary_model`、`active_provider`、`fallback_activated`、`retry_count`、`provider_wait_ms`。
重试延迟与单次请求 timeout 还应受工作流剩余 deadline 截断。

## 7. 建议的修复顺序与重新验收标准

第一批（必须先做）：

1. Task kind + 不同完成契约；
2. 工作区生成物水合与环境错误分类；
3. provenance 驱动的补丁卫生；
4. Explorer/Planner 字段级结构化恢复。

第二批：

1. Runtime 自动证据绑定，删除模型手工搬运 provenance；
2. revision 级测试缓存与 phase 预算；
3. 权限错误的可恢复替代建议；
4. SWE-bench-aware Reviewer/Evaluator。

第三批：

1. provider telemetry 进入 JSONL/summary；
2. 可恢复的 run checkpoint/resume；
3. 在同一冻结代码、同一 V4 Flash 配置上重新跑这 11 项，不做逐题回填。

下一轮建议的准入指标：Explorer 降级不高于 1/11；只读任务 `no_patch` 失败为 0；无临时
文件/质量配置绕过；内部/官方一致率至少 9/11；总 token 至少下降 40%；official resolved
不得低于本轮 6/11。

## 8. RepoPilot 自身验证

- `uv run pytest`：**145 passed**；
- provider/fallback 相关文件定向 `ruff check`：通过；
- `python -m compileall -q repopilot`：通过；
- `git diff --check`：通过；
- 全仓 `ruff check` 仍报告 15 项既有清理项，主要是未使用 import 和脚本为调整
  `sys.path` 产生的 E402；本轮没有为掩盖该结果做无关批量改写。

本轮新增 provider 单元测试覆盖：明确余额耗尽后永久回退；普通 429 不回退；瞬时 429
与 timeout 仅重试主 Provider。真实批次没有触发余额耗尽分支，因此“官方 API 回退逻辑
可执行”由单元测试验证，“本次实际使用官方 API”则明确为否。

