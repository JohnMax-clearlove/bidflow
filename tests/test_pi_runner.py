"""离线测试 `scripts/invoke_pi_worker.ps1`：用临时 PATH 上的 mock pi 覆盖判定规则。

- 只调用 pwsh；缺少 pwsh 时明确 skip。
- mock pi 由 Python 脚本 + 平台外壳（Windows 为 `pi.cmd`）组成，只写合成 JSONL 与合成产物，
  **不发起任何付费模型调用**。
- 用到子进程输出管道，沿用 conftest 的 `piped_subprocess` 夹具在受限环境显式 skip。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "invoke_pi_worker.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="未找到 pwsh，跳过 pi 派工入口测试（本测试只通过 pwsh 调用脚本）")

# ---------------------------------------------------------------- mock pi

MOCK_PI_SOURCE = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ARTIFACT = "产物 文件.txt"

HOLDER_SOURCE = """
import msvcrt, sys, time
f = open(sys.argv[1], 'r+b')
f.seek(0)
msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
open(sys.argv[2], 'w').write('locked')
time.sleep(60)
"""


def _make_link_escape(cwd):
    """在 WorkDir 内创建指向外部的链接目录（Windows 优先用 junction），供越界检查测试。"""
    status = os.environ.get("MOCK_LINK_STATUS")
    outside = cwd.parent / "链接越界 目标"
    outside.mkdir(exist_ok=True)
    (outside / "借道 产物.txt").write_text("外部目标内容，不应被读取", encoding="utf-8")
    link = cwd / "链接 目录"
    err = ""
    try:
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                err = (r.stdout + r.stderr).strip() or f"mklink 退出码 {r.returncode}"
        else:
            os.symlink(outside, link)
    except OSError as exc:
        err = str(exc)
    if status:
        Path(status).write_text(err or "ok", encoding="utf-8")


def _hold_lock(path):
    """Windows 上用独立进程对产物加字节范围锁，等其确认已加锁后再返回，使 Get-FileHash 读取失败。"""
    ready = path.parent / "holder-ready.txt"
    proc = subprocess.Popen([sys.executable, "-c", HOLDER_SOURCE, str(path), str(ready)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.05)
    pid_file = os.environ.get("MOCK_LOCK_PID")
    if pid_file:
        Path(pid_file).write_text(str(proc.pid), encoding="utf-8")

ZERO_USAGE = {
    "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}
NORMAL_USAGE = {
    "input": 100, "output": 50, "cacheRead": 10, "cacheWrite": 5, "reasoning": 7, "totalTokens": 172,
    "cost": {"input": 0.001, "output": 0.002, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.003},
}


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    scenario = os.environ.get("MOCK_SCENARIO", "success_new_file")
    cwd = Path.cwd()
    receipt = os.environ.get("MOCK_RECEIPT")
    if receipt:
        Path(receipt).write_text(
            json.dumps({"cwd": str(cwd), "argv": sys.argv[1:]}, ensure_ascii=False),
            encoding="utf-8",
        )

    emit({"type": "session", "version": 3, "id": "mock-session", "timestamp": "2026-01-01T00:00:00.000Z", "cwd": str(cwd)})
    emit({"type": "agent_start"})
    # 流式增量：体积大且含伪 base64；脚本必须跳过而不解析。
    for _ in range(3):
        emit({"type": "message_update", "usage": ZERO_USAGE,
              "assistantMessageEvent": {"type": "toolcall_delta", "contentIndex": 1, "delta": "A" * 20000}})
    emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "write"})

    stop_reason = "stop"
    usage = dict(NORMAL_USAGE)
    extra_assistants = []
    corrupt = False
    tool_error = None

    if scenario in ("success_new_file", "duplicate_usage", "corrupted_log", "output_overwrite", "tool_error_huge", "hash_read_error"):
        (cwd / ARTIFACT).write_text("产物内容：" + scenario, encoding="utf-8")
        if scenario == "hash_read_error":
            _hold_lock(cwd / ARTIFACT)
    elif scenario == "chinese_spaces":
        target = cwd / "子 目录" / "产出 文件.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("中文产物内容", encoding="utf-8")
    elif scenario == "aborted":
        stop_reason = "aborted"
    elif scenario == "tool_use_at_end":
        stop_reason = "toolUse"
        (cwd / ARTIFACT).write_text("工具调用中断场景产物", encoding="utf-8")
    elif scenario == "length":
        stop_reason = "length"
        (cwd / ARTIFACT).write_text("length 场景产物", encoding="utf-8")
    elif scenario == "missing_usage":
        usage = None
        (cwd / ARTIFACT).write_text("无 usage 场景产物", encoding="utf-8")
    elif scenario == "null_stop_reason":
        stop_reason = None
    elif scenario == "unknown_stop_reason":
        stop_reason = "weirdEnum"
    elif scenario == "link_escape_after":
        _make_link_escape(cwd)
    # no_artifact / existing_unchanged：不创建任何文件

    if scenario == "duplicate_usage":
        extra_assistants = [dict(NORMAL_USAGE)]
    if scenario == "corrupted_log":
        corrupt = True
    if scenario == "tool_error_huge":
        tool_error = "B" * 50000

    if tool_error is not None:
        emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "read", "isError": True,
              "result": {"content": [
                  {"type": "image", "data": tool_error, "mimeType": "image/png"},
                  {"type": "text", "text": "读取失败：mock 文件不存在"}]}})
    else:
        emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "write", "isError": False,
              "result": {"content": [{"type": "text", "text": "ok"}]}})

    emit({"type": "message_start", "message": {"role": "toolResult", "toolCallId": "c1", "toolName": "write",
                                               "content": [{"type": "text", "text": "ok"}]}})
    emit({"type": "message_end", "message": {"role": "toolResult", "toolCallId": "c1", "toolName": "write",
                                             "content": [{"type": "text", "text": "ok"}]}})

    def assistant_msg(stop, usg, rid):
        message = {
            "role": "assistant", "content": [{"type": "text", "text": "已完成 mock 任务。"}],
            "api": "openai-completions", "provider": "opencode-go", "model": "deepseek-v4.1-flash",
            "stopReason": stop, "timestamp": "2026-01-01T00:00:01.000Z", "responseId": rid,
        }
        if usg is not None:
            message["usage"] = usg
        return {"type": "message_end", "message": message}

    emit(assistant_msg(stop_reason, usage, "mock-response-1"))
    for usg in extra_assistants:
        emit(assistant_msg("stop", usg, "mock-response-1"))

    if corrupt:
        sys.stdout.write('{"type":"message_end","message":{"role":"assistant","broken\n')
        sys.stdout.flush()

    emit({"type": "turn_end", "message": {"role": "assistant", "content": []},
          "toolResults": [{"toolCallId": "c1", "toolName": "write", "content": [{"type": "text", "text": "ok"}]}]})
    huge = "C" * 100000
    emit({"type": "agent_end", "messages": [{"role": "direct", "content": [{"type": "image", "data": huge}]}],
          "willRetry": False})
    emit({"type": "agent_settled"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


@pytest.fixture(autouse=True)
def _require_subprocess_pipes(piped_subprocess):
    """需要捕获 pwsh 输出；受限环境无法建管道时按 conftest 约定显式 skip。"""
    return piped_subprocess


@pytest.fixture
def mock_pi(tmp_path):
    mockdir = tmp_path / "mock pi"
    mockdir.mkdir()
    (mockdir / "mock_pi.py").write_text(MOCK_PI_SOURCE, encoding="utf-8")
    if os.name == "nt":
        (mockdir / "pi.cmd").write_text(
            '@echo off\r\n"%MOCK_PI_PYTHON%" "%~dp0mock_pi.py" %*\r\n', encoding="utf-8")
    else:
        shim = mockdir / "pi"
        shim.write_text('#!/bin/sh\nexec "$MOCK_PI_PYTHON" "$(dirname "$0")/mock_pi.py" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = str(mockdir) + os.pathsep + env.get("PATH", "")
    env["MOCK_PI_PYTHON"] = sys.executable
    env["MOCK_SCENARIO"] = "success_new_file"
    env["MOCK_RECEIPT"] = str(tmp_path / "mock-receipt.json")
    env["PYTHONIOENCODING"] = "utf-8"
    return SimpleNamespace(dir=mockdir, env=env, receipt=tmp_path / "mock-receipt.json")


# ---------------------------------------------------------------- helpers

def ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_runner(mock_pi, task, workdir, output_dir, expected=(), allow_unchanged=False):
    """在 mock pi 的 PATH 下调用脚本；返回 CompletedProcess。"""
    parts = [
        "&", ps_quote(SCRIPT),
        "-TaskFile", ps_quote(task),
        "-WorkDir", ps_quote(workdir),
        "-OutputDir", ps_quote(output_dir),
    ]
    if expected:
        parts += ["-ExpectedOutput", "@(" + ",".join(ps_quote(p) for p in expected) + ")"]
    if allow_unchanged:
        parts.append("-AllowUnchanged")
    # -Command 调用脚本文件时需显式传播脚本退出码（-File 不支持字符串数组参数）。
    command = " ".join(parts) + "; exit $LASTEXITCODE"
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
        env=mock_pi.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    return proc


def summary_of(output_dir: Path) -> dict:
    return json.loads((Path(output_dir) / "summary.json").read_text(encoding="utf-8-sig"))


def make_workdir(tmp_path, name="工作 目录"):
    workdir = tmp_path / name
    workdir.mkdir()
    task = workdir / "任务 文件.md"
    task.write_text("# 合成测试任务\n\n只用于离线测试 pi 派工入口。\n", encoding="utf-8")
    return workdir, task


def _try_create_dir_link(link: Path, target: Path) -> str:
    """创建目录符号链接/junction；成功返回空串，失败返回错误文本。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return (r.stdout + r.stderr).strip() or f"mklink 退出码 {r.returncode}"
            return ""
        os.symlink(target, link, target_is_directory=True)
        return ""
    except OSError as exc:
        return str(exc)


# ---------------------------------------------------------------- tests

def test_success_new_file_and_summary_fields(mock_pi, tmp_path):
    workdir, task = make_workdir(tmp_path)
    out = tmp_path / "out"
    proc = run_runner(mock_pi, task, workdir, out, expected=["产物 文件.txt"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    s = summary_of(out)
    assert s["status"] == "待主 Agent 验收"
    assert s["failed"] is False
    assert s["requestedThinking"] == "max"
    assert s["requestedRoute"] == "opencode-go/deepseek-v4.1-flash"
    assert s["provider"] == "opencode-go" and s["model"] == "deepseek-v4.1-flash"
    assert s["routeMatched"] is True
    assert s["lastAssistantStopReason"] == "stop"
    assert s["usageAvailable"] is True and s["usage"]["input"] == 100
    assert s["ignoredEventCounts"]["忽略:message_update"] == 3
    assert s["ignoredEventCounts"]["忽略:agent_end"] == 1
    art = s["expectedOutputs"][0]
    assert art["ok"] is True and art["isNew"] is True and art["changed"] is True
    assert art["existsBefore"] is False and art["existsAfter"] is True
    assert art["sizeAfter"] > 0 and len(art["sha256After"]) == 64
    assert art["sha256Before"] is None
    assert (workdir / "产物 文件.txt").read_text(encoding="utf-8").startswith("产物内容")
    # 默认只打印简报，不回显 JSONL 过程
    assert '"type"' not in proc.stdout
    assert len(proc.stdout.splitlines()) <= 12


def test_expected_output_not_created_fails(mock_pi, tmp_path):
    mock_pi.env["MOCK_SCENARIO"] = "no_artifact"
    workdir, task = make_workdir(tmp_path)
    out = tmp_path / "out"
    proc = run_runner(mock_pi, task, workdir, out, expected=["产物 文件.txt", "另一个 产物.txt"])
    assert proc.returncode == 1
    s = summary_of(out)
    assert s["failed"] is True and s["status"] == "失败"
    assert s["expectedOutput"] == ["产物 文件.txt", "另一个 产物.txt"]
    assert len(s["expectedOutputs"]) == 2
    assert any("期望产物未达标" in r for r in s["failReasons"])
    for art in s["expectedOutputs"]:
        assert art["ok"] is False
        assert art["existsAfter"] is False
        assert art["failReason"] == "运行后不存在"


def test_old_artifact_unchanged_and_allow_unchanged(mock_pi, tmp_path):
    mock_pi.env["MOCK_SCENARIO"] = "existing_unchanged"
    workdir, task = make_workdir(tmp_path)
    artifact = workdir / "产物 文件.txt"
    artifact.write_text("旧产物内容", encoding="utf-8")

    out1 = tmp_path / "out1"
    proc1 = run_runner(mock_pi, task, workdir, out1, expected=["产物 文件.txt"])
    assert proc1.returncode == 1
    s1 = summary_of(out1)
    art1 = s1["expectedOutputs"][0]
    assert art1["ok"] is False and art1["changed"] is False
    assert art1["existsBefore"] is True and art1["existsAfter"] is True
    assert art1["sha256Before"] == art1["sha256After"]
    assert "AllowUnchanged" in art1["failReason"]
    assert artifact.read_text(encoding="utf-8") == "旧产物内容"

    out2 = tmp_path / "out2"
    proc2 = run_runner(mock_pi, task, workdir, out2, expected=["产物 文件.txt"], allow_unchanged=True)
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr
    s2 = summary_of(out2)
    assert s2["allowUnchanged"] is True and s2["failed"] is False
    art2 = s2["expectedOutputs"][0]
    assert art2["ok"] is True and art2["changed"] is False and art2["existsBefore"] is True


def test_expected_output_traversal_rejected_before_run(mock_pi, tmp_path):
    workdir, task = make_workdir(tmp_path)
    outside = tmp_path / "outside.txt"
    out = tmp_path / "out"
    proc = run_runner(mock_pi, task, workdir, out, expected=["../outside.txt"])
    assert proc.returncode == 2
    assert not mock_pi.receipt.exists(), "越界路径必须在调用 pi 之前被拒绝"
    assert not (out / "summary.json").exists()
    assert not outside.exists()

    out_abs = tmp_path / "out-abs"
    proc_abs = run_runner(mock_pi, task, workdir, out_abs, expected=[str(tmp_path / "abs.txt")])
    assert proc_abs.returncode == 2
    assert not mock_pi.receipt.exists(), "绝对路径必须在调用 pi 之前被拒绝"


def test_final_assistant_aborted_or_length_fails(mock_pi, tmp_path):
    workdir, task = make_workdir(tmp_path)

    mock_pi.env["MOCK_SCENARIO"] = "aborted"
    out1 = tmp_path / "out-aborted"
    proc1 = run_runner(mock_pi, task, workdir, out1)
    assert proc1.returncode == 1
    s1 = summary_of(out1)
    assert s1["lastAssistantStopReason"] == "aborted"
    assert any("aborted" in r for r in s1["failReasons"])

    mock_pi.env["MOCK_SCENARIO"] = "length"
    out2 = tmp_path / "out-length"
    proc2 = run_runner(mock_pi, task, workdir, out2, expected=["产物 文件.txt"])
    assert proc2.returncode == 1
    s2 = summary_of(out2)
    assert s2["lastAssistantStopReason"] == "length"
    assert any("length" in r for r in s2["failReasons"])
    # 产物存在且非空也不能挽救被截断的会话
    assert s2["expectedOutputs"][0]["ok"] is True
    assert s2["failed"] is True

    mock_pi.env["MOCK_SCENARIO"] = "tool_use_at_end"
    out3 = tmp_path / "out-tooluse"
    proc3 = run_runner(mock_pi, task, workdir, out3, expected=["产物 文件.txt"])
    assert proc3.returncode == 1
    s3 = summary_of(out3)
    assert s3["lastAssistantStopReason"] == "toolUse"
    assert any("toolUse" in r for r in s3["failReasons"]), "最终 assistant 停在工具调用即视为中断"


def test_missing_usage_is_null_not_zero(mock_pi, tmp_path):
    mock_pi.env["MOCK_SCENARIO"] = "missing_usage"
    workdir, task = make_workdir(tmp_path)
    out = tmp_path / "out"
    proc = run_runner(mock_pi, task, workdir, out, expected=["产物 文件.txt"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    s = summary_of(out)
    assert s["usageAvailable"] is False
    assert s["costAvailable"] is False
    for key in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens"):
        assert s["usage"][key] is None, f"usage.{key} 缺失时必须是 null，不能记为 0"
    for key in ("input", "output", "cacheRead", "cacheWrite", "total"):
        assert s["cost"][key] is None
    assert "不可表述为实际零消耗" in s["note"]


def test_duplicate_response_usage_counted_once(mock_pi, tmp_path):
    mock_pi.env["MOCK_SCENARIO"] = "duplicate_usage"
    workdir, task = make_workdir(tmp_path)
    out = tmp_path / "out"
    proc = run_runner(mock_pi, task, workdir, out, expected=["产物 文件.txt"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    s = summary_of(out)
    assert s["usage"]["input"] == 100, "同一 responseId 的 usage 不得重复累计"
    assert s["usageDuplicatesSkipped"] == 1


def test_chinese_space_paths_and_real_workdir(mock_pi, tmp_path):
    mock_pi.env["MOCK_SCENARIO"] = "chinese_spaces"
    workdir, task = make_workdir(tmp_path, name="中文 工作 目录")
    out = tmp_path / "输出 目录"
    proc = run_runner(mock_pi, task, workdir, out, expected=["子 目录/产出 文件.txt"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    s = summary_of(out)
    assert s["failed"] is False
    assert os.path.normcase(s["workDir"]) == os.path.normcase(str(workdir))
    art = s["expectedOutputs"][0]
    assert art["ok"] is True and art["changed"] is True
    assert (workdir / "子 目录" / "产出 文件.txt").read_text(encoding="utf-8") == "中文产物内容"
    receipt = json.loads(mock_pi.receipt.read_text(encoding="utf-8"))
    assert os.path.normcase(receipt["cwd"]) == os.path.normcase(str(workdir)), "pi 必须在实际 WorkDir 下运行"
    argv = receipt["argv"]
    assert "--thinking" in argv and "max" in argv
    assert any(a.startswith("@") and "任务 文件.md" in a for a in argv), "任务文件应以 @文件 传入"


def test_corrupted_log_fails(mock_pi, tmp_path):
    mock_pi.env["MOCK_SCENARIO"] = "corrupted_log"
    workdir, task = make_workdir(tmp_path)
    out = tmp_path / "out"
    proc = run_runner(mock_pi, task, workdir, out, expected=["产物 文件.txt"])
    assert proc.returncode == 1
    s = summary_of(out)
    assert s["jsonParseErrors"] >= 1
    assert any("JSON 解析失败" in r for r in s["failReasons"])


def test_tool_error_keeps_short_summary_only(mock_pi, tmp_path):
    mock_pi.env["MOCK_SCENARIO"] = "tool_error_huge"
    workdir, task = make_workdir(tmp_path)
    out = tmp_path / "out"
    proc = run_runner(mock_pi, task, workdir, out, expected=["产物 文件.txt"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    s = summary_of(out)
    assert len(s["toolErrors"]) == 1
    entry = s["toolErrors"][0]
    assert entry.startswith("read:")
    assert "读取失败" in entry
    assert "BBBB" not in entry, "工具错误摘要不得复制完整结果/图像"
    assert len(entry) <= 260


def test_output_dir_refuses_overwrite(mock_pi, tmp_path):
    mock_pi.env["MOCK_SCENARIO"] = "output_overwrite"
    workdir, task = make_workdir(tmp_path)
    out = tmp_path / "out"
    proc = run_runner(mock_pi, task, workdir, out, expected=["产物 文件.txt"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    first = (out / "summary.json").read_text(encoding="utf-8-sig")

    proc2 = run_runner(mock_pi, task, workdir, out, expected=["产物 文件.txt"])
    assert proc2.returncode == 2
    assert (out / "summary.json").read_text(encoding="utf-8-sig") == first, "拒绝覆写时不得改动既有汇总"


def test_expected_output_link_before_run_rejected(mock_pi, tmp_path):
    workdir, task = make_workdir(tmp_path)
    outside = tmp_path / "外部 目标"
    outside.mkdir()
    (outside / "借道 产物.txt").write_text("外部内容", encoding="utf-8")
    err = _try_create_dir_link(workdir / "链接 目录", outside)
    if err:
        pytest.skip(f"当前环境无法创建符号链接/junction：{err}")
    out = tmp_path / "out-link-before"
    proc = run_runner(mock_pi, task, workdir, out, expected=["链接 目录/借道 产物.txt"])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not mock_pi.receipt.exists(), "链接越界必须在调用 pi 之前被拒绝"
    assert not (out / "summary.json").exists()
    assert (outside / "借道 产物.txt").read_text(encoding="utf-8") == "外部内容"


def test_expected_output_link_created_during_run_rejected_after(mock_pi, tmp_path):
    probe_err = _try_create_dir_link(tmp_path / "链接探测", tmp_path)
    if probe_err:
        pytest.skip(f"当前环境无法创建符号链接/junction：{probe_err}")
    mock_pi.env["MOCK_SCENARIO"] = "link_escape_after"
    mock_pi.env["MOCK_LINK_STATUS"] = str(tmp_path / "link-status.txt")
    workdir, task = make_workdir(tmp_path)
    out = tmp_path / "out-link-after"
    proc = run_runner(mock_pi, task, workdir, out, expected=["链接 目录/借道 产物.txt"])
    status = (tmp_path / "link-status.txt").read_text(encoding="utf-8").strip()
    if status != "ok":
        pytest.skip(f"mock 运行期无法创建符号链接/junction：{status or '(空)'}")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    s = summary_of(out)
    assert s["failed"] is True
    art = s["expectedOutputs"][0]
    assert art["ok"] is False
    assert "重解析" in art["failReason"] or "链接" in art["failReason"] or "junction" in art["failReason"]
    assert art["sha256After"] is None, "拒绝链接后不得读取外部目标计算哈希"
    assert (tmp_path / "链接越界 目标" / "借道 产物.txt").read_text(encoding="utf-8") == "外部目标内容，不应被读取"


def test_final_stop_reason_null_or_unknown_rejected(mock_pi, tmp_path):
    workdir, task = make_workdir(tmp_path)

    mock_pi.env["MOCK_SCENARIO"] = "null_stop_reason"
    out1 = tmp_path / "out-null-stop"
    proc1 = run_runner(mock_pi, task, workdir, out1)
    assert proc1.returncode == 1, proc1.stdout + proc1.stderr
    s1 = summary_of(out1)
    assert s1["failed"] is True
    assert not s1["lastAssistantStopReason"], "stopReason 缺失时应记录为空"
    assert any("stopReason" in r for r in s1["failReasons"])

    mock_pi.env["MOCK_SCENARIO"] = "unknown_stop_reason"
    out2 = tmp_path / "out-unknown-stop"
    proc2 = run_runner(mock_pi, task, workdir, out2)
    assert proc2.returncode == 1, proc2.stdout + proc2.stderr
    s2 = summary_of(out2)
    assert s2["failed"] is True
    assert s2["lastAssistantStopReason"] == "weirdEnum"
    assert any("stopReason" in r for r in s2["failReasons"])


@pytest.mark.skipif(os.name != "nt", reason="mock 用 msvcrt.locking 模拟哈希读取异常；非 Windows 平台显式跳过")
def test_hash_read_failure_rejects_artifact(mock_pi, tmp_path):
    mock_pi.env["MOCK_SCENARIO"] = "hash_read_error"
    mock_pi.env["MOCK_LOCK_PID"] = str(tmp_path / "lock.pid")
    workdir, task = make_workdir(tmp_path)
    out = tmp_path / "out-hash-lock"
    holder_pid = None
    try:
        proc = run_runner(mock_pi, task, workdir, out, expected=["产物 文件.txt"], allow_unchanged=True)
        pid_file = tmp_path / "lock.pid"
        if pid_file.exists():
            holder_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert proc.returncode == 1, proc.stdout + proc.stderr
        s = summary_of(out)
        assert s["failed"] is True
        art = s["expectedOutputs"][0]
        assert art["ok"] is False
        assert art["sha256After"] is None
        assert "SHA256" in art["failReason"]
        assert s["allowUnchanged"] is True, "AllowUnchanged 不得豁免 SHA256 可计算要求"
    finally:
        if holder_pid:
            try:
                os.kill(holder_pid, signal.SIGTERM)
            except OSError:
                pass
