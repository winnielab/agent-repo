# RepoPilot

RepoPilot is a provider-neutral autonomous issue-solving harness. The default
Stage 2 workflow analyzes an issue, explores the repository with read-only
tools, persists a task DAG and working memory, runs focused Coder workers,
reuses the existing PR review agent, evaluates acceptance criteria, and performs
bounded repair passes inside an isolated Git worktree.

Stage 3 is available as an explicit end-to-end workflow. It shares one
Analyze/Explore/Plan pass, executes each ready TaskGraph frontier through a
lease-backed `AgentCoordinator`, gives every worker an isolated worktree and
runtime, atomically merges hash-validated patches, verifies the merged wave,
and only then releases dependent tasks. Conflict consolidation, pre-merge patch
hygiene, partial-patch preservation, per-worker evidence, and run manifests are
part of this path. The Stage 2 workflow remains the default for compatibility;
select Stage 3 explicitly:

```bash
repopilot solve --repo ./demo --task "Fix divide() so all tests pass" \
  --test-command "python -m unittest discover -v" \
  --provider deepseek --model deepseek-chat \
  --workflow stage3 --max-concurrency 2 --max-worker-tokens 350000
```

The lower-level hash-chained workflow journal and atomic checkpoint runtime are
also available, but a public `repopilot resume RUN_ID` command is not yet
shipped.

```bash
export DEEPSEEK_API_KEY=...
repopilot solve --repo ./demo --task "Fix divide() so all tests pass" \
  --test-command "python -m unittest discover -v" \
  --provider deepseek --model deepseek-chat
```

Use `--workflow task` to run the Stage 1 single-Coder path. The original PR
review agent remains in `agent-rep/pr_review_agent`; only the Stage 2 adapter
imports it, so it is not a dependency of the generic runtime.

For repositories whose dependencies live in a Docker image, keep file editing
in the isolated host worktree while running model-requested shell commands in
the task image:

```bash
repopilot solve --repo ./target --task "$TASK" --test-command "$HOST_TEST_COMMAND" \
  --execution-image example/task:latest --execution-platform linux/amd64 \
  --execution-setup-command 'source /opt/env.sh'
```

The image receives only the isolated worktree and its Git metadata. The model
does not receive permission to invoke the host Docker CLI. A resolved report
can be converted to SWE-bench JSONL with `scripts/swebench_prediction.py`.

Prepared repositories may contain Git-ignored native modules required at
runtime. Worktree creation therefore copies only ignored `.so`, `.pyd`,
`.dylib`, and `.dll` files, with bounded file-count and byte limits; these
artifacts remain outside the patch. Use `--no-hydrate-runtime-artifacts` to
disable this behavior or the `--max-runtime-artifact-*` options to tighten it.
Configured test metadata treats a successful process as authoritative and only
uses failure-ID baseline comparison for nonzero test runs.

The formal Stage 2 medium-task suite is frozen in
`benchmarks/swebench_verified_medium_v1.json`. Run it serially on a 16 GB
Apple Silicon machine so only one amd64 image, agent, or evaluator is active:

```bash
uv run python scripts/run_swebench_serial.py \
  --manifest benchmarks/swebench_verified_medium_v1.json \
  --task-repo /Users/ayc/agent/swe-bench-tasks \
  --swebench-root /Users/ayc/agent/SWE-bench \
  --repopilot-root /Users/ayc/agent/agent-rep \
  --workspace-root /Users/ayc/agent/swebench-formal-workspaces/stage2-medium-v1 \
  --output-root /Users/ayc/agent/repopilot-swebench-results/stage2-medium-v1 \
  --env-file /Users/ayc/agent/agent-rep/agent-rep/.env.example \
  --start 12 --count 1
```

Each task performs a gold preflight, materializes a clean base commit, runs the
blind RepoPilot solve, and submits any non-empty patch to the official harness.
Existing `result.json` files are resumable checkpoints; frozen failures are not
replaced.

## Architecture

```text
CLI → SolveIssueWorkflow
          ├── TaskAnalyzer → TaskSpecification
          │                    └── BehaviorContract
          │                        (positive/negative/boundary/temporal/
          │                         preservation/failure-path + state effect)
          ├── RepositoryExplorer (bounded, read-only AgentRuntime)
          │                    └── task-adaptive evidence slice
          │                        (instructions → definitions → callers/state → tests)
          ├── Planner → cited evidence + falsifiable hypotheses + impact surface
          │                                             └── persisted TaskGraph
          ├── CoderWorker → AgentRuntime → tools/environment/worktree
          ├── PRReviewAdapter → existing pr_review_agent (reuses authoritative test)
          └── GoalEvaluator + SourceContractGuard + PatchHygieneGuard
                                   └── counterexample-driven RepairLoop
```

Concrete providers and tools are assembled only in the workflow. The runtime
does not import them, and the Coder owns only a prompt and a tool allowlist.
Repository comprehension is progressive rather than a full-repository dump:
the Explorer records verifiable observations and the Planner must cite those
observation keys. Read-only exploration has independent iteration, tool-call,
and token ceilings so implementation and finalization retain their budgets.
Behavior-aware plans must also state a material root-cause claim and a source
trace or focused read-only probe capable of disproving it. The Coder performs
that check before editing, command output becomes evaluator evidence, and an
unsupported or contradicted hypothesis is returned to the repair loop instead
of being treated as an implementation fact.
Unless the issue explicitly requests tests, editing tools reject writes to test
paths before mutation; final patch hygiene also rejects debug/reproduction
artifacts and unrequested test changes.

## Verification

```bash
uv sync
uv run pytest -q

# Deterministic ten-case harness E2E (not a model-quality score)
cd /Users/ayc/agent/repopilot-test-benchmark
python3 scripts/run_all.py \
  --adapter /Users/ayc/agent/agent-rep/scripts/scripted_benchmark_adapter.py

# Real DeepSeek Stage 1 evaluation
python3 scripts/run_all.py \
  --repopilot /Users/ayc/agent/agent-rep \
  --env-file /Users/ayc/agent/agent-rep/agent-rep/.env.example \
  --provider deepseek --model deepseek-chat
```

See [the Stage 1 implementation report](docs/RepoPilot_Stage1_实现与验证.md)
and [the Stage 2 implementation report](docs/RepoPilot_Stage2_实现与验证.md)
for module boundaries, event contracts, benchmark results, and current gate status.
The current architecture re-review, semantic-evidence changes, multi-agent target
design, and the new 16-task acceptance protocol are documented in
[the Stage 2 architecture review](docs/RepoPilot_Stage2_架构复查与改进_20261001.md).
Development failures, root causes, fixes, diagnostic reruns, and the frozen-suite
continuation protocol are tracked in
[the development issue log](docs/RepoPilot_开发问题与修复记录.md). The task-adaptive
repository-comprehension design and the path-verified v6 failed-PR regression
are documented in
[the comprehension regression report](docs/RepoPilot_项目理解与失败PR回归_20260930.md).
The Stage 3 infrastructure foundation is recorded in
[the Stage 3 infrastructure report](docs/RepoPilot_Stage3_基础设施实现_20261001.md).
The completed Coordinator, defects found by real execution, and five-task
SWE-bench rerun are documented in
[the Stage 3 Coordinator report](docs/RepoPilot_Stage3_Coordinator实现与五项复测_20261001.md).
The frozen DeepSeek V4 Flash evaluation of the remaining eleven runnable images,
including provider-fallback telemetry and the resulting design issue backlog, is
documented in
[the Stage 3 V4 Flash report](docs/RepoPilot_Stage3_剩余十一项_V4Flash评测与问题分析_20261004.md).
