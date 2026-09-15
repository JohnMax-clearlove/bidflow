# BidFlow

BidFlow 是一套本地优先的咨询服务投标工作流。程序保管原件、拆分文件、检索资料、校验版本、测算证据支持分并生成 Word/PDF；需要理解招标语义、策划和写作的工作由当前 Codex Agent 完成。它不需要单独配置模型 API。

当前版本为 `0.1.0`，状态是**待实标验收**。可以用公开合成资料验证流程，但在至少一个真实咨询服务项目完成逐项核对前，不能把测试通过理解为真实投标质量已经验收。

## 文件夹怎么放

一个总文件夹保存程序和公司公共资料，每次投标建立一个独立项目小文件夹：

```text
Bid Document Preparation/          总文件夹
├─ src、docs、schemas、tests       程序、说明和数据规范
├─ company_library/                公司公共资料库，长期维护
│  └─ 01_输入文件/
│     ├─ 02_公司证照 … 07_人员业绩证明
│     ├─ 09_方法论
│     ├─ 10_历史章节
│     └─ 11_业绩台账
└─ projects/
   ├─ A项目投标/                   一个项目一个文件夹
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

日常只需在 Codex 中打开当前项目小文件夹。迁移或归档项目时复制整个项目文件夹；`.bidflow/records`、`.bidflow/tasks` 和 `.bidflow/history` 都要保留，只有 `.bidflow/cache` 可以重建。

## 安装

需要 Python 3.12。以下命令在总文件夹的 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\bidflow.exe doctor
```

扫描 PDF 或图片需要本地 OCR 时，再安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ocr]"
```

BidFlow 不会联网下载 OCR 模型。`doctor` 会报告 Python、解析组件、Pandoc、Microsoft Word 和 OCR 的实际可用状态。没有 Pandoc 仍可保留 DOCX 结构定位；没有桌面版 Word 仍可生成未分页 DOCX，但不能完成最终页码和链接验收。

## 第一次使用

先在总文件夹初始化公司公共资料库：

```powershell
.\.venv\Scripts\bidflow.exe library ".\company_library"
```

把证照、资质、人员证书、业绩证明、方法论和业绩台账放入相应分类目录，再导入：

```powershell
.\.venv\Scripts\bidflow.exe ingest --scan --project ".\company_library"
```

接到新项目后，从总文件夹创建项目：

```powershell
.\.venv\Scripts\bidflow.exe init "A项目投标" --path ".\projects\A项目投标" --library ".\company_library"
```

然后在 Codex 中打开 `projects/A项目投标`。把招标文件放入 `01_输入文件/01_招标文件`，直接对 Agent 说：

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

确认只绑定当时的文件和主记录版本。补遗、证据更新、事实变更或人工改稿会使有关结果重新待核。

| 确认范围 | 什么时候确认 | 命令 |
|---|---|---|
| `rules` | 所有招标分块已分析，规则、原文来源、冲突和提取事实已核对 | `bidflow confirm rules --actor 确认人 --project .` |
| `selection` | 人员、业绩和证明材料选择已核对 | `bidflow confirm selection --actor 确认人 --project .` |
| `brief` | 新正式文本已完成至少 5 个实质性问题及正文边界确认 | `bidflow confirm brief --actor 确认人 --project .` |
| `plan` | 技术评分项全部进入策划，章节主线和范围已确认 | `bidflow confirm plan --actor 确认人 --project .` |
| `draft` | 用户已经修改并确认当前 Markdown | `bidflow confirm draft --actor 确认人 --project .` |
| `assembly` | 审阅组卷的内容和输入范围已确认 | `bidflow confirm assembly --actor 确认人 --project .` |
| `visual` | 实际查看 Word/PDF，页码、表格、附件和跳转均无误 | `bidflow confirm visual --actor 确认人 --project .` |

## 组卷边界

- `bidflow build --mode review --project .` 生成审阅组卷；加 `--split` 生成分册。
- `bidflow verify --project .` 核验实际 PDF 页数、书签、索引跳转和文件哈希。成功导出仍需人工视觉检查。
- 完成 `assembly` 和 `visual` 确认后，`bidflow build --mode ready --project .` 才会把原字节复制到 `07_最终输出`，状态为“待签章”。
- 暗标会被明确阻止，首版不编制暗标。
- 签字盖章、授权有效性、保证金、上传和递交由人工完成，程序不签章、不提交投标。
- 报价由用户确定；商务证据分、技术模拟分和报价评分分开显示，不承诺评委最终得分。

## 异常恢复和隐私

- 随时运行 `bidflow status --project .` 和 `bidflow next --project .`，换一个 Codex 对话也能从项目记录继续。
- 索引损坏可运行 `bidflow reindex --project .`；派生报告可运行 `bidflow reports --project .` 重建。
- 只有确认原写入进程已经终止后，才按报错中的旧进程号运行 `bidflow recover --pid 旧进程号 --project .`。仍在运行的写入不会被抢占。
- 所有输入、解析缓存、主记录和成品默认留在本机。钉钉、飞书和合同系统接口当前只生成“需人工检索”提示；下载后的资料要放回项目再导入。
- `projects/`、`company_library/`、缓存和真实成果均不应提交公开 Git。仓库中的 [`examples`](examples/合成咨询服务输入/README.md) 全部为虚构测试数据。

## 进一步说明

- [文件导入、OCR、检索和台账](docs/文件导入.md)
- [证据核验、计分和人员配置](docs/证据与计分.md)
- [商务表单、组卷和输出核验](docs/组卷与表单.md)
- [任务包、版本状态和恢复](docs/架构与状态.md)
- [程序数据接口](docs/接口约定.md)
- [首版完成与验收记录](docs/首版完成与验收.md)
