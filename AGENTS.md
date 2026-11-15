# 开发约定

## 从这里开始

- 先读 `HANDOFF.md`，了解当前版本、验证结果、历史部署和下一步工作。它是交接入口；代码与实际运行结果是最终依据。
- 本仓库是通用科研 co-scientist 应用，基于开源 AI co-scientist 扩展。保留 Apache-2.0 许可及原作者信息。纳米孔研究对象选择和 AlphaFold / AlphaProtein Novo 路径已移除。
- 使用中文与维护者沟通。实施前检查 `git status` 和相关 diff，保留已有修改。
- 每次交付说明修改内容、实际执行的验证和剩余限制；持续更新 `HANDOFF.md` 中已经改变的状态。

## 代码地图

- `co_scientist/cli.py`：Typer 命令入口。
- `co_scientist/agents/`：Supervisor 与 Generation、Reflection、Ranking、Evolution、Proximity、Meta-review。
- `co_scientist/orchestrator/`：调度策略、Elo、终止和事件；`storage/`：SQLite 队列、迁移及 repositories；`vectors/`：embedding、FAISS 与准入去重。
- `co_scientist/llm/`：Provider、CLI backend、工具循环、路由、预算及账目；`config/default.toml` 与 `config/prompts/`：默认配置及运行 Prompt。
- `co_scientist/web/`：FastAPI、Jinja、原生 JavaScript/CSS、SSE。当前主界面是 Studio，旧界面在 `/classic`；本项目没有 Node 前端构建流程。
- `co_scientist/scoping.py`、`web/scoping.py`：引导式研究；`inputs.py`、`web/inputs.py`：附件及提取。
- `co_scientist/models/scientific_evidence.py`、`safety/evidence.py`：历史计算证据表与文献引文核查。当前没有蛋白质设计执行器。
- `docs/generation-and-refinement.md`：生成与迭代机制；本机忽略目录 `docs/validation/` 保存历史运行记录，不随 clone 恢复；`deployment/` 与 `docs/deployment*.md`：部署模板和历史操作记录。

## 环境与验证

要求 Python 3.11–3.13；推荐新环境使用 3.12。当前本机已有 `.venv`，优先使用它。

```sh
# 仅在新环境需要安装时执行
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

# 离线测试，使用临时数据库
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q co_scientist/tests/unit
.venv/bin/python -m pip check
git diff --check

# 可选：对修改的 JavaScript 文件做语法检查
node --check co_scientist/web/static/studio.js
```

界面演示优先使用隔离预览：

```sh
.venv/bin/python scripts/scoping_preview.py --port 7882
```

它创建系统临时目录、使用 Mock、截获正式研究启动。浏览 `http://127.0.0.1:7882/#new`。普通 `serve` 会加载本地真实配置，不等同于离线预览。

## 配置与数据

- 配置顺序：`config/default.toml` → `~/.co-scientist/config.toml` → 当前目录 `co-scientist.toml` → CLI `--config`。密钥来自环境/`.env`，详见 `config.py`。
- 更换 Provider 时逐项核对 `[models]`；配置深度合并，未覆盖项会沿用默认值。OpenAI-compatible Provider 的密钥选择还受 `OPENAI_API_KEY` 优先级影响。
- `.env*`（保留空白示例）、`co-scientist.toml`、真实 `config/deployment.toml` 及备份、`data/`、SQLite/WAL、上传原件、模型权重、`.venv/` 不入库。使用示例配置传递结构，不复制秘密。
- 不把本地服务、临时预览和服务器的 Session/数据库混作同一环境。真实数据库迁移前停止活动 worker，正确备份 SQLite/WAL，确认配置和数据目录后再 `init`。
- 交接本身不包含线上部署、重跑历史失败任务或启用真实模型；只有维护者明确要求这些操作时再执行。

## 必须保留的语义

- SQLite 持久化队列、lease/heartbeat、幂等及预算恢复必须保持；不得通过跳过测试或放宽断言掩盖失败。
- 旧服务器历史 SQLite 为 3.26.0。新 SQL 要检查兼容性，避免重新引入 `RETURNING`、`NULLS LAST`；相关回归见 `test_deployment_queue.py`、`test_sqlite_ordering.py`。
- 新 schema 同步更新 `storage/schema.sql` 和递增迁移，不改写已经使用的历史迁移；校验旧数据保留、外键和任务恢复。
- Mock 证据不得升级成 real，不经由 Review 间接进入正式 Ranking/Evolution/Meta-review。
- 原文匹配仅说明引文出现在工具返回内容，不代表论断正确或来源支持假设；检索无结果不等于创新性已确认；Elo 不等于实验效果。
- 保留 Session 隔离、artifact 路径范围、附件限制和密钥脱敏。网页登录只区分本机的管理员和普通用户，密码以哈希存在本地库；这不是完整用户认证或租户 ACL，不能把 Session ID 隔离当作用户权限。
- 离线测试通过只说明所覆盖的软件行为；真实 LLM、浏览器交互与科研效果分别报告验证证据。
