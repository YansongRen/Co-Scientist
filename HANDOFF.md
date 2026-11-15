# Cursor 交接说明

交接日期：2026-11-10（Asia/Shanghai）。本文件记录仓库快照和实际验证，不代表服务器实时状态。

## 项目与目标

项目是基于 AI co-scientist 的通用科研助手：从自然语言研究目标出发，进行文献检索、假设生成、审查、比较、迭代与报告；网页工作区包含研究范围引导、附件和证据追踪。纳米孔研究对象选择和 AlphaFold / AlphaProtein Novo 蛋白质设计路径已从当前产品移除。产品方向与具体研究任务需随维护者需求继续明确。

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
| 文献核查 | 引文原文匹配与新颖性台账；没有把字符串匹配描述成科学支持。`safety/evidence.py` |
| 历史计算证据表 | 蛋白质设计执行路径已删除。旧库里的 `scientific_evidence` 表和任务枚举仍可读取；模拟证据继续排除在 Ranking、Evolution 和 Meta-review 之外。 |
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

## Cursor 接手验证（2026-11-10）

同一台机器、同一 `.venv`（Python 3.13.13，conda-forge）复查，环境与上文记录一致。

- 分支 `codex/cursor-handoff-20261110` 与 `handoff/codex/cursor-handoff-20261110` 同步，工作区干净。相对个人仓库快照 `4fd48f5`，交接提交补上生成与迭代（35 个文件，+2179/−434）以及 `AGENTS.md`、本文件和 `docs/generation-and-refinement.md`。相对原始基线 `cef5bcf` 只有 `4fd48f5` 与 `0051a21` 两次提交，这是对原项目的扩展。
- `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q co_scientist/tests/unit`：**562 passed in 16.48s**。
- `.venv/bin/python -m pip check`：No broken requirements found。`git diff --check` 通过。
- Node v26.7.0 对 `studio.js`、`scoping.js`、`inputs.js`、`agent-network.js` 的语法检查通过。
- `scripts/scoping_preview.py --port 7882` 已启动，数据在系统临时目录。浏览器打开 `http://127.0.0.1:7882/#new`，标题为「Co-Scientist · 研究工作区」；页面有新建研究、引导式研究（开始梳理思路）和直接研究表单（模式、对象、预算、并发、Token、初始假设、时限）。预览截获正式启动，没有执行真实科研。

本次仍是离线测试加页面可达性确认，没有浏览器内完整提交流程、真实模型、APN/AF3 或服务器核对。

## Cursor 第一轮任务

1. 已完成：阅读本文件与 `AGENTS.md`，核对分支、Git 状态和相对父版本的 diff。
2. 已完成：离线测试、依赖检查和隔离预览页面确认，见上一节。
3. 维护者已选择「真实研究链路小预算验证」，随后暂停付费调用。暂停原因是：难以判断一次运行的结果好不好，也不清楚真实科研人员的使用感受。在形成可执行的核对清单之前，不恢复历史失败任务，不改 `data/` 里已有数据库，不开启 APN，不部署，也不发起新的模型调用。
4. 维护者确认核对方式后，再按明确的新任务和预算实施，并以测试和实际运行记录更新本文件。

## 论文深层审查已接入（2026-11-12）

按 Nature 论文 Methods 补上此前只写了提示词、执行器会拒绝的审查，以及相近假设对打和策略偏向。没有调用真实模型，没有改 `data/` 里的已有库。

新假设的审查顺序是：初审（不检索）→ 全文审查 → 深度验证 → 观察审查 → 失败情景模拟。初审结论为 `disproved` 或 `already_explained`，或后续审查为 `disproved` 时，假设标为 rejected，不进入锦标赛。模拟审查写入失败情景，并继续被排除在 Elo、Evolution 和 Meta-review 的推理输入之外。没有可核对文章时，观察审查直接记录“无发现”，不调用模型。Meta-review 文字会附在后续审查提示里；没有为每轮反馈再排一次审查，避免反馈和审查互相触发。

Ranking 优先比较同一相似簇、嵌入更近的假设。至少两种策略已经有比赛 Elo 后，空闲 Generation 和 Evolution 会把平均 Elo 更高的策略排在前面，其余策略仍保留。研究者可以用 `co-scientist propose`、`POST /api/sessions/{id}/hypotheses`，或工作室“研究假设”页提交自己的假设，先走同一条初审。

验证：`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q co_scientist/tests/unit` 为 **569 passed in 19.63s**。`node --check co_scientist/web/static/studio.js` 和 `git diff --check` 通过。隔离预览 `http://127.0.0.1:7883/#session/preview-form` 打开后，假设页能看到提交框。浏览器里的“加入审查队列”点击被拦截，改用同一接口 `POST /api/sessions/preview-form/hypotheses` 核对：返回 `created: true, review_queued: true`，库里是 `created_by=researcher`、`strategy=human`、`state=draft`，并有一条 pending 的 `ReviewHypothesis`（`kind=initial`）。预览进程已停，没有调用真实模型。经典页面的表单没有在浏览器里点过。

仍未实现论文中的多轮自我对弈、逐层假设拆解、Evolution 的单独文献补强、领域联系人推荐，以及按方法效果做统计抽样。Elo 仍然不是实验结果。

## 论文其余机制已接入（2026-11-12）

在上一节的审查链之上，补上 Methods 里仍缺的五块。没有调用真实模型，没有改 `data/` 里的已有库。

- 生成策略 `debate` 先做有界自我对弈（默认 2 轮文本，再注册 1 条假设）。`assumption` 先做有界假设跳（同样默认 2 轮文本再注册）。前面的回合不能调用 `record_hypothesis`。
- Ranking 的 debate 模式默认 2 轮；pairwise 仍是 1 轮。只解析最后一轮的 `better idea`。
- Evolution 新增 `grounding`：检索后写一条新的子假设，不改写父假设。默认策略列表末尾包含它。轻量模式仍只取排序后的前两个，因此默认顺序下轻量轮次不会跑到文献补强。
- 研究总览追加联系人一节。姓名只从已记录摘录里的 “Surname et al.” 或 “某某等人” 抽取。没有这样的摘录就不推荐联系人，也不写邮箱或单位。
- 至少两种策略已有比赛 Elo 时，空闲生成和演化的策略顺序改为按权重抽样，同一种子和权重结果稳定。生成与演化都已有比赛 Elo 时，二者之间也按权重决定先做哪一个；抽到的动作排不进去时改走另一个，避免饿死。不足两种测量时仍按配置顺序。审查链和锦标赛批次不参与这次抽样。

验证：`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q co_scientist/tests/unit` 为 **573 passed in 20.31s**。`git diff --check` 通过。Elo 仍然不是实验结果。改动尚未提交。

## 纳米孔与蛋白质设计路径已移除（2026-11-12）

创建研究不再询问 DNA/RNA，也不再根据目标文字推断研究对象或写出范围警告。AlphaProtein Novo / AlphaFold 的适配器、设计队列、科学计算 worker、CLI 命令和配置段已删除。历史库仍可打开：旧迁移不改写，`scientific_evidence` 表和 `scientific_tool` / `RunProteinDesign` 任务字面量保留，以便旧行能被读出；没有注册对应执行器，等待中的设计任务不会被批准执行。模拟证据仍然不能进入 Ranking、Evolution 和 Meta-review。

轻量锦标赛在有 `focus` 时优先比较这条新假设。没有调用真实模型，没有改 `data/` 里的已有库。

验证：同一条单元测试命令为 **531 passed in 21.43s**。`node --check co_scientist/web/static/studio.js` 和 `git diff --check` 通过。隔离预览 `http://127.0.0.1:7884/#direct` 的创建表单只有研究模式、预算、并发、Token、初始假设和运行时限。提交“比较两种测量误差的可能来源”后会话页和证据页能打开，预览没有启动 Supervisor。预览进程已停。改动尚未提交。

## 开放全文阅读（2026-11-13）

检索结果仍是题名和摘要。生成、审查和文献补强在引用一篇论文的方法或结果前，会调用 `read_paper`。它读取 PMC 开放 XML、arXiv 的 HTML 或 PDF，以及直接给出的开放链接，并把方法、结果、讨论交回模型；没有章节标题的长文则交回开头、中段和结尾。没有开放全文时，结果标明 `abstract_only`，总览和证据卡不能把这篇写成已经读过正文。付费墙里的全文、图表和补充材料仍然读不到。摘录对上原文，仍只说明这句话出现在返回内容里。

验证：`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q co_scientist/tests/unit` 为 **538 passed in 26.90s**。`node --check co_scientist/web/static/studio.js` 和 `git diff --check` 通过。没有调用真实模型和真实文献接口。证据卡上的“已读开放全文 / 只读到摘要”没有在浏览器里用一条真实文献卡点过。改动尚未提交。

## 生成假设使用独立模型（2026-11-13）

生成假设按 `[generation].authors` 的顺序轮流写。名字在 `[llm.endpoints]` 里时走该端点；`session` 使用当前会话 Provider。审查、排序、演化和总览仍走会话 Provider。命名端点不继承会话 Provider 的 `extra_body`。

本机配置（不入库）：生成假设按任务序号轮流由 Kimi `kimi-k3.1-pro`、GLM `glm-5.3` 和会话里的 DeepSeek `deepseek-flash` 撰写。审查、排序、演化和总览仍只用 DeepSeek。密钥只在 `.env` 的 `KIMI_API_KEY`、`GLM_API_KEY`。模型名来自两边当时的模型列表，没有启动付费科研任务。本机 `co-scientist.toml` 仍把生成和审查的工具循环限制为 3 轮，预算为 1 美元。

验证：同一条单元测试命令为 541 条通过，`test_multiple_agent_types_really_overlap_in_existing_worker_pool[3]` 一次因 SQLite `database is locked` 失败，单独重跑通过。`git diff --check` 通过。改动尚未提交。

## 登录页（2026-11-13）

`/login` 用和工作区相同的浅色界面，左右分别是普通用户和管理员。某个身份还没有账号时，第一次提交会在本机创建；之后需要密码。普通用户进入研究工作区，管理员进入 `/admin` 查看账号和研究数量。有账号之后，未登录访问工作区会回到登录页。密码以 PBKDF2 哈希存在 `accounts` 表，登录态是本机签名 cookie。研究 API 没有因此改成多用户权限。

验证：同一条单元测试命令为 **544 passed in 20.88s**。`git diff --check` 通过。隔离预览 `http://127.0.0.1:7886/login` 上创建普通用户后进入工作区，侧栏显示该身份；创建管理员后进入管理页并能看到两个账号。错误密码留在登录页，普通用户打开 `/admin` 会看到只对管理员开放。窄屏下两张卡片改为上下排列。预览进程已停。改动尚未提交。
