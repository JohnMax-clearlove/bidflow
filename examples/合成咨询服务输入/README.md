# 合成咨询服务输入

本目录只含虚构测试数据，不对应任何真实采购人、公司、人员、合同或项目。它用于熟悉 BidFlow 的导入、台账和任务包流程，不能用于真实投标。

包含：

- `招标文件/合成招标文件.md`：100 分制的明标咨询服务招标要求；
- `公司资料/合成业绩台账.csv`：含稳定人员ID和角色的候选台账；
- `公司资料/合成方法论.md`：只能作为技术策划参考的方法论。

在仓库根目录运行以下命令。示例数据统一写到仓库外的临时目录，避免测试产物混入仓库：

```powershell
$DATA = "$env:TEMP\bidflow-合成示例"
.\.venv\Scripts\bidflow.exe library "$DATA\company_library"
.\.venv\Scripts\bidflow.exe ledger ".\examples\合成咨询服务输入\公司资料\合成业绩台账.csv" --project "$DATA\company_library"
.\.venv\Scripts\bidflow.exe ingest ".\examples\合成咨询服务输入\公司资料\合成方法论.md" --category 方法论 --project "$DATA\company_library"

.\.venv\Scripts\bidflow.exe init "合成咨询服务投标" --path "$DATA\合成咨询服务投标" --library "$DATA\company_library"
.\.venv\Scripts\bidflow.exe ingest ".\examples\合成咨询服务输入\招标文件\合成招标文件.md" --category 招标文件 --project "$DATA\合成咨询服务投标"
.\.venv\Scripts\bidflow.exe ledger ".\examples\合成咨询服务输入\公司资料\合成业绩台账.csv" --project "$DATA\合成咨询服务投标"
.\.venv\Scripts\bidflow.exe ingest ".\examples\合成咨询服务输入\公司资料\合成方法论.md" --category 方法论 --project "$DATA\合成咨询服务投标"
.\.venv\Scripts\bidflow.exe task prepare analyze --project "$DATA\合成咨询服务投标"
```

最后一条命令只生成拆标任务包。接下来在任意 Agent 中打开 `$env:TEMP\bidflow-合成示例\合成咨询服务投标`，说：

> 请处理全部 pending 的 analyze 任务。逐个读取 context.json，按 result_schema 生成 result.json 并用实际执行者标识接收。完成后生成拆标报告，先让我核对规则。

不要把示例台账当成证明材料。示例没有合同、证书或招标表单，所以 `match`、`audit` 和最终组卷应如实显示缺件；出现这些缺口说明保守校验正在生效。

