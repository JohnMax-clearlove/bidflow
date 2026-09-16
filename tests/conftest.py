"""测试环境适配：受限环境下无法捕获子进程输出时，明确跳过而不是报错。

部分环境（容器、Windows 沙箱）禁止进程建立管道，`subprocess.run(..., capture_output=True)`
会直接抛 `PermissionError: [WinError 5]`。这不是被测代码的问题，也无法在进程内绕过，
因此对确实需要捕获子进程输出的用例给出显式跳过原因，避免把环境限制记成代码缺陷。

其它情况不使用任何 fixture，测试保持原样。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

PIPE_REASON = "当前环境不允许建立子进程管道（capture_output 不可用），无法捕获子进程输出"


def _pipes_available() -> bool:
    try:
        subprocess.run([sys.executable, "-c", "pass"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except PermissionError:
        return False
    return True


@pytest.fixture(scope="session")
def piped_subprocess() -> bool:
    """供需要捕获子进程输出的用例声明依赖；不可用时用例自动跳过。"""
    available = _pipes_available()
    if not available:
        pytest.skip(PIPE_REASON)
    return available
