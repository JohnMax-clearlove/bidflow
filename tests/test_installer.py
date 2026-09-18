"""install.ps1 的离线流程测试。

全部通过 mock 的 uv、uv 引导脚本和 mock 的 bidflow doctor 运行：
- 不发起真实网络安装，不下载任何真实文件；
- 不修改真实用户持久 PATH（用 BIDFLOW_INSTALLER_TEST_USER_PATH_FILE 钩子替代读写）；
- 不触碰用户项目、公司资料或共享的 uv/Python。

真实联网安装由主 Agent 在验收阶段单独执行，本文件的通过只代表流程正确。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PS1 = ROOT / "install.ps1"

SHA_A = "a" * 40
SHA_B = "b" * 40
REPO = "JohnMax-clearlove/bidflow"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="install.ps1 仅面向 Windows")

# mock bidflow：doctor 输出与真实 CLI 相同的 JSON 结构；OCR 状态由 release 内的 ocr.flag 推断。
MOCK_BIDFLOW_CMD = r"""@echo off
if /i "%~1"=="doctor" goto doctor
echo %*
exit /b 0
:doctor
if "%MOCK_DOCTOR_FAIL%"=="1" (
  echo {"ok": false, "error": "mock doctor failure", "command": "doctor"}
  exit /b 1
)
set "OCR=false"
if exist "%~dp0..\tool\ocr.flag" set "OCR=true"
echo {"ok": true, "result": {"version": "0.2.0", "python": "3.12.10", "modules": {"pydantic": true, "docx": true, "pdfplumber": true, "pypdfium2": true, "pypdf": true, "openpyxl": true, "PIL": true, "rapidocr": %OCR%, "onnxruntime": %OCR%}, "pandoc": null, "word_registered": false, "ocr": "mock models", "model_api_required": false, "acceptance_status": "pending"}}
exit /b 0
"""

# mock uv：记录调用参数；按 UV_TOOL_DIR/UV_TOOL_BIN_DIR 造出同级入口；MOCK_UV_FAIL=1 时失败。
MOCK_UV_CMD = r"""@echo off
if defined MOCK_UV_LOG echo %*>>"%MOCK_UV_LOG%"
if "%MOCK_UV_FAIL%"=="1" exit /b 1
if not "%~1"=="tool" exit /b 0
if not "%~2"=="install" exit /b 0
if not exist "%UV_TOOL_DIR%" mkdir "%UV_TOOL_DIR%"
if not exist "%UV_TOOL_BIN_DIR%" mkdir "%UV_TOOL_BIN_DIR%"
copy /y "%MOCK_BIDFLOW_TEMPLATE%" "%UV_TOOL_BIN_DIR%\bidflow.cmd" >nul
> "%UV_TOOL_DIR%\installed.marker" echo ok
set "HASOCR=false"
echo %* | findstr /c:"[ocr]" >nul && set "HASOCR=1"
if "%HASOCR%"=="1" > "%UV_TOOL_DIR%\ocr.flag" echo yes
exit /b 0
"""

# mock uv 引导安装器：只把 mock uv 放进 UV_INSTALL_DIR，并留下可断言的引导痕迹。
MOCK_UV_INSTALLER = r"""param()
if (-not $env:UV_INSTALL_DIR) { exit 3 }
New-Item -ItemType Directory -Force -Path $env:UV_INSTALL_DIR | Out-Null
Copy-Item -LiteralPath $env:MOCK_UV_PAYLOAD -Destination (Join-Path $env:UV_INSTALL_DIR 'uv.cmd') -Force
Set-Content -LiteralPath (Join-Path $env:UV_INSTALL_DIR '..\bootstrapped.txt') -Value 'yes' -Encoding ASCII
exit 0
"""


def _which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _ps51() -> str | None:
    return _which("powershell.exe", "powershell")


def _ps7() -> str | None:
    return _which("pwsh.exe", "pwsh")


def _default_ps() -> str:
    found = _ps51() or _ps7()
    if not found:
        pytest.skip("未找到 PowerShell 可执行文件")
    return found


def _decode(data: bytes) -> str:
    """兼容 PS5.1 的 OEM 输出与 PS7 的 UTF-8 输出，保证断言不受控制台代码页影响。"""
    for encoding in ("utf-8", "oem", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _proc(returncode: int, stdout: bytes, stderr: bytes) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=_decode(stdout), stderr=_decode(stderr))


def _ps_lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _make_junction(link: Path, target: Path) -> None:
    proc = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    assert proc.returncode == 0, _decode(proc.stdout + proc.stderr)


class Lab:
    """一次测试的隔离环境：mock 工具 + 专用安装根目录。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.mock_dir = tmp / "mock"
        self.mock_dir.mkdir()
        self.uv_log = tmp / "uv.log"
        self.user_path_file = tmp / "user_path.txt"
        self.template = self.mock_dir / "bidflow_template.cmd"
        self.template.write_text(MOCK_BIDFLOW_CMD, encoding="ascii", newline="")
        (self.mock_dir / "uv.cmd").write_text(MOCK_UV_CMD, encoding="ascii", newline="")
        self.uv_installer = self.mock_dir / "mock_uv_installer.ps1"
        self.uv_installer.write_text(MOCK_UV_INSTALLER, encoding="ascii", newline="")
        self.root = tmp / "安装 目录" / "BidFlow"

    def base_env(self, path_mode: str = "with-uv") -> dict:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("UV_")}
        env["BIDFLOW_INSTALLER_TEST_USER_PATH_FILE"] = str(self.user_path_file)
        env["MOCK_UV_LOG"] = str(self.uv_log)
        env["MOCK_BIDFLOW_TEMPLATE"] = str(self.template)
        env["BIDFLOW_INSTALLER_TEST_UV_INSTALLER"] = str(self.uv_installer)
        env["MOCK_UV_PAYLOAD"] = str(self.mock_dir / "uv.cmd")
        env.pop("MOCK_UV_FAIL", None)
        env.pop("MOCK_DOCTOR_FAIL", None)
        if path_mode == "with-uv":
            env["PATH"] = str(self.mock_dir) + os.pathsep + os.environ.get("PATH", "")
        elif path_mode == "without-uv":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            # 不把 mock 目录放进 PATH，确保 Get-Command uv 找不到任何 uv。
            env["PATH"] = os.pathsep.join([os.path.join(system_root, "System32"), system_root])
        return env

    def run(self, *args: str, root: str | Path | None = None, extra_env: dict | None = None,
            ps: str | None = None, path_mode: str = "with-uv", command_mode: str = "script",
            include_root: bool = True, timeout: int = 300) -> SimpleNamespace:
        env = self.base_env(path_mode)
        if extra_env:
            env.update(extra_env)
        exe = ps or _default_ps()
        target_root = str(root if root is not None else self.root)
        if command_mode == "file":
            cmd = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALL_PS1)]
            if include_root:
                cmd += ["-InstallRoot", target_root]
            cmd += list(args)
        elif command_mode == "script":
            # 文档推荐的本地执行方式：显式按 UTF-8 读取后执行，兼容 PS5.1 与 PS7。
            body = "& $sb"
            if include_root:
                body += " -InstallRoot " + _ps_lit(target_root)
            if args:
                body += " " + " ".join(args)
            wrapper = (
                "$ErrorActionPreference='Stop';$failed=$false;"
                "try { $sb=[scriptblock]::Create((Get-Content -Raw -Encoding UTF8 -LiteralPath "
                + _ps_lit(str(INSTALL_PS1)) + ")); "
                + body + " } catch { $failed=$true; Write-Output ('BIDFLOW-RUN-CAUGHT: ' + $_.Exception.Message) }; "
                "if ($failed) { exit 1 } else { exit 0 }"
            )
            cmd = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", wrapper]
        else:
            raise ValueError(command_mode)
        try:
            proc = subprocess.run(cmd, capture_output=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            pytest.fail("PowerShell 调用超时")
        return _proc(proc.returncode, proc.stdout, proc.stderr)

    def run_cmd(self, *args: str, timeout: int = 120) -> SimpleNamespace:
        # cmd /c 对多段带引号命令有去引号规则，整体再包一层引号避免路径被空格截断。
        program = str(args[0])
        rest = ['"%s"' % str(a) if " " in str(a) else str(a) for a in args[1:]]
        if rest:
            command_line = 'cmd /d /c ""%s" %s"' % (program, " ".join(rest))
        else:
            command_line = 'cmd /d /c ""%s""' % program
        proc = subprocess.run(command_line, capture_output=True, env=self.base_env(), timeout=timeout)
        return _proc(proc.returncode, proc.stdout, proc.stderr)

    def state(self, root: str | Path | None = None) -> dict:
        return _read_json(Path(root if root is not None else self.root) / "state.json")

    def uv_log_text(self) -> str:
        if not self.uv_log.exists():
            return ""
        return self.uv_log.read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def test_syntax_and_public_parameters():
    raw = INSTALL_PS1.read_bytes()
    # 面向 irm | iex 的文件不能带 BOM：BOM 会被当成命令首字符，破坏参数绑定。
    assert not raw.startswith(b"\xef\xbb\xbf")
    content = raw.decode("utf-8")
    for name in ("$InstallRoot", "$Ref", "$WithOcr", "$NoOcr", "$NoPath", "$Rollback", "$Uninstall"):
        assert name in content
    hosts = [h for h in {_ps51(), _ps7()} if h]
    assert hosts, "未找到 PowerShell 可执行文件"
    path_lit = str(INSTALL_PS1).replace("'", "''")
    check = (
        "$text = [System.IO.File]::ReadAllText('" + path_lit + "', [System.Text.Encoding]::UTF8); "
        "$errs = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$errs); "
        "if ($errs -and $errs.Count -gt 0) { $errs | ForEach-Object { Write-Output $_.Message }; exit 1 }; "
        "Write-Output 'AST-OK'; exit 0"
    )
    for host in hosts:
        proc = subprocess.run([host, "-NoProfile", "-Command", check], capture_output=True)
        assert proc.returncode == 0, _decode(proc.stdout + proc.stderr)


@pytest.mark.parametrize("host_name", ["ps51", "ps7"])
def test_install_with_existing_uv_core(lab, host_name):
    host = _ps51() if host_name == "ps51" else _ps7()
    if not host:
        pytest.skip(f"未找到 {host_name}")
    url_file = lab.tmp / "should-not-resolve.txt"
    result = lab.run("-Ref", SHA_A, "-NoPath", ps=host,
                     extra_env={"BIDFLOW_INSTALLER_TEST_RESOLVE_URL_FILE": str(url_file)})
    assert result.returncode == 0, result.stdout + result.stderr
    # 40 位 SHA 必须跳过 GitHub API
    assert not url_file.exists()

    state = lab.state()
    assert state["current"]["sha"] == SHA_A
    assert state["current"]["ocr"] is False
    assert state["current"]["version"] == "0.2.0"
    assert state["path_added"] is False
    assert state["previous"] is None

    log = lab.uv_log_text()
    assert "--python 3.12" in log
    assert f"bidflow-local @ https://codeload.github.com/{REPO}/tar.gz/{SHA_A}" in log

    release_dir = lab.root / "releases" / state["current"]["release"]
    assert release_dir.is_dir()
    assert _read_json(release_dir / ".bidflow-release.json")["sha"] == SHA_A
    assert (lab.root / ".bidflow-install.json").is_file()

    stable = lab.root / "bin" / "bidflow.cmd"
    assert stable.is_file()
    content = stable.read_text(encoding="ascii")
    assert "%~dp0" in content and "[ocr]" not in content

    doctor = lab.run_cmd(stable, "doctor")
    assert doctor.returncode == 0
    assert '"ok": true' in doctor.stdout

    passthrough = lab.run_cmd(stable, "status", "--project", "a b")
    assert passthrough.returncode == 0
    assert 'status --project "a b"' in passthrough.stdout


def test_ref_resolution_uses_encoded_api_url(lab):
    commit = lab.tmp / "commit.json"
    commit.write_text(json.dumps({"sha": SHA_B}), encoding="utf-8")
    url_file = lab.tmp / "resolve-url.txt"
    ref = "feature/发布-分支"
    result = lab.run("-Ref", ref, "-NoPath",
                     extra_env={"BIDFLOW_INSTALLER_TEST_COMMIT_JSON": str(commit),
                                "BIDFLOW_INSTALLER_TEST_RESOLVE_URL_FILE": str(url_file)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert url_file.read_text(encoding="utf-8") == f"https://api.github.com/repos/{REPO}/commits/" + quote(ref, safe="")
    assert f"tar.gz/{SHA_B}" in lab.uv_log_text()


def test_no_uv_bootstrap_and_install(lab):
    result = lab.run("-Ref", SHA_A, "-NoPath", path_mode="without-uv")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (lab.root / "runtime" / "bootstrapped.txt").is_file()
    assert _read_json(lab.root / "runtime" / ".bidflow-runtime.json")["product"] == "bidflow-local"
    assert lab.state()["current"]["sha"] == SHA_A
    assert "tool install" in lab.uv_log_text()


def test_ocr_selection_and_preservation(lab):
    assert lab.run("-Ref", SHA_A, "-WithOcr", "-NoPath").returncode == 0
    state = lab.state()
    assert state["current"]["ocr"] is True
    assert "-ocr-" in state["current"]["release"]
    assert f"bidflow-local[ocr] @ https://codeload.github.com/{REPO}/tar.gz/{SHA_A}" in lab.uv_log_text()

    assert lab.run("-Ref", SHA_A, "-NoPath").returncode == 0
    state2 = lab.state()
    assert state2["current"]["ocr"] is True
    assert "-ocr-" in state2["current"]["release"]
    assert state2["previous"]["release"] == state["current"]["release"]

    assert lab.run("-Ref", SHA_A, "-NoOcr", "-NoPath").returncode == 0
    state3 = lab.state()
    assert state3["current"]["ocr"] is False
    assert f"bidflow-local @ https://codeload.github.com/{REPO}/tar.gz/{SHA_A}" in lab.uv_log_text().splitlines()[-1]


def test_failed_upgrade_keeps_previous_entry_and_state(lab):
    assert lab.run("-Ref", SHA_A, "-NoPath").returncode == 0
    state_path = lab.root / "state.json"
    cmd_path = lab.root / "bin" / "bidflow.cmd"
    before_state = state_path.read_bytes()
    before_cmd = cmd_path.read_bytes()

    result = lab.run("-Ref", SHA_B, "-NoPath", extra_env={"MOCK_UV_FAIL": "1"})
    assert result.returncode != 0
    assert state_path.read_bytes() == before_state
    assert cmd_path.read_bytes() == before_cmd
    assert len(list((lab.root / "releases").iterdir())) == 1
    assert lab.state()["current"]["sha"] == SHA_A


def test_rollback_switches_without_uv(lab):
    assert lab.run("-Ref", SHA_A, "-NoPath").returncode == 0
    assert lab.run("-Ref", SHA_B, "-NoPath").returncode == 0
    state = lab.state()
    assert state["current"]["sha"] == SHA_B and state["previous"]["sha"] == SHA_A
    log_before = lab.uv_log_text()

    result = lab.run("-Rollback", extra_env={"MOCK_UV_FAIL": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert lab.uv_log_text() == log_before
    state = lab.state()
    assert state["current"]["sha"] == SHA_A and state["previous"]["sha"] == SHA_B
    assert state["current"]["release"] in (lab.root / "bin" / "bidflow.cmd").read_text(encoding="ascii")
    assert '"ok": true' in lab.run_cmd(lab.root / "bin" / "bidflow.cmd", "doctor").stdout


def test_uninstall_keeps_unknown_and_user_dirs(lab):
    assert lab.run("-Ref", SHA_A, "-NoPath").returncode == 0
    state = lab.state()
    (lab.root / "我的资料.txt").write_text("keep", encoding="utf-8")
    (lab.root / "projects").mkdir()
    (lab.root / "projects" / "keep.txt").write_text("keep", encoding="utf-8")
    (lab.root / "releases" / "用户自建").mkdir()
    (lab.root / "releases" / "用户自建" / "x.txt").write_text("x", encoding="utf-8")
    log_before = lab.uv_log_text()

    result = lab.run("-Uninstall")
    assert result.returncode == 0, result.stdout + result.stderr
    assert lab.uv_log_text() == log_before
    assert (lab.root / "我的资料.txt").is_file()
    assert (lab.root / "projects" / "keep.txt").is_file()
    assert (lab.root / "releases" / "用户自建" / "x.txt").is_file()
    assert not (lab.root / "releases" / state["current"]["release"]).exists()
    assert not (lab.root / "state.json").exists()
    assert not (lab.root / ".bidflow-install.json").exists()
    assert not (lab.root / "runtime").exists()
    assert not (lab.root / "cache").exists()
    assert not (lab.root / "bin").exists()


def test_user_path_added_then_removed_via_hook(lab):
    lab.user_path_file.write_text(r"C:\Other;D:\Keep", encoding="utf-8")
    assert lab.run("-Ref", SHA_A).returncode == 0
    bin_path = str(lab.root / "bin")
    value = lab.user_path_file.read_text(encoding="utf-8-sig")
    assert bin_path in value
    assert lab.state()["path_added"] is True

    assert lab.run("-Ref", SHA_A).returncode == 0
    value = lab.user_path_file.read_text(encoding="utf-8-sig")
    assert value.count(bin_path) == 1

    result = lab.run("-Uninstall")
    assert result.returncode == 0
    value = lab.user_path_file.read_text(encoding="utf-8-sig")
    assert bin_path not in value
    assert r"C:\Other" in value and r"D:\Keep" in value


def test_dangerous_or_invalid_roots_rejected_before_any_action(lab):
    file_path = lab.tmp / "some-file.txt"
    file_path.write_text("x", encoding="utf-8")
    drive_root = Path(lab.tmp.anchor).as_posix()
    cases = [drive_root, str(lab.tmp), str(file_path), str(lab.tmp / "bad%dir"), str(lab.tmp / "bad;dir")]
    for value in cases:
        result = lab.run("-Ref", SHA_A, "-NoPath", root=value)
        assert result.returncode != 0, value
    assert not (lab.tmp / "bad%dir").exists()
    assert not (lab.tmp / "bad;dir").exists()
    assert not lab.uv_log.exists()


def test_invalid_mode_combinations_rejected_before_actions(lab):
    result = lab.run("-Rollback", "-Uninstall")
    assert result.returncode != 0
    assert not lab.root.exists()
    assert not lab.uv_log.exists()

    assert lab.run("-Uninstall", "-WithOcr").returncode != 0
    assert lab.run("-Rollback", "-Ref", SHA_A).returncode != 0
    assert lab.run("-Rollback").returncode != 0
    assert lab.run("-WithOcr", "-NoOcr", "-NoPath").returncode != 0
    assert not lab.uv_log.exists()

    result = lab.run("-Uninstall")
    assert result.returncode == 0
    assert not lab.root.exists()


def test_reparse_points_block_install_and_uninstall(lab):
    outside = lab.tmp / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")

    lab.root.mkdir(parents=True)
    (lab.root / ".bidflow-install.json").write_text(
        json.dumps({"schema_version": 1, "product": "bidflow-local"}), encoding="utf-8")
    _make_junction(lab.root / "releases", outside)
    result = lab.run("-Ref", SHA_A, "-NoPath")
    assert result.returncode != 0
    assert (outside / "keep.txt").is_file()
    assert not lab.uv_log.exists()

    root2 = lab.tmp / "安装2" / "BidFlow"
    assert lab.run("-Ref", SHA_A, "-NoPath", root=root2).returncode == 0
    state = lab.state(root2)
    release_dir = root2 / "releases" / state["current"]["release"]
    outside2 = lab.tmp / "outside2"
    outside2.mkdir()
    (outside2 / "keep2.txt").write_text("keep", encoding="utf-8")
    _make_junction(release_dir / "linked", outside2)
    result = lab.run("-Uninstall", root=root2)
    assert result.returncode == 0
    assert (outside2 / "keep2.txt").is_file()
    assert release_dir.exists()
    assert not (root2 / ".bidflow-install.json").exists()


def test_environment_restored_after_run(lab):
    script_path = _ps_lit(str(INSTALL_PS1))
    root_path = _ps_lit(str(lab.root))
    wrapper = (
        "$env:UV_TOOL_DIR = 'preset-tool'; "
        "$env:UV_CACHE_DIR = 'preset-cache'; "
        "$pathBefore = $env:PATH; "
        "$userBefore = $env:USERPROFILE; "
        "$sb = [scriptblock]::Create((Get-Content -Raw -Encoding UTF8 -LiteralPath " + script_path + ")); "
        "& $sb -InstallRoot " + root_path + " -Ref " + SHA_A + " -NoPath; "
        "if ($env:UV_TOOL_DIR -ne 'preset-tool') { exit 31 }; "
        "if ($env:UV_CACHE_DIR -ne 'preset-cache') { exit 32 }; "
        "if ($env:PATH -ne $pathBefore) { exit 33 }; "
        "if ($env:USERPROFILE -ne $userBefore) { exit 34 }; "
        "exit 0"
    )
    proc = subprocess.run([_default_ps(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", wrapper],
                          capture_output=True, env=lab.base_env(), timeout=300)
    assert proc.returncode == 0, _decode(proc.stdout + proc.stderr)


def test_default_install_root_and_default_ref(lab):
    local_app_data = lab.tmp / "localappdata"
    commit = lab.tmp / "commit-main.json"
    commit.write_text(json.dumps({"sha": SHA_B}), encoding="utf-8")
    url_file = lab.tmp / "resolve-main.txt"
    result = lab.run("-NoPath", include_root=False,
                     extra_env={"LOCALAPPDATA": str(local_app_data),
                                "BIDFLOW_INSTALLER_TEST_COMMIT_JSON": str(commit),
                                "BIDFLOW_INSTALLER_TEST_RESOLVE_URL_FILE": str(url_file)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert url_file.read_text(encoding="utf-8") == f"https://api.github.com/repos/{REPO}/commits/main"
    state = lab.state(local_app_data / "BidFlow")
    assert state["current"]["sha"] == SHA_B


def test_local_utf8_run_with_parameters(lab):
    result = lab.run("-Ref", SHA_A, "-NoPath", command_mode="script")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (lab.root / "state.json").is_file()

    result = lab.run("-Ref", SHA_B, "-NoPath", command_mode="script", extra_env={"MOCK_UV_FAIL": "1"})
    assert result.returncode != 0
    assert "BIDFLOW-RUN-CAUGHT" in result.stdout
    assert lab.state()["current"]["sha"] == SHA_A


def test_ps7_can_run_downloaded_file_directly(lab):
    host = _ps7()
    if not host:
        pytest.skip("未找到 PowerShell 7")
    result = lab.run("-Ref", SHA_A, "-NoPath", ps=host, command_mode="file")
    assert result.returncode == 0, result.stdout + result.stderr
