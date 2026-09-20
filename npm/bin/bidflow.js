#!/usr/bin/env node
"use strict";

/*
 * BidFlow npm 包装器：安装引导与命令转发。
 * - 已安装时把参数原样转发给本机 BidFlow 入口（读取安装根目录的 state.json）；
 * - 未安装时先从 GitHub 固定提交下载官方安装脚本执行，再转发命令；
 * - 带 --with-ocr / --no-ocr / --no-path / --rollback / --uninstall / --ref / --install-root
 *   时只执行安装、更新、回滚或卸载。
 * npm 包本身不含业务逻辑；核心是本机 Python 程序 bidflow-local，不联网调用模型。
 */

const { spawnSync } = require("child_process");
const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");

const REPO_RAW = "https://raw.githubusercontent.com/JohnMax-clearlove/bidflow";
const DEFAULT_REF = process.env.BIDFLOW_REF || "main";
const IS_WINDOWS = process.platform === "win32";
const INSTALL_SCRIPT = IS_WINDOWS ? "install.ps1" : "install.sh";

const INSTALL_FLAGS = ["--with-ocr", "--no-ocr", "--no-path", "--rollback", "--uninstall", "--ref", "--install-root"];

function info(message) {
  console.log(`[bidflow] ${message}`);
}

function fail(message) {
  console.error(`[bidflow 安装失败] ${message}`);
}

function defaultRoot() {
  if (process.env.BIDFLOW_INSTALL_ROOT) {
    return process.env.BIDFLOW_INSTALL_ROOT;
  }
  if (IS_WINDOWS) {
    const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    return path.join(localAppData, "BidFlow");
  }
  return path.join(os.homedir(), ".bidflow");
}

function readState(root) {
  try {
    return JSON.parse(fs.readFileSync(path.join(root, "state.json"), "utf8"));
  } catch {
    return null;
  }
}

// 安装入口优先级：state.json 记录的 release 入口 → 安装根 bin 下的稳定入口 → PATH 上的 bidflow。
function findEntry(root) {
  const state = readState(root);
  if (state && state.current && state.current.entry) {
    const candidate = path.join(root, state.current.entry);
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  const stableName = IS_WINDOWS ? "bidflow.cmd" : "bidflow";
  const stable = path.join(root, "bin", stableName);
  if (fs.existsSync(stable)) {
    return stable;
  }
  const pathNames = IS_WINDOWS ? ["bidflow.exe"] : ["bidflow"];
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) {
      continue;
    }
    for (const name of pathNames) {
      const candidate = path.join(dir, name);
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    }
  }
  return null;
}

// cmd.exe 参数引号包裹：仅处理空格与双引号，双引号按 cmd 规则写成两个。
function cmdQuote(value) {
  if (value === "") {
    return '""';
  }
  if (!/[\s"]/.test(value)) {
    return value;
  }
  return '"' + value.replace(/"/g, '""') + '"';
}

function runEntry(entry, args) {
  if (IS_WINDOWS && !/\.exe$/i.test(entry)) {
    const commandLine = [entry].concat(args).map(cmdQuote).join(" ");
    return spawnSync("cmd.exe", ["/d", "/s", "/c", commandLine], {
      stdio: "inherit",
      windowsVerbatimArguments: true
    });
  }
  return spawnSync(entry, args, { stdio: "inherit" });
}

function download(url, destination, redirectsLeft) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, (response) => {
      const status = response.statusCode || 0;
      if (status >= 300 && status < 400 && response.headers.location) {
        response.resume();
        if (redirectsLeft <= 0) {
          reject(new Error("下载重定向次数过多"));
          return;
        }
        const next = new URL(response.headers.location, url).toString();
        if (!next.startsWith("https://")) {
          reject(new Error("重定向地址不是 HTTPS，已拒绝"));
          return;
        }
        download(next, destination, redirectsLeft - 1).then(resolve, reject);
        return;
      }
      if (status !== 200) {
        response.resume();
        reject(new Error(`下载失败：HTTP ${status}（${url}）`));
        return;
      }
      const file = fs.createWriteStream(destination);
      response.pipe(file);
      file.on("finish", () => file.close(resolve));
      file.on("error", reject);
    });
    request.on("error", reject);
  });
}

// npm 参数 → 官方安装脚本参数。
function toInstallerArgs(parsed) {
  if (IS_WINDOWS) {
    const args = ["-Ref", parsed.ref];
    if (parsed.installRoot) {
      args.push("-InstallRoot", parsed.installRoot);
    }
    if (parsed.withOcr) args.push("-WithOcr");
    if (parsed.noOcr) args.push("-NoOcr");
    if (parsed.noPath) args.push("-NoPath");
    if (parsed.rollback) args.push("-Rollback");
    if (parsed.uninstall) args.push("-Uninstall");
    return args;
  }
  const args = ["--ref", parsed.ref];
  if (parsed.installRoot) {
    args.push("--install-root", parsed.installRoot);
  }
  if (parsed.withOcr) args.push("--with-ocr");
  if (parsed.noOcr) args.push("--no-ocr");
  if (parsed.noPath) args.push("--no-path");
  if (parsed.rollback) args.push("--rollback");
  if (parsed.uninstall) args.push("--uninstall");
  return args;
}

function parseArgs(argv) {
  const parsed = {
    ref: DEFAULT_REF,
    installRoot: defaultRoot(),
    withOcr: false,
    noOcr: false,
    noPath: false,
    rollback: false,
    uninstall: false
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--ref") {
      parsed.ref = argv[index + 1] || DEFAULT_REF;
      index += 1;
    } else if (arg === "--install-root") {
      parsed.installRoot = argv[index + 1] || "";
      index += 1;
    } else if (arg === "--with-ocr") {
      parsed.withOcr = true;
    } else if (arg === "--no-ocr") {
      parsed.noOcr = true;
    } else if (arg === "--no-path") {
      parsed.noPath = true;
    } else if (arg === "--rollback") {
      parsed.rollback = true;
    } else if (arg === "--uninstall") {
      parsed.uninstall = true;
    }
  }
  if (parsed.withOcr && parsed.noOcr) {
    throw new Error("--with-ocr 与 --no-ocr 不能同时使用。");
  }
  if ((parsed.rollback || parsed.uninstall) && (parsed.withOcr || parsed.noOcr)) {
    throw new Error("--with-ocr / --no-ocr 只能用于安装或更新。");
  }
  if (!parsed.ref || !/^(main|[0-9a-f]{40}|[A-Za-z0-9._\-]+)$/.test(parsed.ref)) {
    throw new Error("--ref 取值不合法：" + parsed.ref);
  }
  if (parsed.installRoot && !path.isAbsolute(parsed.installRoot)) {
    throw new Error("--install-root 必须是本机绝对路径。");
  }
  return parsed;
}

// PowerShell 单引号字符串字面量；内部单引号写成两个。
function psQuote(value) {
  return "'" + String(value).replace(/'/g, "''") + "'";
}

// install.ps1 按 UTF-8 无 BOM 保存（保证 irm | iex 在 Windows PowerShell 5.1 与 PowerShell 7
// 下都能正确读取中文；见 docs/安装与更新.md）。5.1 的 -File 会按本地代码页误读中文导致解析
// 失败，因此本地执行必须显式按 UTF-8 读取后以 scriptblock 执行（5.1 与 7 均可用）。
const INSTALLER_SWITCHES = new Set(["-Ref", "-InstallRoot", "-WithOcr", "-NoOcr", "-NoPath", "-Rollback", "-Uninstall"]);

function buildWindowsInstallCommand(scriptPath, installerArgs) {
  const args = installerArgs
    .map((arg) => (INSTALLER_SWITCHES.has(arg) ? arg : psQuote(arg)))
    .join(" ");
  return "$ErrorActionPreference='Stop';" +
    "$code=Get-Content -Raw -Encoding UTF8 -LiteralPath " + psQuote(scriptPath) + ";" +
    "try { & ([scriptblock]::Create($code))" + (args ? " " + args : "") + " } catch { Write-Error $_; exit 1 }";
}

// 下载并执行官方安装脚本；只允许 HTTPS，执行后删除临时脚本。
async function runInstaller(parsed) {
  const suffix = IS_WINDOWS ? ".ps1" : ".sh";
  const scriptPath = path.join(os.tmpdir(), `bidflow-install-${Date.now()}-${process.pid}${suffix}`);
  const url = `${REPO_RAW}/${parsed.ref}/${INSTALL_SCRIPT}`;
  info(`下载官方安装脚本：${url}`);
  try {
    await download(url, scriptPath, 3);
  } catch (error) {
    fail(`无法下载安装脚本：${error.message}`);
    return 1;
  }
  let result;
  if (IS_WINDOWS) {
    result = spawnSync("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", buildWindowsInstallCommand(scriptPath, toInstallerArgs(parsed))], {
      stdio: "inherit"
    });
  } else {
    result = spawnSync("bash", [scriptPath].concat(toInstallerArgs(parsed)), { stdio: "inherit" });
  }
  try {
    fs.unlinkSync(scriptPath);
  } catch {
    // 临时脚本删除失败不影响结果
  }
  if ((result.status || 0) !== 0) {
    return result.status == null ? 1 : result.status;
  }
  info(`安装/更新完成。安装目录：${parsed.installRoot}`);
  info("请新开一个终端（或重新打开 Agent）以继承新的 PATH，然后运行：bidflow doctor");
  return 0;
}

function printHelp() {
  console.log(`BidFlow 命令行（npm 包装器，包名 bidflow）

用法：
  bidflow <子命令> [参数]        转发到本机已安装的 BidFlow；未安装时先自动安装再转发
  bidflow --help                 显示本说明

安装管理参数（只执行安装、更新、回滚或卸载，不转发命令）：
  --with-ocr                     安装/更新并附带扫描件 OCR 依赖
  --no-ocr                       安装/更新并明确不安装 OCR（覆盖上次选择）
  --no-path                      安装/更新但不修改用户 PATH
  --rollback                     回滚到上一版本（不联网）
  --uninstall                    卸载本安装器管理的内容
  --ref <main|标签|40位SHA>      指定要安装的提交，默认 ${DEFAULT_REF}
  --install-root <绝对路径>      指定安装根目录（默认 ${defaultRoot()}）

环境变量：BIDFLOW_REF（默认安装提交）、BIDFLOW_INSTALL_ROOT（安装根目录）。
说明：npm 包只负责安装引导与命令转发；核心是本机 Python 程序 bidflow-local，
用 uv 管理隔离的 Python 与依赖，程序本身不调用模型 API、不自动签章、不提交投标。
首次使用：bidflow doctor
BidFlow 子命令（status、next、ingest、task、confirm、build、verify 等）
在安装完成后运行 bidflow --help 查看。`);
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv.includes("--help") || argv.includes("-h")) {
    printHelp();
    return 0;
  }
  if (argv.some((arg) => INSTALL_FLAGS.includes(arg))) {
    let parsed;
    try {
      parsed = parseArgs(argv);
    } catch (error) {
      fail(error.message);
      return 2;
    }
    return runInstaller(parsed);
  }
  const root = defaultRoot();
  let entry = findEntry(root);
  if (!entry) {
    info("本机尚未安装 BidFlow 核心，先执行安装（用 uv 管理隔离的 Python 与依赖）...");
    const code = await runInstaller(parseArgs([]));
    if (code !== 0) {
      return code;
    }
    entry = findEntry(root);
    if (!entry) {
      fail("安装命令已执行成功，但未找到 BidFlow 入口；请新开一个终端后重试。");
      return 1;
    }
  }
  const result = runEntry(entry, argv);
  return result.status == null ? 1 : result.status;
}

if (require.main === module) {
  main().then((code) => {
    process.exit(code);
  }, (error) => {
    fail(error && error.message ? error.message : String(error));
    process.exit(1);
  });
}

// 供测试引用（直接以 CLI 运行不受影响）。
module.exports = { buildWindowsInstallCommand, psQuote, toInstallerArgs };
