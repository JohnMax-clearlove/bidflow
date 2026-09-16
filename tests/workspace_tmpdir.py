"""受限环境下让 pytest 的临时目录落在工作区内且保持可访问。

背景：本机受限环境（容器/沙箱）在目录被以 `mode=0o700` 创建并 chmod 之后会拒绝访问该目录，
`listdir`/`chmod` 都返回 PermissionError。pytest 的 tmpdir 插件正是用 `mkdir(mode=0o700)`
建立编号临时目录并随后 chmod，于是所有使用 `tmp_path` 的用例在 fixture 阶段直接报错，报错位置是
`tmpdir.py::_ensure_relative_to_basetemp → getbasetemp`。

本插件因此不使用 pytest 的编号临时目录：
1. basetemp 固定到仓库内（默认 `.bidflow-pytest-tmp`，可用 `BIDFLOW_PYTEST_BASETEMP` 覆盖）；
2. 用普通权限 `mkdir` 为每个用例建立独立目录，覆盖 `tmp_path` 与 `tmp_path_factory`。

使用：`python tests/run_tests.py`。
只改变临时目录位置和建立方式，不改变任何用例的断言。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

DEFAULT_BASETEMP = Path(__file__).resolve().parent.parent / ".bidflow-pytest-tmp"
_UNSAFE = re.compile(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+")


def _base() -> Path:
    return Path(os.environ.get("BIDFLOW_PYTEST_BASETEMP", DEFAULT_BASETEMP)).resolve()


def pytest_configure(config) -> None:
    base = _base()
    base.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(base)
    # 让直接调用 tempfile 的代码也落在工作区，避免写入不可用的系统临时目录。
    tempfile.tempdir = str(base)


def _make_dir(base: Path, name: str) -> Path:
    safe = _UNSAFE.sub("_", name).strip("_")[:60] or "tmp"
    root = base / safe
    suffix = 0
    while True:
        candidate = root if suffix == 0 else root.with_name(f"{root.name}-{suffix}")
        try:
            candidate.mkdir(parents=True)
            return candidate
        except FileExistsError:
            suffix += 1
            if suffix > 9999:
                raise


@pytest.fixture
def tmp_path(request) -> Path:
    """为每个用例建立独立临时目录（覆盖 pytest 内置实现）。"""
    return _make_dir(_base(), f"{request.node.nodeid}-{os.getpid()}")


@pytest.fixture(scope="session")
def tmp_path_factory():
    """在 tmp_path 之外仍需工厂接口时使用；返回普通对象，提供 mktemp。"""

    class Factory:
        def mktemp(self, basename: str = "tmp", numbered: bool = True) -> Path:
            return _make_dir(_base(), basename)

        def getbasetemp(self) -> Path:
            return _base()

    return Factory()
