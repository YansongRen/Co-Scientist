# Cursor 交接说明

交接日期：2026-11-10（Asia/Shanghai）。本文件记录仓库快照和实际验证，不代表服务器实时状态。

## 项目与目标

项目是基于 AI co-scientist 的 Nanopore Research Copilot：从自然语言研究目标出发，进行文献检索、假设生成、审查、比较、迭代与报告；新增适合纳米孔研究的网页工作区、研究范围引导、附件和证据追踪。产品方向与具体研究任务需随维护者需求继续明确，当前交接请求没有授权新的功能目标。

本机工作目录是 `/Users/song/project/Nanopore Research Copilot/Co-Scientist`。父目录自身不是 Git 仓库。相邻 `co-scientist-explorer/` 是独立教学页面，源码固定于原始提交，不能用于判断当前实现；`co-scientist-deploy.tar.gz` 是旧部署归档，也不是本次源码来源。这两项不包含在本次应用仓库上传中。

原始本地基线为 `cef5bcfec8820865855593b437a941005a9f961a`，个人仓库 main 已有较早快照 `4fd48f55cc7dc2a50235a15e9607d0fd42a631d4`。本交接分支以该个人仓库快照为父版本，补齐其后的本地工作。原远程 `origin` 为 `https://github.com/Kaimen-Inc/Co-Scientist.git`。维护者指定交接目标为 `https://github.com/YansongRen/Co-Scientist`。上传远程名为 `handoff`，保留 `origin` 指向原项目。交接工作使用新分支 `codex/cursor-handoff-20261110`，保留原有历史，不覆盖远程 main。上传结果以该分支在 GitHub 上实际存在为准。

交接前有 49 个已跟踪文件修改、83 个未跟踪文件，涵盖此前多轮开发；这是既有成果的整体快照，并非本轮重写。具体提交范围请以 Git diff 为准。本轮新增 `AGENTS.md`、本文件与公开技术说明 `docs/generation-and-refinement.md`，保留个人远程仓库已有的 README 介绍和私密文件忽略规则。

## 已有功能与关键决定

| 范围 | 当前实现与入口 |
| --- | --- |
| 科研核心 | Supervisor + 六类核心 Agent；SQLite 持久化任务队列、Elo、FAISS、事件/SSE、预算与恢复。`agents/`、`storage/`、`orchestrator/` |
| 网页工作区 | Studio、研究详情、证据核查、Agent 网络与活动；标准/轻量研究路径。`web/studio.py`、`web/static/`、`docs/local-research-workspace.md` |
| 研究范围引导 | 独立草稿、讨论、可编辑任务书、用户确认与 AI 建议来源、预算、原子确认并启动正式 Session。`scoping.py`、`docs/research-scoping.md` |
| 附件 | PDF/DOCX/TXT/MD/CSV/JSON 文字提取和图片预览，关联研究输入；视觉发送需单独配置与模型支持。`inputs.py`、`docs/research-scoping.md` |
| 生成与迭代 | literature/debate/assumption/feedback_driven 四种策略；有界初始/补充生成、保存时去重、局部就绪 Evolution。`generation_strategies.py`、`vectors/admission.py`、`docs/generation-and-refinement.md` |
| 文献核查 | 引文原文匹配、新颖性台账、DNA/RNA 范围提示；没有把字符串匹配描述成科学支持。`safety/evidence.py` |
| 可选蛋白质计算 | AlphaProtein Novo 本地适配、Evidence Layer、人工审核、任务/输入指纹、配额与两轮 Mock 集成；远程 worker 仍为接口占位。`science/`、`docs/alphaprotein-novo-integration.md` |
| 部署 | Docker/Compose 模板及 Python + systemd/Nginx 部署说明，旧 SQLite 兼容修复。`deployment/`、`docs/deployment*.md` |

不得把 Mock/结构预测/引文匹配/Elo 直接描述成真实科研成果。核心状态、证据来源、Session 隔离、任务恢复和预算限制是现有设计约束，详见 `AGENTS.md`。

## 本轮实际验证

2026-11-10，在现有 Python **3.13.13** `.venv` 中执行：

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q co_scientist/tests/unit
# 562 passed in 18.17s

.venv/bin/python -m pip check
# No broken requirements found.
```

此外 `git diff --check` 和四个新增前端脚本（studio、scoping、inputs、agent-network）的 Node 语法检查通过。

另将暂存区导出到全新临时目录，在没有本地 `.env`、个人配置、研究数据或私有验证文件的副本中，使用同一解释器和已安装依赖再次执行全部单元/组件测试：**562 passed in 15.81s**。该验证确认待上传文件集合完整，不等于在新电脑重新安装依赖已验证。

本轮是离线单元/组件回归，没有重新验证浏览器操作、服务器状态、真实模型推理、真实 APN/AF3 或科研效果。本机忽略目录 `docs/validation/` 中 451/472/499/505 等测试数字属于较早记录，不能代替本轮结果；其中 JSON 是 Mock/隔离预览记录，不是正式科研数据库。这些运行记录保持本地，不随本次上传。

## 接手运行方式

同一台电脑可直接使用当前 `.venv`。新电脑先按 `AGENTS.md` 安装依赖；不复制旧虚拟环境。默认配置依赖 Provider 和模型选择，本地个人配置未上传，clone 后不能假定拥有相同后端。

首次看界面建议：

```sh
.venv/bin/python scripts/scoping_preview.py --port 7882
# http://127.0.0.1:7882/#new
```

该脚本使用临时数据、Mock 并截获正式科研启动。需要浏览已有本地研究时才使用普通服务，并先确认有效配置：

```sh
.venv/bin/co-scientist serve --host 127.0.0.1 --port 7879
```

普通服务会加载真实个人配置；网页新建研究可能使用真实模型额度。数据库或配置迁移前先明确选用的环境。

## 历史部署与待验证问题

本机私有记录 `docs/deployment-preview-verification.md`（不随上传）记载 2026-10-27 的阿里云部署与后续修复：应用在 `/opt/co-scientist`、数据在 `/data`、systemd + Nginx；后续已配置 DeepSeek 并打开真实引导。这些是历史事实记录，本轮没有访问或修改服务器，也未核对当下模型可用性。

重要上下文：

- 曾有真实任务仅完成 parse_goal，Generation 未启动。旧服务器 SQLite 3.26.0 不支持任务领取的 `RETURNING`，现已改为事务内选择/更新/读取；查询的 `NULLS LAST` 也已替换。
- 历史文档报告补丁已部署，但服务器最终 Mock 队列验收输出没有完成核对；真实端到端科研成功也没有充分证据。接手时不可把 healthz、模型列表认证或本地测试通过当作真实科研闭环通过。
- 旧失败任务没有自动重跑；若维护者要求真实联调，应明确预算与新任务，先收集失败记录，避免盲目恢复旧任务。
- 公开部署历史上没有完整 HTTPS、登录、用户所有权及配额保护。若下一步要给多人使用，应先完成这些产品需求。
- 引导在“正式 Session 已创建、Supervisor bootstrap 尚未完成”之间跨进程退出时，可能留下暂停准备 Session；普通 resume 不能被当作完整 bootstrap 恢复协议。
- 真正 APN/AF3、GPU 资源、官方输入校验和科学效果未验证；远程 worker 尚未实现。扫描 PDF 无 OCR，附件解析也不是完整安全沙箱。

## 文件与数据交接范围

上传范围：应用源码、Prompt、默认/示例配置、迁移、测试、公开技术文档及部署模板。保留 LICENSE。待上传文本已做密钥模式检查，并与本地 `.env` 中的密钥值比对；匹配结果仅为代码字段引用及明确伪造的测试值，没有发现本地密钥进入候选文件。

个人远程原有规则明确排除了本机验证记录；本次沿用这些规则，没有上传 `docs/validation/` 和 `docs/deployment-preview-verification.md`，必要工程信息已归纳到本文件和公开技术说明。

以下仍仅在原机器或原服务器：`.env`、个人 `co-scientist.toml`、实际部署配置及备份、`data/`（SQLite、artifact、日志、向量、上传原件）、虚拟环境、模型权重和外部 vendor。Git clone 不会恢复它们；换电脑时应另走私密数据备份，不把这些内容补进代码仓库。服务器数据也没有导回本机。

## Cursor 第一轮任务

1. 阅读本文件与 `AGENTS.md`，检查当前分支、Git 状态和上传分支的 diff；识别这是对原项目的扩展。
2. 运行上述离线测试，启动隔离预览并确认页面；如环境不同，记录依赖和平台差异。
3. 向维护者确认接下来优先处理的产品/功能问题。可选方向包括真实研究链路小预算验证、部署访问控制、或引导/附件体验；本交接没有替维护者选定新目标。
4. 在获得明确下一项任务后实施，以测试和实际运行记录更新本文件。不要为了“接手”而先做大规模重构或重新部署。
