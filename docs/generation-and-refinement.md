# 假设生成、去重与局部迭代

本文描述当前代码行为，供开发和接手时查阅。离线软件测试不能证明科学新颖性或真实科研效果。

## Generation 策略

`agents/generation_strategies.py` 显式分派四种策略：

| 策略 | 行为 |
| --- | --- |
| `literature` | 使用有界文献查询，提出一条可检验机制 |
| `debate` | 在单次模型调用中模拟不同论证视角，输出一条终稿 |
| `assumption` | 质疑关键假设，提出反例、替代机制或区分性观察 |
| `feedback_driven` | 根据已有研究者或 Meta-review 反馈生成；无反馈时明确 fallback 到 `assumption` |

`[generation].strategies` 按配置顺序循环分配，重复项表示整数权重。初始任务和有界 idle refill 使用持久化幂等 key。未知策略、`n != 1` 或多个 `record_hypothesis` 终结记录会拒绝发布。每次任务只允许一个假说。

生成上下文只读取有限的当前池摘要与反馈；配置见 `GenerationCfg` 和 `config/default.toml`。不会为了去重把全部历史全文放进 Prompt。标准队列、六类 Agent 和原工具链继续使用。

## 保存与向量一致性

Generation 与 Evolution 共用 `vectors/admission.py::publish()`，在 Session 文件锁中执行确定性 ID 检查、容量检查、最新索引查询和保存。Proximity 使用同一池锁。所有模式都受 `max_ideas` 限制，不删除旧假说来腾出容量。

SQLite 与 artifact 是权威数据，FAISS 是可恢复派生缓存，两者没有跨存储原子事务。索引按 embedding 模型身份隔离；查询前按数据库补齐遗漏向量与 metadata。缓存损坏保留副本后重建；提交失败明确记录，持续故障时任务失败。

Embedding 服务超时、非法或非有限向量会明确降级到 hash，并在同一 hash 空间重建池。Hash 主要识别词汇重叠，未检出重复不能证明语义不同或科学新颖。语义阈值仍需领域标定。

Proximity 更新聚类标签，不删除假说。Ranking 使用标签优先选择不同方向的候选，仍保留同簇比较。Elo 代表相对优先级，不是正确性或实验效果评分。

## 小规模成熟度

小池可使用配置化的比赛次数、成熟比例及可选 top 分离/稳定性门槛。普通大规模运行仍保留默认 `min_mature=20`。小池门槛、剩余预算和成对比较上限由 `orchestrator/research_policy.py` 与 Supervisor 协同检查，避免小池永远达不到固定大池门槛。

配置参见 `[evolution]` 的 `small_pool_*`、`strategies`，以及 `[ranking]` 的轻量比较限制。Evolution 子假说保留服务器指定的 parent IDs，重新进入 Reflection → Ranking。

## 局部就绪迭代

新建 Session 默认启用：

```toml
[evolution]
streaming_enabled = true
max_streaming_rounds = 4
```

任务完成通知会触发 `Supervisor._schedule_ready_refinement()` 检查局部成熟候选；其他 Generation 或队列任务仍在执行时，也可调度 Evolution。后续 Reflection、入赛、比较和就绪 Evolution 的优先级允许先推进已有候选。

调度时检查 Session 状态、预算、时限、容量、成熟条件与合规 Review；将 `parent_ids`、`parent_review_ids` 和 revision 固定到 task payload。确定性 key 避免重复通知、Elo 波动和重启产生相同任务。同 Session 最多一个等待/执行中的 Evolution，失败也计入 streaming 轮数上限。

Evolution 执行时读取被冻结的父假说及 Review 版本，检查归属和有效性，不按最新排行榜偷偷替换父候选。保存仍经过池锁内的容量和去重检查；入队时有容量不保证执行时有容量。

旧 Session 缺少该配置快照时保持旧调度合同，不因恢复历史研究自动增加计算。启用 streaming 后，旧 idle 分支不再重复安排 Evolution，仍负责有界 Generation、Ranking 和 Meta-review 等工作。

局部迭代仍需可用 worker 槽位，仍需同一假说完成合法评审与成熟条件，也保留预算、时限、停止和真实计算审批机制。它没有把所有 Agent 改成永久驻留进程。

## 可观测性与回归

`obs/diversity.py` 向新报告与 Session metadata 写入程序诊断，包括请求/有效策略、去重拦截、Evolution 尝试、embedding backend、hash 降级、缓存恢复、覆盖率和有限规模的高相似比例。历史报告不会自动回填；缺失覆盖率保留未知，不填成零。

主要测试：

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q \
  co_scientist/tests/unit/test_hypothesis_diversity.py \
  co_scientist/tests/unit/test_streaming_refinement.py \
  co_scientist/tests/unit/test_agent_network.py
```

测试覆盖策略分派、fallback、终结记录、跨进程去重、容量、向量降级/恢复、调度幂等、冻结父候选/Review 和子假说后续链。streaming 集成场景阻塞无关 Generation，验证 Evolution 及子假说审查能在该任务结束前完成。部分调度 fixture 使用 1.01 去重阈值隔离调度行为，不能作为真实多样性证据。

2026-11-10 全部单元/组件回归为 **562 passed**，使用现有 Python 3.13 环境和临时数据库。没有进行新的真实 LLM、APN/AF3、GPU 或实验验证。当前文件锁只支持同一可靠文件系统上的本地进程，不提供多主机分布式事务保证。
