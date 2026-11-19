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

## 登录页（2026-11-17）

`/login` 只提供登录和注册。打开页面时如果还没有管理员账号，会写入用户名 `rys`；这个用户名不能再被注册。管理员登录后进入 `/admin`，注册用户进入研究工作区。有账号之后，未登录访问工作区会回到登录页。密码以 PBKDF2 哈希存在 `accounts` 表。研究 API 没有因此改成多用户权限。

验证：同一条单元测试命令为 **544 passed in 23.95s**。`git diff --check` 通过。隔离预览登录页只有登录和注册，没有并列的身份入口。

## 阿里云更新准备（2026-11-18）

维护者明确要求将个人仓库 `codex/cursor-handoff-20261110` 分支部署到原阿里云服务器，本次授权取代此前“暂不部署”的暂停约定。没有授权恢复历史失败任务或额外发起付费模型验证。

- `git ls-remote handoff refs/heads/codex/cursor-handoff-20261110` 确认远程最新提交为 `2e943c8c7aeb5467a5112c3f2ab122bfc876e573`，与本地 HEAD 一致；开始时工作区干净。
- 离线完整测试 **544 passed in 21.65s**；`pip check` 与 `git diff --check` 通过。
- 从该提交用 `git archive` 生成临时部署包，SHA256 为 `7db860ebcb61269e746057a96dcc9b0166f76e72eb0bf1b5dd4cee5caef14cf6`，确认不含本地 `.env`、实际部署配置、虚拟环境或科研数据。临时包不是长期备份，可由上述提交重新生成。
- Chrome 原有阿里云页面显示河源实例 `i-f8z6yzr8vjyd0am69xph` 处于节省停机状态、没有公网 IP；刷新后跳转到阿里云登录页。旧页面信息尚不能作为实时状态依据。已请维护者重新登录。
- **尚未连接服务器、备份、迁移、上传或更新服务；部署未完成。** 登录恢复后先核实实例和原 `/opt/co-scientist`、`/data`，备份后再更新，保留服务器密钥与模型配置，不自动重跑旧任务。
- 当前版本含固定默认管理员密码，研究 API 没有完整用户权限校验；登录页不能等同于已具备公共网站的访问控制。此次未修改账号或凭据。
- 维护者随后完成阿里云重新登录。新标签页已显示账号及实例页面，页面被“ECS 专属安全防护权益”推广弹窗遮挡；没有开通推广服务。电脑控制接口多次报 `noWindowsAvailable`，按钮点击和键盘提交未产生预期页面变化，重连接口后仍未恢复。已请维护者保持 Mac 解锁、将 Chrome 置于前台并关闭弹窗。此时仍未连接或更新服务器，不能报告部署成功。

## 阿里云更新完成（2026-11-18，Asia/Shanghai）

本节取代上一节的未部署状态。维护者启动原实例后，实时控制台显示 `i-f8z6yzr8vjyd0am69xph` 运行正常，公网 IP 已变成 **47.115.224.185**。旧文档中的 `47.120.42.34` 不再作为当前访问地址。

- Workbench 免密 SSH 返回“建立远程连接失败”；改用实例“全部操作 → 发送命令”的云助手。只读预检成功：原服务 active、服务用户 `coscientist`、源码 `/opt/co-scientist` 约 680 MB、数据 `/data` 约 7.8 MB。没有修改 SSH、安全组、云账号凭据或开通推广服务。
- 固定提交的 GitHub codeload 归档 SHA256 为 `7e899ba294dd18dc3ca1c3b6dcbb8a1199470e8cc16658074a16b15ce00cf0cb`；本地逐一验证其 257 个文件与测试通过的 `2e943c8c7aeb5467a5112c3f2ab122bfc876e573` 一致。它与前文 `git archive` 的压缩包格式不同，校验值不能混用。
- 自动输入长脚本出现字符丢失，未执行损坏的编辑内容。准备了更新脚本（本机临时文件 `co-scientist-update-2e943c8.sh` / `.txt`，SHA256 `e9ad9c339567aae2b7525e1af8580b46cbbd5eb4520209726794bb25db2eaf6f`，`bash -n` 通过），维护者随后手动粘贴并执行；已读取其截图及云助手成功输出，不重复执行。
- 新代码位于 `/opt/co-scientist-releases/2e943c8c7aeb5467a5112c3f2ab122bfc876e573`。复用原 `.venv`，链接原服务器 `.env`，继续使用原 `config/deployment.toml` 与 `/data`。没有复制本地科研数据库或本地 Provider 密钥，没有启用本地多模型端点。
- `pip check` 通过；在独立数据库副本上迁移至 schema 13、校验 SQLite integrity 与行数保留，输出 `MIGRATION_DRY_RUN_OK`，副本计数为 sessions 4、hypotheses 10、tasks 64、reviews 10。这些计数来自迁移预演，不是生产迁移后的逐行复核。
- 停应用后备份数据与旧源码配置，存入服务器 `/var/backups/co-scientist-YYYYMMDD-HHMMSS/`，包含 `data.tar.gz`、`app.tar.gz`、原 systemd 单元及已有 drop-in（如有）、`SHA256SUMS`。此次未读到日志末尾的完整时间戳，因此不编造具体备份目录名。原源码目录和虚拟环境保留。
- 新增 `/etc/systemd/system/co-scientist.service.d/90-codex-20261118.conf`，只覆盖 `WorkingDirectory` 和 `PYTHONPATH` 指向新 release；原 ExecStart/ExecStartPre 继续使用原虚拟环境和部署配置。重启时首次健康探测曾 connection refused，重试后成功；最终 `nginx -t` 通过，应用与 Nginx 均 active，日志显示正确的 `DEPLOYED_COMMIT`。这条启动窗口的连接失败不代表最终部署失败。

19:50–19:51 公网实际验收：

- `GET /healthz`：HTTP 200，`{"ok":true}`。
- `GET /login`：HTTP 200，包含登录、注册表单；更新前该路径为 404。
- 未登录 `GET /`：HTTP 303 → `/login`。
- `GET /static/studio.js`：SHA256 `73681e35af3762f9166e27a57fc22b12ac70feaee31bf32bb4b530987f0c4f68`，与本地固定提交中的同一文件一致。
- `GET /.env`：HTTP 404。
- Chrome 已打开 `http://47.115.224.185/login`，实际看到“进入科研工作区”、登录与注册两个表单。

此次完成版本更新、数据库副本迁移验证及上线可达性验收，没有发起模型推理、创建付费科研任务或重跑旧任务，也未执行公网账号登录/注册的完整提交流程。服务器原模型配置保留，但这不等于已验收真实科研闭环。

当前依然是 HTTP 内测部署；源码中的固定默认管理员密码及研究 API 缺少完整用户权限校验的问题仍存在。本次没有将其描述为可面向公众使用的安全站点。后续维护以新 release 为源码入口，不能只修改仍保留的旧 `/opt/co-scientist/co_scientist` 后就认为线上代码已更新。

## 上线后用户任务预算与多端点排查（2026-11-18）

维护者提供 BudgetExceeded 截图并说明本机曾配置三个生成 API。只读检查其正在查看的线上 Session `ses_01M7Q91WTX9TXA6Y551W1HRG3E`：

- 持久化总预算为 100000 tokens / 2 USD，已用 99773 tokens，程序估算费用 0.0494525 USD；仅余 227 tokens。Generation 的下一轮请求单是输出预留就为 8192 tokens，因此本地 `TokenBudget.admit` 会拒绝，调用不会进入 OpenAI-compatible SDK 的 HTTP 请求。
- 实际保存了 15 条成功调用记录，模型全部为 `deepseek-flash`：parse_goal 1 次 / 2814 tokens，Generation 14 次 / 96959 tokens。没有 Kimi、GLM 调用记录；本次成功调用不能据此验证未使用的另外两把 Key。
- 该 Session 配置快照中 `generation.authors=[]`、`generation.endpoints={}`、`llm.endpoints` 为空，各模型路由都为 DeepSeek。本机不入库的配置则为 authors `kimi, glm, session`，命名模型为 `kimi-k3.1-pro`、`glm-5.3`。此前更新按保留原服务器配置执行，没有同步本机三端点设置或相关秘密。
- 错误提示存在软件问题：美元和 Token 两种上限共用同一条只显示美元的报错，造成“只用 $0.05 却提示 $2 耗尽”的误导；活动装饰器在预算准入前记录“请求模型”，不能把 BudgetExceeded 对应的请求事件当作已发到供应商。
- 调度器把 BudgetExceeded 当普通任务失败重试；最终 Generation 失败 8 个、0 假设，其他研究 Agent 未触发，却被 session_done / 界面标记为已完成。该状态只代表流程结束，不代表取得科研产出。

本轮是诊断，没有修改预算/配置/密钥/调度代码，没有重跑任务或发起模型请求。后续如要修复，应分别处理预算原因提示、预算不足时的重试/结束语义及服务器三端点配置，不能通过只更换 API Key 来解决该次 Token 准入失败。

## 本机正式服务（2026-11-18）

隔离预览 `7887` 会截获正式启动，因此在该页面新建的研究会一直保持「已暂停」。本机正式库原先停在 schema 8，打开带登录的正式服务时因缺少 `accounts` 表返回 500。

- 已停止当时的本机 `serve`，备份为 `data/backups/co_scientist-before-accounts-20261118-203336.db`（504K，迁移前会话为 1 条 `done`）。
- 已执行 `co-scientist init`，schema 现为 1–13；账号表当时为空，访问 `/login` 后才会写入默认管理员。没有重跑历史任务，也没有新建科研会话。
- 正式服务在 `http://127.0.0.1:7879`。`GET /login` 为 200，未登录 `GET /` 为 303 到 `/login`。在这个地址新建研究会调用真实模型并消耗额度。

## 共享文献阅读（2026-11-18）

正式启动在生成假设之前，由会话模型（本机是 DeepSeek）做一次文献检索和阅读。Kimi、GLM 和 DeepSeek 之后只根据这份笔记写假设，生成阶段不再各自调用 PubMed 或 Europe PMC。笔记按 [norman-paper-reading](https://github.com/N-tongxue/norman-skill) v1.4.0 的 quick 模式组织：研究问题、核心设计、关键结果、证据缺口和下一步核对，并保留该技能要求的署名。该技能把文献综述定为快速阅读，完整逐图精读只用于指定的单篇论文，因此这里没有把每篇都做成 deep 报告，也没有把仓库里的本地 PDF 可执行文件放进版本库。正文仍来自现有 `read_paper`。审查阶段可以先用这份笔记，只在笔记没有覆盖的论断上再检索。2026-11-18 21:08 已重启本机 `http://127.0.0.1:7879`。重启前正在跑的研究会随旧进程停止；当时库里的会话随后均为 `done`。这次重启后新建的研究才会使用共享阅读。

协作网络的红点原先只在「回放」里出现：运行中每 5 秒整页刷新会拆掉图，第一次载入又把已有交接记成历史。现在同一次研究的新交接会在运行中沿连线移动；打开页面时已有的交接仍不自动重放。

## 运行可靠性（2026-11-18）

维护者审查后要求先处理真实运行里的失败语义，线上登录保护暂缓。没有调用真实模型，没有改 `data/`。

- **预算不足不再当普通失败重试。** `BudgetExceeded` 立即作废该任务并结束会话（`StopReason.BUDGET`）。Token 上限和美元上限使用不同的报错；`budget_tokens=0` 或 `budget_usd=0` 与调度侧一致，表示该项不限额。结束时把 `termination.reason/output/detail` 写入 `config_snapshot`。界面把 0 条假设的 `done` 显示为「已结束（无产出）」，预算停机显示为「已结束（预算用尽）」。
- **命名生成端点默认 1 路并发。** `[llm.endpoints.<name>] max_concurrent` 默认 1，进程内共享信号量；0 表示不限制。会话 Provider 不受此项约束。
- **审查分数接受 `"0.5"` 这类字符串。** 仍拒绝布尔值和区间外的数。
- **网页进程可以接回研究。** `co-scientist serve` 启动时恢复 `status=running` 的会话。`POST /api/sessions/{id}/resume` 在当前进程没有 Supervisor 时会真正启动调度，而不是只改数据库。Resume 时若记录了 `n_initial` 却没有初始生成任务，会按幂等键补上；导入的会话没有该字段，不会凭空生成。

验证：`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q co_scientist/tests/unit` 为 **560 passed in 24.13s**。`pip check`、`git diff --check` 和 `node --check co_scientist/web/static/studio.js` 通过。本机正式服务需要重启后才会加载这些改动；已经标记为 `done` 的历史会话不会被自动接回。

未做：公网登录保护与 HTTPS、研究者反馈闭环、Meta-review 触发门槛、安全分类器、共享文献失败回退、固定目标的效果核对。Kimi `tool_choice`/`thinking` 的未提交修复仍未做真实调用验证。

## 研究者反馈、Meta-review 触发与安全分类（2026-11-18）

维护者要求继续论文对照的第三项。没有调用真实模型，没有改 `data/`。第一项（公网登录/HTTPS）仍由维护者部署时处理。

- **Agent 读取研究者反馈。** `system_feedback` 里生效的 human `directive` / `preference`，以及带文字的 pin / rejection，会经 `compose_prompt` 写入 Generation、Reflection、Ranking、Evolution 和 Meta-review 提示。不再只看最新一条 meta-review。没有文字的 pin/reject 仍只改假设状态。`feedback_driven` 在只有研究者指令、还没有系统反馈时也会走该策略。
- **标准模式 Meta-review 不再卡 50 场比赛。** `[metareview] feedback_every_matches` 默认 10；第一次可在 `min(every, max(2, 锦标赛池大小))` 场后触发，短局也能出系统反馈。`0` 关闭周期性反馈。轻量模式原有门槛未改。预估费用按同一间隔计算。
- **安全分类器接到运行路径。** 研究目标在 parse_goal 之前分类；`block` 把 Session 标为 `failed` 并停止。假设在写入前分类：`block` 不入库，`quarantine` 写入 `quarantined` 且不进入审查。分类器关闭或后端失败时放行，不中断离线测试。API 后端从 `record_safety_assessment` 工具参数读取结果，CLI 后端仍读 `capture`。离线测试默认 `enable_classifier=False`，避免 RecordingLLM 多一次分类调用。

验证：同一条单元测试命令为 **571 passed in 20.99s**。`pip check` 和 `git diff --check` 通过。没有调用真实模型，没有改 `data/`。

未做：公网登录保护与 HTTPS、最终报告发布前的可选分类、共享文献失败回退、固定目标的效果核对。分类器的真实模型效果未验证。Kimi `tool_choice`/`thinking` 的未提交修复仍未做真实调用验证。

## 共享文献回退、最终报告分类与效果核对清单（2026-11-18）

维护者要求继续处理审查清单里剩余三项。没有调用真实模型，没有改 `data/`。公网登录/HTTPS 仍由维护者部署时处理。

- **共享文献失败时退回各自检索。** 共享阅读未完成、没有论文，或没有笔记文件时，生成任务重新获得检索工具，不再被要求空引文硬写。成功的笔记对 literature/debate/assumption 仍是唯一文献来源，避免再次打爆检索站；`feedback_driven` 可以补搜笔记没覆盖的方向。共享阅读本身在有密钥时也会使用 `web_search`。`[literature] exclusive=true` 可改成 false，让所有策略都把共享笔记当补充。
- **最终报告可选安全分类。** 在分类器开启且 `classify_final_overview=true` 时，总览草稿会再分类一次。`block` 不发布原稿，只留下说明；会话仍结束。`warn`/`quarantine` 发布并附标记。被隔离或否决的假设不再进入总览候选。离线测试默认关闭分类器。
- **固定目标的效果核对清单。** `co-scientist scorecard SESSION_ID` 从已保存会话读取假设数、比赛数、完成的演化、系统反馈、后半段 Elo 是否高于前半段、结束原因，并留出 `human_score`。固定目标是「比较两种测量误差的可能来源」。Elo 仍不是实验结果。本轮没有按该协议发起付费运行。

验证：同一条单元测试命令为 **577 passed in 21.79s**。`pip check` 和 `git diff --check` 通过。

未做：公网登录保护与 HTTPS、按该协议用真实模型重复跑固定目标、分类器的真实模型效果。Kimi `tool_choice`/`thinking` 的未提交修复仍未做真实调用验证。

## 固定目标协议实跑（2026-11-19）

维护者明确要求按该协议用真实模型重复跑固定目标。没有改正式 `data/`，没有部署，没有用 Kimi/GLM 作者（三次之间只用会话 DeepSeek `deepseek-flash`，便于比较）。命令是：

```sh
.venv/bin/co-scientist protocol-run --data-dir docs/validation/quality-protocol-20261119
```

协议：目标「比较两种测量误差的可能来源」，轻量模式，3 条初始假设，2 美元 / 100000 tokens，时限 600 秒，重复 3 次。隔离库不入库。嵌入为本地 hash。

三次都在 Token 上限停机，美元只用了约 0.03–0.06。共享文献阅读记在 supervisor 名下，单次约 4–9 万 tokens，把 10 万上限几乎用完，锦标赛、演化和 Meta-review 都没有跑起来。

| 次序 | Session | 假设 | 比赛 | 演化完成 | 系统反馈 | 后半段 Elo 是否更高 | 结束 | tokens / USD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `ses_01M7S5XG238J5PB23Y686Z59HD` | 1（draft） | 0 | 0 | 0 | 无法判断 | 预算 | 90789 / 0.044 |
| 2 | `ses_01M7S611Q1X6R7VSNFRAMRVB7D` | 0 | 0 | 0 | 0 | 无法判断 | 预算，无产出 | 92278 / 0.034 |
| 3 | `ses_01M7S645YTRDZ4WQKQP4Q655B6` | 1（reviewed） | 0 | 0 | 0 | 无法判断 | 预算 | 97595 / 0.061 |

`human_score` 仍为空：没有专家评分。Elo 仍不是实验结果。这次只能说明：在当前共享阅读 + 100000 token 上限下，固定目标跑不完比较循环。若要看后出现的假设是否压过早期假设，需要另开更高 token 上限或缩短共享阅读，不能把这次三次 `done` 当成科研效果通过。

离线单元测试随后为 **578 passed in 26.27s**。`git diff --check` 通过。

未做：公网登录保护与 HTTPS、在足够预算下重跑该目标、分类器的真实模型效果、人工评分。Kimi `tool_choice`/`thinking` 仍未做真实调用验证。

## 管理员 API 管理（2026-11-19）

维护者要求在管理员页增加 API 管理，方便后期更换或新增 API Key 和模型。没有调用真实模型，没有改正式 `data/`。

- 管理员打开 `/admin` 可以看到密钥、会话 Provider/模型名、生成端点。普通用户仍只看到“只对管理员开放”。工作区侧栏对管理员多一个「管理」入口。
- 密钥只写入 `data_dir/runtime.env`（权限 600），页面只显示是否已配置和末尾几位。模型、接口地址和作者写入 `data_dir/runtime.toml`，不含密钥。不会把本机 `.env` 整份拷进数据目录。
- 进程内改的是同一个 Config 对象，新建研究会用新设置；已经在跑的研究会继续用启动时的配置。`load_config` 会叠加这两份 runtime 文件，重启后仍然有效。
- 端点名、环境变量名、http(s) 地址会校验；作者必须是已有端点或 `session`。正在当作者的端点不能直接删除。

验证：`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q co_scientist/tests/unit` 为 **581 passed in 25.21s**。`pip check` 和 `git diff --check` 通过。隔离预览 `http://127.0.0.1:7888` 用管理员 `rys` 登录后，能保存一把测试检索密钥（页面显示 `sk…cdef`，不回显全文），并新增生成端点 `demo` / `demo-flash`。预览已停，测试密钥只写在系统临时目录，没有写入正式 `data/`。2026-11-19 14:28 已重启本机正式服务 `http://127.0.0.1:7879`，该进程会加载管理员 API 管理页；在这个地址新建研究会调用真实模型。

未做：公网登录保护与 HTTPS、在足够预算下重跑固定目标、分类器的真实模型效果。已在运行的 Supervisor 不会热切换新钥匙。
