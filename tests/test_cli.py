"""已安装命令入口的中文JSON行为测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def invoke(*args, cwd=None):
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run([sys.executable, "-m", "bidflow", *map(str, args)], cwd=cwd, env=environment, capture_output=True, text=True, encoding="utf-8")
    stream = result.stdout if result.returncode == 0 else result.stderr
    return result, json.loads(stream)


def test_doctor_and_project_cli_round_trip(tmp_path: Path, piped_subprocess):
    result, payload = invoke("doctor")
    assert result.returncode == 0 and payload["ok"]
    assert payload["result"]["model_api_required"] is False
    project = tmp_path / "项目"
    result, payload = invoke("init", "命令入口测试", "--path", project)
    assert result.returncode == 0 and project.joinpath("项目状态.md").is_file()
    result, payload = invoke("next", "--project", project)
    assert payload["result"]["next"] and payload["result"]["acceptance_status"] == "待实标验收"
    source = project / "01_输入文件" / "01_招标文件" / "测试.md"
    source.write_text("资格条件和技术评分。", encoding="utf-8")
    result, payload = invoke("ingest", "--scan", "--project", project)
    assert result.returncode == 0 and payload["result"]["results"]
    result, payload = invoke("task", "prepare", "analyze", "--project", project)
    assert payload["result"]["created"][0]["package"].endswith("context.json")
    result, payload = invoke("connector", "dingtalk", "项目经理业绩", "--project", project)
    assert payload["result"]["results"][0]["status"] == "manual_required"


def test_cli_errors_are_machine_readable_and_do_not_traceback(tmp_path: Path, piped_subprocess):
    project = tmp_path / "不存在"
    result, payload = invoke("status", "--project", project)
    assert result.returncode == 1 and payload["ok"] is False
    assert "Traceback" not in result.stderr
    assert payload["command"] == "status"
