# RepoPilot 当前进度检查点（2026-09-30）

状态：已恢复并完成 v6 定向回归。后续继续开发前先阅读本文件和
`docs/RepoPilot_项目理解与失败PR回归_20260930.md`。

## 1. 当前工作区

- 工作区：`/Users/ayc/.codex/worktrees/fbfa/agent-rep`
- 当前修改尚未提交，保留在工作区中。
- `git diff --check`：通过。
- 最新完整产品测试：`116 passed in 3.24s`。
- `python -m compileall`：通过。
- 当前没有运行中的 Docker 容器，也没有仍在运行的串行 benchmark 进程。
- 当前差异规模：33 个 tracked 文件，814 insertions、54 deletions。

## 2. 已完成的通用 Agent 改进

1. 任务自适应仓库理解：Explorer 按“项目指令 → 定义 → caller/state → 邻近测试”建立有限证据切片，并保存可验证 observation。
2. Explorer 独立预算：增加只读阶段的 iteration、tool-call 和 token 上限，防止项目理解耗尽编码与修复预算。
3. 证据化规划：PlanStep 必须引用 Explorer observation；Plan 保存 impact surface。
4. 行为契约：支持 positive、negative、boundary、temporal、preservation、failure-path 和 state effect。
5. 假设可证伪：行为型 Plan 必须给出 root-cause claim 和能够推翻它的 source trace/read-only probe；Coder 在编辑前执行，Evaluator 将其作为独立目标检查。
6. Patch hygiene：阻止临时调试/复现文件和未请求的测试变更；最新产品代码还在编辑工具入口加入测试路径写入保护。
7. Reviewer 复用权威测试结果及顶层 timeout，避免固定 180 秒重复测试。
8. Structured JSON parser 接受首个完整 JSON 对象及 provider 尾部文本，语义 schema 仍由调用方验证。

## 3. 有效的 v4 旧失败项回归

- 有效冻结目录：`/Users/ayc/agent/repopilot-agent-freezes/repopilot-comprehension-v4-20260930`
- 冻结产品测试：113 passed。
- 冻结归档 SHA-256：`91e99502ca4bba0e281e4a4db15563ecefdb405fc9fc924d4caf9c9e16648ab5`
- 结果目录：`/Users/ayc/agent/repopilot-swebench-results/stage2-medium-v1-comprehension-v4-failures`
- 数据集：原 20 项中先前 official unresolved 且环境有效的 9 项。
- Gold：9/9 通过；Agent：9/9 产生非空 patch 并进入官方评测。
- 官方结果：2 resolved、7 unresolved，Resolve Rate 22.22%。
- 新增 resolved：
  - #19 `pytest-dev__pytest-7324`
  - #20 `scikit-learn__scikit-learn-10908`
- v4 输入 tokens：3,335,107；输出 tokens：94,020。

v4 科学边界：这是旧失败集的受控回归，不是新的盲测分数。v4 不含随后增加的“测试文件写入工具边界保护”和“假设可证伪”机制。

## 4. v5 运行无效，禁止作为结果引用

原计划的冻结目录：`/Users/ayc/agent/repopilot-agent-freezes/repopilot-comprehension-v5-20260930`。

发现问题：该目录通过复制 v4 的 `.venv` 建立。虚拟环境入口脚本包含绝对 shebang，`v5/.venv/bin/repopilot` 实际仍启动 v4 Python/可编辑安装，因此名义 v5 的 Astropy 运行实际执行了 v4 代码。表现为最新编辑工具本应拒绝测试文件修改，但事件日志仍显示修改成功。

处理结果：

- 已中断该批次。
- 已显式删除中断后遗留的 Astropy evaluator 容器。
- 该批次未完成，不能用于 v5 结论，也不能与 v4 合并统计。
- 未继续创建 v6；一次命令因目标工作目录尚不存在而在启动前失败，没有产生 v6 目录或新进程。

## 5. 恢复时的正确顺序

1. 先确认用户要求继续。
2. 检查当前工作区 `git status`，不要覆盖现有修改。
3. 用全新空目录复制源码，明确排除 `.venv`、`.git`、缓存和 `.env*`。
4. 在新冻结目录内运行 `uv sync` 创建独立虚拟环境，不要复制旧 `.venv`。
5. 做入口自检：运行冻结目录的 Python，打印 `repopilot.__file__`，必须指向该冻结目录；检查 `head -1 .venv/bin/repopilot` 的 shebang 也必须指向新目录。
6. 在冻结目录运行完整 `pytest -q`，预期至少 116 passed。
7. 重新计算内容哈希后再命名为新的有效冻结版本。
8. 优先回归 #12、#15、#16；使用新的 output/workspace/run-prefix，绝不复用无效 v5 结果目录。
9. 测试完成后再写最终开发报告，并明确区分 v4 有效批次与后续冻结批次。

## 6. 恢复后的首个待验证问题

最新产品代码中的编辑工具测试路径保护已由“入口路径已验证”的 v6 冻结版本完成真实 DeepSeek/SWE-bench 验证：#12 对 `astropy/table/tests/test_mixin.py` 的三次 `edit_file` 调用均返回 `test_change_not_allowed`，事件日志没有该文件的 `file_changed`。

## 7. 恢复完成记录

- 有效冻结：`/Users/ayc/agent/repopilot-agent-freezes/repopilot-comprehension-v6-20260930`
- 入口路径与 shebang 均确认指向 v6；116 项产品测试通过。
- #12、#15、#16 的 Gold 均通过，三项均产生非空、仅生产文件 patch 并完成官方评测。
- 官方结果：0 resolved、3 unresolved；没有基础设施失败或遗留容器。
- #12 三次测试文件编辑均被 `test_change_not_allowed` 阻止，工具边界保护完成真实验证。
- 详细原因、措施、数据和后续设计见 `docs/RepoPilot_项目理解与失败PR回归_20260930.md`。
