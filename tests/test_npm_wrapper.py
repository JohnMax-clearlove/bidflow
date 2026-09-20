"""npm 包装包与 install.sh 的离线测试。

不联网、不执行真实安装：只校验包结构、脚本语法（工具可用时）、
参数互斥校验，以及命令转发（用 mock 入口与 state.json）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NPM_DIR = ROOT / "npm"
WRAPPER = NPM_DIR / "bin" / "bidflow.js"
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"

MOCK_BIDFLOW_CMD = r"""@echo off
echo cmd-forwarded %*
exit /b 0
"""

MOCK_BIDFLOW_SH = """#!/usr/bin/env bash
echo "sh-forwarded $@"
exit 0
"""


@pytest.fixture(scope="module")
def package_json() -> dict:
    return json.loads((NPM_DIR / "package.json").read_text(encoding="utf-8"))


def test_package_metadata(package_json: dict) -> None:
    assert package_json["name"] == "bidflow"
    assert package_json["version"] == "0.2.0"
    assert package_json["bin"] == {"bidflow": "bin/bidflow.js"}
    assert package_json["files"] == ["bin/", "README.md"]
    assert package_json["engines"]["node"] >= "18"
    assert package_json["os"] == ["win32", "darwin", "linux"]
    assert (NPM_DIR / "README.md").exists()
    # 包内容只允许 bin 与 README，防止误把仓库其他内容发上 npm
    assert all(item in ("bin/", "README.md") for item in package_json["files"])


def test_wrapper_constants() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert "JohnMax-clearlove/bidflow" in text
    assert "install.ps1" in text and "install.sh" in text
    for flag in ("--with-ocr", "--no-ocr", "--no-path", "--rollback", "--uninstall", "--ref", "--install-root"):
        assert flag in text, f"包装器缺少参数：{flag}"
    assert "state.json" in text, "包装器必须读取安装根目录的 state.json"


def test_wrapper_syntax_with_node() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用")
    result = subprocess.run([node, "--check", str(WRAPPER)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_sh_exists_and_pins_uv() -> None:
    assert INSTALL_SH.is_file()
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "JohnMax-clearlove/bidflow" in text
    assert "bidflow-local" in text
    # 两个安装器应使用同一个 uv 固定版本
    import re

    version_ps = re.search(r"UvVersion\s*=\s*'([\d.]+)'", INSTALL_PS1.read_text(encoding="utf-8"))
    version_sh = re.search(r'UV_VERSION="([\d.]+)"', text)
    assert version_ps and version_sh, "两个安装器都应声明 uv 固定版本"
    assert version_ps.group(1) == version_sh.group(1)


def test_install_sh_syntax_with_bash() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash 不可用")
    result = subprocess.run([bash, "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="mock 入口为 Windows cmd")
def test_wrapper_forwards_to_installed_entry(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用")
    os = __import__("os")
    root = tmp_path / "BidFlow"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "bidflow.cmd").write_text(MOCK_BIDFLOW_CMD, encoding="ascii")
    env = dict(os.environ)
    env["BIDFLOW_INSTALL_ROOT"] = str(root)
    env.pop("BIDFLOW_REF", None)
    result = subprocess.run(
        [node, str(WRAPPER), "doctor", "--project", "some project"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "cmd-forwarded" in result.stdout
    # 无 state.json 时使用根 bin 稳定入口，含空格的参数应原样转发
    assert "--project" in result.stdout and '"some project"' in result.stdout


def test_wrapper_rejects_conflicting_flags(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用")
    os = __import__("os")
    env = dict(os.environ)
    env["BIDFLOW_INSTALL_ROOT"] = str(tmp_path / "unused")
    result = subprocess.run(
        [node, str(WRAPPER), "--with-ocr", "--no-ocr"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert result.returncode == 2
    assert "不能同时使用" in (result.stdout + result.stderr)


def test_wrapper_help() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用")
    result = subprocess.run([node, str(WRAPPER), "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0
    assert "npm 包装器" in result.stdout
    assert "bidflow doctor" in result.stdout


def _decode_ps_bytes(data: bytes) -> str:
    """兼容 PS5.1 的 OEM 输出与 PS7 的 UTF-8 输出。"""
    for encoding in ("utf-8", "oem", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


@pytest.mark.skipif(sys.platform != "win32", reason="本用例验证 Windows PowerShell 执行路径")
def test_windows_install_command_reads_utf8_instead_of_file_flag(tmp_path: Path) -> None:
    """Windows 上必须以 UTF-8 读取方式执行 install.ps1，不得使用 -File。

    背景：install.ps1 按 UTF-8 无 BOM 保存（见 docs/安装与更新.md），Windows PowerShell 5.1
    的 -File 会按本地代码页误读中文导致解析失败；包装器曾用 powershell.exe -File，
    在中文 Windows 上 npm 安装命令会直接坏掉（已有 docs 与实际测试佐证：只有 PS7 能直接 -File）。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用")
    text = WRAPPER.read_text(encoding="utf-8")
    assert "Get-Content -Raw -Encoding UTF8" in text, "安装脚本必须以 UTF-8 显式读取"
    assert '"-File"' not in text, "不得用 -File 执行 install.ps1（5.1 会误读中文）"

    # 动态复现：用含中文与空格的替身脚本走包装器的真实执行命令
    stub = tmp_path / "替身 安装 脚本.ps1"
    stub.write_text(
        "param([string]$Ref, [string]$InstallRoot, [switch]$NoPath)\n"
        "Write-Output ('标记=中文正常 ref=' + $Ref + ' root=' + $InstallRoot + ' noPath=' + [bool]$NoPath)\n"
        "exit 0\n",
        encoding="utf-8",
    )
    probe = (
        "const w = require(process.argv[1]);"
        "process.stdout.write(w.buildWindowsInstallCommand(process.argv[2],"
        " ['-Ref', 'main', '-NoPath', '-InstallRoot', 'D:/x y']));"
    )
    built = subprocess.run([node, "-e", probe, str(WRAPPER), str(stub)], capture_output=True, text=True, encoding="utf-8")
    assert built.returncode == 0, built.stderr
    command = built.stdout
    assert "Get-Content -Raw -Encoding UTF8" in command
    assert "-File" not in command

    ps = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not ps:
        pytest.skip("未找到 PowerShell")
    result = subprocess.run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
    )
    output = _decode_ps_bytes(result.stdout) + _decode_ps_bytes(result.stderr)
    assert result.returncode == 0, output
    assert "标记=中文正常" in output, output
    assert "ref=main" in output and "noPath=True" in output and "root=D:/x y" in output, output
