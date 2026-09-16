"""在受限环境下运行完整测试套件。

用法：`python tests/run_tests.py [pytest 参数...]`

与直接执行 `python -m pytest` 的区别只有一个：自动加载 `workspace_tmpdir` 插件，
把临时目录放到仓库内并用普通权限目录实现 `tmp_path`，原因见该插件文档。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# 显式导入本仓库的插件，避免被环境中同名 tests 包遮蔽。
sys.path.insert(0, str(ROOT / "tests"))

if __name__ == "__main__":
    args = ["-q", "--tb=short", "-p", "no:cacheprovider", "-p", "workspace_tmpdir", *sys.argv[1:]]
    raise SystemExit(pytest.main(args))
