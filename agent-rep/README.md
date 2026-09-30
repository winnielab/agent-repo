# PR Review Agent（模块化版本）

这个目录是 `agents/review_agent.py` 的模块化重组版本。原文件保持不变，便于继续对照、回归和渐进迁移；这里的运行时代码不导入原文件，可以独立装配和执行。

完整组件职责、依赖关系和端到端流程见：[架构与运行流程](docs/架构与运行流程.md)。

## 目录结构

```text
agent-rep/
├── pr_review_agent/
│   ├── settings.py       # 常量、模型供应商、工具 schema
│   ├── utils.py          # Git、路径、diff 和通用辅助函数
│   ├── domain.py         # Finding、Transcript、消息与生命周期
│   ├── skills.py         # Skill 加载
│   ├── knowledge.py      # 公司知识库与 RAG
│   ├── tools.py          # Reviewer 可调用的只读工具
│   ├── reviewers.py      # 专项 Reviewer
│   ├── llm.py            # LLM 抽象与 OpenAI-compatible 实现
│   ├── orchestration.py  # Agentic / Council / Debate 编排
│   ├── reporting.py      # 标准化报告生成
│   ├── judge.py          # AI Judge
│   ├── agent.py          # 顶层 ReviewAgent 组装根
│   ├── cli.py            # 命令行参数与入口
│   ├── __main__.py       # python -m pr_review_agent
│   └── resources/skills/ # 随 Python 包分发的内置 Skill
├── tests/                # 模块边界与装配测试
├── scripts/
│   └── sync_from_legacy.py  # 从旧文件同步完整定义的迁移工具
├── review_agent.py       # 兼容旧式导入/执行的薄入口
└── pyproject.toml
```

依赖方向保持单向：`settings/utils → domain → knowledge/tools/reviewers/llm → orchestration/reporting → agent → cli`。顶层 `ReviewAgent` 仍然是 composition root，各模式的行为、CLI 参数和输出格式均沿用原实现。

## 运行

在本目录直接运行，无需安装：

```bash
cd agent-rep
python -m pr_review_agent --repo .. --base main --target HEAD --llm-provider none
```

兼容原入口：

```bash
python review_agent.py --repo .. --base main --target HEAD --llm-provider none
```

也可以安装为命令：

```bash
python -m pip install -e .
pr-review-agent --help
```

## 后续开发建议

- 新增领域对象放在 `domain.py`，避免让数据模型依赖具体 LLM 或工具实现。
- 新增只读能力放在 `tools.py`，同时在 `settings.py` 的 `TOOLS` 中声明 schema。
- 新增 reviewer 放在 `reviewers.py`；跨 reviewer 的流程放在 `orchestration.py`。
- 外部模型供应商适配只进入 `llm.py`，业务编排依赖 `LLMClient` 接口。
- 输出 schema 和渲染逻辑集中在 `reporting.py`，Judge 保持独立。

若旧文件仍有功能更新，可从仓库根目录执行 `python agent-rep/scripts/sync_from_legacy.py`。脚本按 AST 复制完整顶层定义，不会修改旧文件；同步后应重新运行测试并审查模块间依赖。
