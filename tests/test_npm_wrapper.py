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
