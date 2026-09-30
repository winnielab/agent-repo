# RepoPilot 项目理解与失败 PR 回归报告（面试版）

日期：2026-09-30。

## 一、这轮工作的结论

本轮从暂停检查点恢复后，完成了两件事：一是把 Coding Agent 的“项目理解”从宽泛仓库扫描改造成可审计的任务相关证据链；二是使用路径已验证的冻结版本，对三个最有代表性的历史失败任务做真实 DeepSeek + 官方 SWE-bench 回归。

工程可靠性目标已经达到：冻结入口真实指向当前源码，产品测试 116/116 通过，三项 Gold 全部通过，三次 Agent 都生成了非空且只包含生产文件的 patch，没有环境失败或遗留容器。正确性目标尚未达到：三项官方结果仍为 unresolved。这个结果没有被包装成“测试完成即成功”，而是进一步定位出 temporal intent、semantic counterexample coverage 和 fallback planning 三个设计缺口。

## 二、从 Coding Agent 设计中借鉴了什么

### 1. 渐进式项目理解

Agent 不再把完整仓库地图一次性塞进上下文，而是根据任务建立最小证据切片：

```text
项目/目录指令
  → 目标定义或状态写入点
  → 一个 caller、consumer 或失败路径
  → 邻近测试契约
  → 实现计划
```

Explorer 输出的每条 observation 都包含稳定 key、kind、路径、行号、符号和说明，并且必须对应实际读取或搜索过的文件。Planner 的实现步骤必须引用这些 observation key，从领域模型上阻止“没有读代码就规划”。

这与成熟 Coding Agent 的共同经验一致：根目录只保存路由信息，真正的项目知识按任务逐层加载；在多步骤工作中保持紧凑工作记忆，而不是保留无限增长的原始对话。

### 2. 独立理解预算

上一轮 Seaborn 任务表明 Explorer 可以消耗 20 万以上 tokens，导致 Coder 没有实施预算。当前设计为 Explorer 单独设置：

- `max_explorer_iterations=12`
- `max_explorer_tool_calls=40`
- `max_explorer_tokens=120000`

达到只读上限后，Runtime 撤回工具并要求基于已有证据合成结论。编码、评估和修复仍保留自己的预算。

### 3. 行为矩阵和状态副作用

BehaviorCase 不只保存“输入—输出”，还区分 positive、negative、boundary、temporal、preservation、failure-path，并可声明成功或失败路径必须发生的 state effect。这用于覆盖此前出现的四类错误：

- 把展示层问题误当成运行时行为；
- 只处理正向案例，遗漏否定/边界分支；
- 把未来阶段行为提前到当前版本；
- 只看返回值，遗漏失败路径仍需产生的状态副作用。

### 4. 根因假设必须可证伪

行为型 Plan 现在必须保存：

- root-cause claim；
- 能够推翻该 claim 的源码追踪或只读 probe；
- 支持该检查的 repository evidence key。

Coder 在编辑前执行检查，Bash 输出被保存为 command evidence；Evaluator 把 hypothesis 当成独立目标，缺乏证据时返回 unknown，被命令或源码反驳时返回 fail。这样 Repair 接收到的是“哪条假设被推翻”，而不是泛化的“再试一次”。

### 5. 工具边界和补丁卫生

未明确请求测试工作的任务，`write_file`、`edit_file`、`delete_file` 在执行前拒绝测试路径；最终 Evaluator 再做一次 patch hygiene，阻止测试改动、临时 probe、debug 和 run 文件进入交付补丁。Reviewer 复用 workflow 已有的权威测试结果和顶层 timeout，避免重复执行固定 180 秒测试。

## 三、冻结版本的科学性修复

最初的 v5 是无效冻结：复制 v4 的 `.venv` 后，CLI 脚本的绝对 shebang 仍指向 v4，因此目录名称变化了，实际代码入口没有变化。本轮没有掩盖该错误，而是中断评测并排除全部 v5 结果。

有效 v6 使用空目录复制源码，排除 `.git`、`.venv`、缓存、`.env*` 和 `.DS_Store`，再执行 `uv sync` 创建全新环境。冻结前同时验证：

- `repopilot.__file__` 位于 v6 目录；
- `.venv/bin/repopilot` shebang 指向 v6 Python；
- CLI smoke 通过；
- 116 项产品测试通过；
- compileall 和 diff check 通过。

冻结身份：

- 目录：`/Users/ayc/agent/repopilot-agent-freezes/repopilot-comprehension-v6-20260930`
- 归档：`/Users/ayc/agent/repopilot-agent-freezes/repopilot-comprehension-v6-20260930-source.tar.gz`
- 归档 SHA-256：`07bcb79031feeb7f965589d80bd4cb54d7b6c4f14665adb5c73d73cdcea6b264`
- 源文件聚合 SHA-256：`0140f4e2e20ece0828d6470283b305ab1d3dab1ece2b1eff8510817bbe8909d5`

## 四、真实回归结果

选取旧失败集中的 #12、#15、#16，固定 DeepSeek、650k token、单 worker、amd64 prepared image、逐题 Gold 和官方 Oracle。

| 序号 | 实例 | 内部状态 | 官方状态 | 结果解释 |
|---|---|---|---|---|
| 12 | `astropy__astropy-13236` | failed | unresolved | 工具边界修复生效，但时间阶段仍选错 |
| 15 | `mwaskom__seaborn-3187` | resolved | unresolved | probe 被执行，但没有检验标签数值语义 |
| 16 | `psf__requests-6028` | failed | unresolved | Evaluator 避免 false pass，但 Planner fallback 丢失具体根因 |

批次总计：Gold 3/3、非空补丁 3/3、官方 resolved 0/3、环境失败 0、input tokens 1,471,450、output tokens 37,392。

### #12：工具守卫通过，temporal intent 仍失败

v4 的测试文件修改只在最终 hygiene 阶段被发现。v6 中模型三次尝试编辑 `astropy/table/tests/test_mixin.py`，工具都返回 `test_change_not_allowed`，且日志中没有该测试文件的 `file_changed`。最终 patch 只剩 `astropy/table/table.py`。

但 Analyzer 把两阶段提案理解为“当前添加 FutureWarning，未来删除转换”。公开测试把 FutureWarning 当作失败，而本任务对应的目标版本实际需要执行后一个阶段。Agent 仍试图通过改测试证明前一阶段，工具守卫正确阻止了越权，但无法替它选择正确版本阶段。

下一步不是允许改测试，而是给 temporal behavior 增加“当前 checkout 处于哪个阶段”的证据字段：版本文件、changelog、目标分支或现有测试共同决定 active phase；计划不得同时覆盖互斥阶段。

### #15：可证伪机制执行了，但检查对象不够强

Agent 实际运行了 focused probe，并证明：`ScalarFormatter.get_offset()` 对大范围数据返回 `1e6`，legend consumer 直接使用 `_legend` 的 labels。这两条 claim 都是真的，但不足以证明“把 `1e6` 字符串追加到每个 label”具有正确数值语义。

正确反例应该比较 formatter 在 useOffset/scientific 开关不同组合下的最终标签，并验证每个 legend label 单独表达真实数值。当前检查只验证 dependency fact，没有验证 candidate transformation。因此内部 Evaluator 通过而官方失败。

下一步应把 hypothesis check 分成两层：

1. dependency fact：外部 API 返回什么；
2. candidate semantic property：候选转换对代表性正向、边界和 preservation 输入是否仍表达相同值。

只有第二层通过，根因假设才能支持实现。

### #16：避免 false pass，但 fallback 计划仍过弱

Planner 的结构化输出没有覆盖全部 behavior key，因此验证器正确拒绝并进入通用 fallback。问题是 fallback 只有抽象的“追踪最早不变量”，没有 Explorer 的具体 URL authority/auth evidence，Coder 最终修改 `get_auth_from_url` 的 None 归一化，而正确路径位于 scheme prepend 时恢复 auth 到 netloc。

Evaluator 没有因为 visible tests 通过而放行，它明确返回 unknown：没有 Python 3.8.12 认证代理的端到端证据，也没有证明修改点是最早偏离位置。这修复了内部 false positive，但两轮 Repair 后接近 token 上限，无法形成正确补丁。

下一步应把 Planner 的“部分有效输出”修复为 schema recovery，而不是整份降级：保留已验证的 root cause、evidence 和 steps，只为遗漏 behavior key 生成补充映射；fallback 也必须携带 observation，而不能退化为空 evidence。

## 五、如何在面试中介绍

> 我没有针对三个 SWE-bench 答案写特例，而是把失败抽象成项目理解、假设验证和工具边界问题。Explorer 按定义、caller/state 和邻近测试建立可引用证据；Planner 必须说明最早不变量和可推翻它的检查；Evaluator 把未验证假设判为 unknown，不再因为公开测试通过就放行。真实回归证明测试文件保护已经从事后检查升级到执行前阻断，Requests 也不再产生内部 false positive。同时官方三题仍未解决，进一步说明“运行了 probe”不等于“probe 覆盖了候选语义”，以及整份 Planner fallback 会丢失有效仓库知识。这个结果让我能把下一步准确收敛到 active temporal phase、candidate semantic property 和 partial-plan recovery，而不是继续增加 token 或针对隐藏测试调 prompt。

## 六、科学边界与产物

- 这是对旧失败项的定向回归，不是新的盲测分数。
- v4 九题结果仍是有效历史；v5 全部无效并排除；v6 三题独立记录。
- 内部状态是诊断信号，官方 SWE-bench 才是任务正确性判定。
- DeepSeek API key 仅从外部 env 文件加载，未写入冻结、报告或日志摘要。
- 批次结束后无运行中的 Docker 容器。

机器可读报告：`reports/stage2/swebench_comprehension_v6_targeted_20260930.json`。
原始结果：`/Users/ayc/agent/repopilot-swebench-results/stage2-medium-v1-comprehension-v6-targeted`。
