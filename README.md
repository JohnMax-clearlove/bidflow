# BidFlow

BidFlow 是一套本地优先的咨询服务投标工作流。程序保管原件、拆分文件、检索资料、校验版本、测算证据支持分并生成 Word/PDF。语义任务由你正在使用的宿主 Agent 完成，主 Agent 负责分工与验收，人工完成必要确认。Python 程序本身不调用模型 API。

BidFlow 与具体 Agent 无关：Codex、Claude Code、Qoder、Trae、WorkBuddy、千问办公等任何能在项目目录运行命令的 Agent 都可以驱动；换一个 Agent 或换一次对话，从项目文件继续。

当前版本为 `0.2.0`，状态是**待实标验收**。新版增加组卷后的人工与 Agent 独立双审，支持外部人工完成的 PDF。软件测试通过不等于真实投标项目已经全流程验收。

从旧版继续项目时，原件和历史记录保留；缺少人工声明或版本依据的旧确认、证据覆盖和审核关闭记录会要求补核，不会直接沿用“已通过”。详见[成品双审](docs/成品双审.md)。

## 文件夹怎么放

**程序与资料分开**：安装器把程序装到系统位置（Windows 默认 `%LOCALAPPDATA%\BidFlow`，Linux/macOS 默认 `~/.bidflow`），与投标资料互不相干——升级或卸载程序都不会动你的资料。

资料放哪里由你决定：公司公共资料库建一处长期维护，每次投标建一个独立项目文件夹（位置都可以自定）。建议集中放在一个数据目录下，例如：

```text
D:\投标数据/                       数据目录（名称与位置自定）
├─ company_library/                公司公共资料库，长期维护
│  └─ 01_输入文件/
│     ├─ 02_公司证照 … 07_人员业绩证明
│     ├─ 09_方法论
│     ├─ 10_历史章节
│     └─ 11_业绩台账
├─ A项目投标/                      一个项目一个文件夹
└─ B项目投标/
```

项目小文件夹内固定为：

```text
A项目投标/
├─ AGENTS.md                       本项目完整工作规则
├─ 项目状态.md                     人可以直接阅读的当前状态
├─ 01_输入文件/                    原始输入，按八类存放
├─ 02_招标拆解/
├─ 03_资料匹配/
├─ 04_技术标策划/
├─ 05_投标文件编制/                Markdown 主稿和商务表单
├─ 06_审核检查/                    报告和组卷预览
├─ 07_最终输出/                    已确认的待签章成果
└─ .bidflow/                       主记录、任务、历史和缓存
```

日常只需在任意 Agent 中打开当前项目小文件夹（Codex、Claude Code、Qoder、Trae、WorkBuddy、千问办公等均可）。迁移或归档项目时复制整个项目文件夹；`.bidflow/records`、`.bidflow/tasks` 和 `.bidflow/history` 都要保留，只有 `.bidflow/cache` 可以重建。

## 安装

不需要管理员权限。以下方式任选其一，效果相同：都以固定 commit 从仓库安装 `bidflow-local`，用 uv 管理隔离的 Python 与依赖，Windows 默认安装到 `%LOCALAPPDATA%\BidFlow`，与投标项目、公司资料彻底分开。

### 方式一：PowerShell 一键（Windows）

在 PowerShell 中执行：

```powershell
irm https://raw.githubusercontent.com/JohnMax-clearlove/bidflow/main/install.ps1 | iex
```

安装器用 uv 管理隔离的 Python 与依赖，默认安装到 `%LOCALAPPDATA%\BidFlow`，与投标项目、公司资料彻底分开；默认把 `main` 解析为完整 commit SHA 后从该 SHA 的归档安装，并在新入口的 `bidflow doctor` 检查通过后才切换稳定入口。不放心直接执行远程脚本时，可先下载查看再运行：

```powershell
irm https://raw.githubusercontent.com/JohnMax-clearlove/bidflow/main/install.ps1 -OutFile "$env:TEMP\bidflow-install.ps1"
Get-Content "$env:TEMP\bidflow-install.ps1" | more
$code = Get-Content -Raw -Encoding UTF8 "$env:TEMP\bidflow-install.ps1"
& ([scriptblock]::Create($code))
```

脚本按 UTF-8 无 BOM 保存，以便 `irm | iex` 在任何 PowerShell 中正确读取；Windows PowerShell 5.1 的 `-File` 参数会按本地代码页误读中文，所以本地执行时显式按 UTF-8 读取，PowerShell 7 下同样可用。

常用参数：`-WithOcr` 安装扫描件 OCR 依赖，`-NoOcr` 明确不装并覆盖上次选择，`-NoPath` 不改用户 PATH，`-Rollback` 回滚上一版本，`-Uninstall` 卸载。更新就是重新运行安装命令；安装、回滚、卸载与安全边界的完整说明见[安装与更新](docs/安装与更新.md)。

BidFlow 不会联网下载 OCR 模型。`doctor` 会报告 Python、解析组件、Pandoc、Microsoft Word 和 OCR 的实际可用状态。没有 Pandoc 仍可保留 DOCX 结构定位；没有桌面版 Word 仍可生成未分页 DOCX，但不能完成最终页码和链接验收。

### 方式二：npm（需要 Node.js 18+；计划中，尚未发布）

> **该渠道尚未发布到 npm**：`npm install -g bidflow` 目前会返回 404。请先使用方式一或方式三安装；包与文档已就绪，发布后本节恢复可用。

npm 包只是安装引导与命令转发，核心仍是同一套本地 Python 程序：

```powershell
npm install -g bidflow
bidflow doctor
```

也可以不全局安装，直接 `npx bidflow doctor`。本机未安装时，包装器会先自动执行安装再转发命令；`bidflow --with-ocr`、`--no-ocr`、`--no-path`、`--rollback`、`--uninstall`、`--ref 提交`、`--install-root 路径` 与安装脚本参数一一对应，`bidflow --help` 查看说明。

### 方式三：curl

Windows 上习惯 curl 时，下载后按方式一同样以 UTF-8 读取运行：

```powershell
curl.exe -fsSL https://raw.githubusercontent.com/JohnMax-clearlove/bidflow/main/install.ps1 -o "$env:TEMP\bidflow-install.ps1"
$code = Get-Content -Raw -Encoding UTF8 "$env:TEMP\bidflow-install.ps1"
& ([scriptblock]::Create($code))
```

Linux/macOS 提供 bash 安装器（可生成与检查文档，但完整 Word 分页与链接验收仍需 Windows 桌面版 Word）：

```bash
curl -fsSL https://raw.githubusercontent.com/JohnMax-clearlove/bidflow/main/install.sh | bash
```

安装渠道的细节（参数、安装位置、更新、回滚、卸载与安全边界）见[安装与更新](docs/安装与更新.md)。

### 开发者从源码安装

需要在仓库根目录用 Python 3.12 建立虚拟环境（程序目录，不要放投标资料）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\bidflow.exe doctor
```

扫描 PDF 或图片需要本地 OCR 时，再安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ocr]"
```

下文命令中的 `bidflow` 在源码安装方式下等价于 `.\.venv\Scripts\bidflow.exe`。

## 第一次使用

安装后请新开一个终端或重新打开 Agent，再初始化公司公共资料库：

```powershell
bidflow library "D:\投标数据\company_library"
```

把证照、资质、人员证书、业绩证明、方法论和业绩台账放入相应分类目录，再导入：

```powershell
bidflow ingest --scan --project "D:\投标数据\company_library"
```

接到新项目后，创建项目文件夹（位置自定，示例放在数据目录下）：

```powershell
bidflow init "A项目投标" --path "D:\投标数据\A项目投标" --library "D:\投标数据\company_library"
```

然后在任意 Agent 中打开该项目文件夹。把招标文件放入 `01_输入文件/01_招标文件`，直接对 Agent 说：

> 请按本项目 AGENTS.md 工作。先运行 BidFlow 的 status、next 和 ingest --scan，处理全部 analyze 任务；每个任务读取 context.json，按 result_schema 生成 result.json 并接收。先给我核对招标规则，不要直接开始写正文。

后续可以继续说：

> 规则确认后，检索当前项目和公司资料库，核验证明页，分别列出台账线索、材料证明事实和本次招标适用性；再给出人员方案和缺件清单。

> 形成技术评分响应矩阵和章节策划。正式正文按项目规则先完成沟通记录，等我确认策划后再逐章写 Markdown。

> 独立审核每一章，处理不超过两轮。然后检查表单、生成审阅组卷并核验 Word/PDF；我确认内容和视觉效果后再生成待签章文件。

Agent 在每个阶段会调用真实命令。你也可以在项目目录激活环境后复制执行：

```powershell
bidflow status --project .
bidflow next --project .
bidflow ingest --scan --project .
bidflow task prepare analyze --project .
bidflow match --project .
bidflow score --project .
bidflow reports --project .
bidflow audit --project .
```

`task prepare` 只生成任务包，不会假装已经理解招标文件。Agent 必须读取 `.bidflow/tasks/TASK编号/context.json`，按其中的 `result_schema` 写入同目录 `result.json`，再运行：

```powershell
bidflow task accept --project . --task TASK编号 --result .bidflow/tasks/TASK编号/result.json --actor 执行者标识
```

一次拆标可能产生多个任务；全部招标分块处理完后，规则才允许确认。任务输入发生变化时，旧结果会被拒绝；正文已被人工修改时，Agent 稿会另存为候选稿和差异文件，不覆盖人工稿。

## 必须经过的确认

确认只绑定当时的文件和主记录版本。补遗、证据更新、事实变更或人工改稿会使有关结果重新待核。所有 `confirm` 都必须带 `--attest-human`，表示由主 Agent 在用户明确确认后录入；这是流程声明，不是身份认证。没有 `actor_kind/attestation` 的旧确认记录不再视为满足当前门槛，需重新确认。

| 确认范围 | 什么时候确认 | 命令 |
|---|---|---|
| `rules` | 所有招标分块已分析，规则、原文来源、冲突和提取事实已核对 | `bidflow confirm rules --actor 确认人 --attest-human --project .` |
| `selection` | 人员、业绩和证明材料选择已核对 | `bidflow confirm selection --actor 确认人 --attest-human --project .` |
| `brief` | 新正式文本已完成至少 5 个实质性问题及正文边界确认 | `bidflow confirm brief --actor 确认人 --attest-human --project .` |
| `plan` | 技术评分项全部进入策划，章节主线和范围已确认 | `bidflow confirm plan --actor 确认人 --attest-human --project .` |
| `draft` | 用户已经修改并确认当前 Markdown | `bidflow confirm draft --actor 确认人 --attest-human --project .` |
| `assembly` | 审阅组卷的内容和输入范围已确认 | `bidflow confirm assembly --actor 确认人 --attest-human --project .` |
| `visual` | 实际查看 Word/PDF，页码、表格、附件和跳转均无误 | `bidflow confirm visual --actor 确认人 --attest-human --project .` |

## 组卷边界

- `bidflow build --mode review --project .` 生成审阅组卷；加 `--split` 生成分册。
- `bidflow verify --project .` 核验实际 PDF 页数、书签、索引跳转和文件哈希。成功导出仍需人工视觉检查。
- 完成 `assembly` 和 `visual` 确认还不够：当前组卷成品还必须通过人工与 Agent 并行的内容双审，`bidflow build --mode ready --project .` 才会把原字节复制到 `07_最终输出`，状态为“待签章”。
- 暗标会被明确阻止，首版不编制暗标。
- 签字盖章、授权有效性、保证金、上传和递交由人工完成，程序不签章、不提交投标。
- 报价由用户确定；商务证据分、技术模拟分和报价评分分开显示，不承诺评委最终得分。

## 成品双审（人工 + Agent）

组卷完成后，可以直接对 Agent 说：

> 我组卷好了，请对这份待签章PDF做成品双审。

也可以说：

> 这是外部人工排版好的待签章文件，请先锁定这份 PDF，再让我和一个独立 Agent 分别独立核对，最后汇总问题清单。

流程固定为：

1. **锁定成品**：`bidflow final-review start --stage content --writer 编制者 --pdf 待签章.pdf`（或 `--assembly` 引用本程序组卷成品）。程序复制原件并记录哈希、页数和规则/证据版本，不改原件。
2. **两条 lane 独立准备**：`prepare --lane human` 生成给用户看的人工核查清单，`prepare --lane agent` 生成给独立 Agent 的任务包；两边不互相提供初稿或评分。
3. **用户核对**：按清单逐项查看原页，填写结论和理由。
4. **独立 Agent 核对**：该 Agent 只依据锁定成品、规则原文和项目事实逐项核查。
5. **主 Agent 汇总**：`finalize` 保留双方原始结论与分歧，列出所有未解决阻断项；需要修改时由主 Agent 说明并安排补证或改稿。
6. **改版再核**：内容或证据改动后重新发起双审，旧通过失效；同一版本纠正误报时由该 lane 交新 revision 并保留原记录。
7. **签章后再核**：签章 PDF 作为新快照，用 `--stage signed --parent 原内容双审ID` 重新双审，逐页对比签章版与内容版。
8. **人工递交前确认**：装订、副本、介质（如 2 套 U 盘）、密封、递交地点时间等逐项确认，缺一仍 pending。

常用命令：

```powershell
bidflow final-review start --project . --stage content --writer 编制者标识 --assembly
bidflow final-review prepare --project . FR001 --lane agent
bidflow final-review prepare --project . FR001 --lane human
bidflow final-review submit --project . FR001 --lane agent --result "06_审核检查/成品双审/FR001/agent/B01/result.json" --actor 核查Agent
bidflow final-review submit --project . FR001 --lane human --result "06_审核检查/成品双审/FR001/human/结果模板.json" --actor 人工复核人 --attest-human
bidflow final-review finalize --project . FR001
bidflow final-review status --project . FR001
```

程序不替代人工签章、不代为提交，也不承诺“保证不废标”“万无一失”。详细协议见 [成品双审](docs/成品双审.md)。

## 异常恢复和隐私

- 随时运行 `bidflow status --project .` 和 `bidflow next --project .`，换一个 Agent 对话（或换一个 Agent）也能从项目记录继续。
- 索引损坏可运行 `bidflow reindex --project .`；派生报告可运行 `bidflow reports --project .` 重建。
- 只有确认原写入进程已经终止后，才按报错中的旧进程号运行 `bidflow recover --pid 旧进程号 --project .`。仍在运行的写入不会被抢占。
- 所有输入、解析缓存、主记录和成品默认留在本机。钉钉、飞书和合同系统接口当前只生成“需人工检索”提示；下载后的资料要放回项目再导入。
- 投标资料、公司资料和项目成果不要提交到任何公开仓库；本仓库中的 [`examples`](examples/合成咨询服务输入/README.md) 全部为虚构测试数据。

## 开发验证与提交

所有 Agent 修改本仓库后，按 `AGENTS.md` 检查差异、运行相关测试并提交到本地 Git；推送 GitHub 需要用户授权。真实资料和临时产物不提交。

常规测试运行 `python -m pytest`。Windows 受限环境若无法创建默认临时目录，运行 `python tests/run_tests.py -rs`；测试产物保存在已忽略的 `.bidflow-pytest-tmp/`。跳过项必须单独检查，不能按通过计数。

本机安装 Microsoft Word 后，显式运行实际分页和组卷验证：

```powershell
$env:BIDFLOW_WORD_TEST = '1'
$env:BIDFLOW_TEST_WORD = '1'
python tests/run_tests.py -rs
```

Word 自动化应在能正常使用桌面 Word 的 Windows 环境运行。沙箱阻止 COM 自动化时，须在允许的正常环境补验，不能以基础测试通过替代 Word/PDF 验证。

## 进一步说明

- [安装与更新（一键/npm/curl 安装、更新、回滚与卸载）](docs/安装与更新.md)
- [文件导入、OCR、检索和台账](docs/文件导入.md)
- [证据核验、计分和人员配置](docs/证据与计分.md)
- [商务表单、组卷和输出核验](docs/组卷与表单.md)
- [成品双审：人工与 Agent 独立核查](docs/成品双审.md)
- [任务包、版本状态和恢复](docs/架构与状态.md)
- [程序数据接口](docs/接口约定.md)
- [首版完成与验收记录](docs/首版完成与验收.md)
