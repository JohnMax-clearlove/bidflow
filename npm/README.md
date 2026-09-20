# bidflow（npm 包装器）

> **状态：尚未发布到 npm（计划中）。** 发布前，仓库根 README 的一键/curl 渠道是可用安装方式；包与文档已就绪，发布后即生效。

本目录是 BidFlow 的 npm 安装引导包，**不包含业务逻辑**：核心是 GitHub 仓库
`JohnMax-clearlove/bidflow` 中的本地 Python 程序 `bidflow-local`（本地优先的
Agent 投标文件编制工作流）。npm 包只做两件事：

1. 未安装时，下载并执行官方安装脚本（Windows 为 `install.ps1`，Linux/macOS 为
   `install.sh`；默认取 `main`，可用 `--ref`/`BIDFLOW_REF` 指定标签或提交，
   安装器再把引用解析为固定提交归档），用 uv 管理隔离的 Python 与依赖；
2. 已安装时，把命令转发给本机 BidFlow 入口。

## 使用

```powershell
# Windows：安装后直接使用（也可全程只用 npx）
npm install -g bidflow
bidflow doctor

# Linux/macOS
npm install -g bidflow
bidflow doctor
```

不全局安装也可以：

```bash
npx bidflow@latest doctor
```

安装管理参数（只执行安装、更新、回滚或卸载）：

```powershell
bidflow --with-ocr              # 安装/更新并附带扫描件 OCR 依赖
bidflow --no-ocr                # 明确不安装 OCR
bidflow --no-path               # 不修改用户 PATH
bidflow --rollback              # 回滚上一版本
bidflow --uninstall             # 卸载
bidflow --ref v0.2.0            # 安装指定标签/提交
bidflow --install-root D:\BidFlow
```

- Windows 默认安装根目录 `%LOCALAPPDATA%\BidFlow`；Linux/macOS 默认 `~/.bidflow`。
- 可用环境变量：`BIDFLOW_REF`（默认安装提交）、`BIDFLOW_INSTALL_ROOT`（安装根目录）。
- Windows 下包装器显式按 UTF-8 读取安装脚本后执行，兼容 PowerShell 5.1 与 PowerShell 7
  （5.1 的 `-File` 会按本地代码页误读中文，与 `docs/安装与更新.md` 的约定一致）。
- 安装器不会要求管理员权限，不配置模型 API，不自动签章，不提交投标；
  完整 Word 分页与链接验收仍需 Windows 桌面版 Microsoft Word。

## 发布

- 版本号与仓库根 `pyproject.toml` 的 `version` 保持同步（当前 `0.2.0`）。
- 发布命令：`cd npm && npm publish`（首次发布需 npm 账号与发布授权）。
- 包内容仅 `bin/bidflow.js` 与 `README.md`；不含任何真实资料、公司数据或密钥。
