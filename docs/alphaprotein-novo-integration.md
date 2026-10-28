# AlphaProtein Novo 集成说明

## 当前边界

Co-Scientist 仍由 Supervisor、Generation、Reflection、Ranking、Evolution、Proximity 和 Meta-review 负责科研推理、假设管理和反馈循环。AlphaProtein Novo 是一个可选的科学工具，由一个辅助 `scientific_tool` worker 执行。它不替换任何核心 Agent，也不在普通研究问题中自动启动。

官方流水线由 `run_pipeline.py` 编排四个阶段：`run_generator.py`、可选的 `run_ligandmpnn.py`、`run_alphafold.py` 和 `evaluate_design.py`。适配器只负责 manifest 校验、官方命令调用、断点运行委托和结果解析。

## 证据记录

Scientific Evidence Layer 在 `scientific_evidence` 表中保存每个指标，并同时写入 session artifact。每条记录包括：

- `task_id`、`hypothesis_id`、`design_id`
- `kind`（AP Novo metric、结构预测、manifest、执行日志等）
- `status`（`real`、`mock`、`unavailable`、`error`）
- 指标名称和值、模型版本、manifest SHA-256、源文件和 artifact 路径
- 不确定性和说明
- `evaluation_condition`（例如评估 suite）

AP Novo 评估 JSON、`evaluation_summary.csv`、`pipeline_index.json` 以及 AlphaFold 3 confidence JSON 都会保留源路径。`status=mock` 的数据只用于测试，不会被描述成实验结论。结构置信度、催化几何和 self-consistency 不能证明真实催化活性。

## 使用方式

先准备官方仓库中的公开示例 manifest，例如 Kemp eliminase，并确保其 motif CIF 相对路径可读取：

```bash
co-scientist protein-design <session_id> \
  --hypothesis <hypothesis_id> \
  --manifest /path/to/alphaprotein-novo/examples/kemp_eliminase/kemp_test_manifest.json \
  --output-dir /tmp/apn_kemp_mock \
  --mode validate
```

`validate` 只检查输入，不执行模型；`mock` 产生供软件测试使用的布局兼容 fixture 并写入 Evidence Layer，这不代表通过官方科学输入校验或运行了官方模型；`real` 只有在配置同时设置 `alpha_protein_novo.enabled=true` 和 `alpha_protein_novo.allow_real_runs=true`，且任务通过人工审批时才允许执行官方 `run_pipeline.py`。`--hypothesis` 必须对应本 Session 的既存假设；`--output-dir` 保留为兼容参数，实际入队时由系统分配 Session 内的任务专属目录。

Generation/Evolution 的设计请求统一进入 `science/design_queue.py`。Supervisor 会重新校验 manifest、motif/ligand 文件、设计迭代号、任务预算和幂等键后再入队；内容摘要绑定科学输入。真实任务一律进入 `awaiting_approval`，旧配置 `require_real_run_approval=false` 不能绕过审批。人工确认后执行：

```bash
co-scientist approve-protein-design <task_id>
co-scientist resume <session_id>
```

每个 session 的设计任务数和 lineage 迭代数受 `max_design_tasks_per_session`、`max_design_iterations` 限制，失败/取消的提交也计入总限额。原子准入和稳定内容指纹避免并发及重启重放重复提交。审批摘要同时绑定 task payload 和输入文件内容；审批后改动科学输入会使执行校验失败。LLM Tool 不允许直接自批 real 计算。

配置示例：

```toml
[alpha_protein_novo]
enabled = false
allow_real_runs = false
repo_path = "/path/to/alphaprotein-novo"
python_executable = "/path/to/alphaprotein_novo/.venv/bin/python"
apn_model_dir = "/path/to/models/apnovo_generator"
af3_model_dir = "/path/to/models/af3_la"
max_design_iterations = 3
max_design_tasks_per_session = 10
require_real_run_approval = true
execution_backend = "local"
# remote_worker_url = "https://gpu-worker.example.org"
```

任务只会被排入现有队列。完成后 Supervisor 会沿原有链路安排 Reflection；Reflection 读取持久化证据，随后照常进入 Ranking 和 Evolution。Generation 只有在研究者明确提供真实本地 manifest，且模型输出的路径通过文件存在性检查时，才会创建可选的 `protein_design_request`。

Ranking 和正式 Evolution 只消费有效的非 Mock 计算证据；Reflection 可以读取 `mock`，但会标为 `simulation_only`。Ranking、Evolution 和 Meta-review 共用 Review 来源过滤，防止 Mock 经由评语间接影响正式判断。缺失、非有限和错误指标不能作为有效证据；同一 run 的 JSON/CSV 同值指标合并来源，冲突值标为无效，不同 task、单位或评价条件保留区分。数据库保留全部原始来源，Agent 上下文包含证据 ID、task/design/iteration、类型、来源、模型及不确定性。

## A/B 对照评估

`co-scientist scientific-evidence-eval <record.json>` 只比较已经记录的 baseline/evidence 输出，不会调用模型。输入可包含 Reflection、Ranking、Evolution 三个 Agent 的输出、模型名、token/cost 和 evidence IDs；仅在提供 Prompt 时计算 Prompt hash，未知 metadata 保留为未知，预算与实际 usage 分别比较。引用与过度推断检查基于文本规则，不能证明语义或科研结论正确。真实 LLM 运行需要由调用方先按相同模型和预算导出记录，系统不会自动发起付费调用。

## Local / Remote worker seam

`co_scientist/science/worker.py` 定义了 `submit/status/cancel` 的异步 Worker 接口。当前 `LocalAlphaProteinNovoWorker` 由 Supervisor 的持久化任务队列驱动；`RemoteScientificWorker` 只提供未来 GPU 服务的契约占位，不会发起 HTTP 请求。远程服务应返回 job ID、状态、artifact 清单和错误信息，主服务器再统一写入 Evidence Layer。

## 当前验证范围

已覆盖离线 manifest 校验、缺失输入拒绝、Mock campaign 目录、JSON／CSV 解析、证据持久化、真实运行安全门和 Agent 任务结果。真实 AP Novo 与 AlphaFold 3 运行仍需要官方仓库、Python 3.12 环境、模型权重、相应许可以及足够 GPU／CPU 资源；当前版本不会自动下载这些资源。

2026-10-26 持续验证的完整结果为 **451 passed**，其中新增 90 个测试参数实例，包含真实六 Agent 类与 Supervisor 的两轮 Mock 调度。详见 验证总结（本机运行记录，未随源码公开） 和 逐轮记录（本机运行记录，未随源码公开）。这不代表真实 LLM、APN/AF3 或科研效果验证通过。

新增迁移 `0009_task_approval.sql`、`0010_task_result.sql` 用于审批摘要和可恢复的 TaskResult。本轮仅迁移临时测试库，真实科研数据库未改动。启用新版本前需由维护者在备份和停止活动 worker 后执行现有初始化迁移；不要直接用新 worker 操作未升级的旧库。

## 论文与代码映射

- Co-Scientist 的 Supervisor 和任务队列仍位于 `co_scientist/agents/supervisor.py` 与 `co_scientist/storage/repos/tasks.py`。
- Co-Scientist 的 Reflection／Evolution 证据反馈仍由 `co_scientist/agents/reflection.py` 和 `co_scientist/agents/evolution.py` 负责。
- AP Novo 适配器位于 `co_scientist/science/alphaprotein_novo.py`，工具边界位于 `co_scientist/tools/alphaprotein_novo.py`。
- Evidence Layer 的模型和持久化位于 `co_scientist/models/scientific_evidence.py`、`co_scientist/storage/repos/scientific_evidence.py` 和 migration `0006_scientific_evidence.sql`。
- 设计请求和审核状态位于 `co_scientist/models/protein_design.py`、`co_scientist/agents/supervisor.py` 和 `co_scientist/storage/repos/tasks.py`；评估条件字段由 migration `0007_evidence_conditions.sql` 加入。
- 官方输出布局依据 AlphaProtein Novo README 和 `run_pipeline.py`、`evaluate_design.py`、`design_manifest.py` 的实际实现解析；没有复制官方生成或评分算法。
