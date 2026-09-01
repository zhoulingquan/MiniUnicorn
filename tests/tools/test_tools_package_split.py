"""W5-1 回归测试:agent/tools → miniunicorn/tools 外置(纯搬家)验收。

固化四项验收:
1. 包位置(旧路径不存在、新路径存在);
2. 零残留(源码树下不再出现旧 dotted 路径);
3. 冷导入回归(message/self 作为进程首批 miniunicorn 导入,W5-0 循环解除防护);
4. 门面 re-export 身份(ToolRegistry 经新路径导入与 registry 模块同一对象)。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import miniunicorn

ROOT = Path(miniunicorn.__file__).parent

# 拼接构造,避免本测试源文件自身命中被扫描的字符串
_LEGACY_DOTTED = "miniunicorn" + ".agent" + ".tools"


def test_tools_package_location() -> None:
    assert not (ROOT / "agent" / "tools").exists()
    assert (ROOT / "tools" / "__init__.py").is_file()


def test_no_legacy_agent_tools_references() -> None:
    hits: list[str] = []
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if _LEGACY_DOTTED in path.read_text(encoding="utf-8"):
            hits.append(str(path))
    assert hits == []


def test_cold_import_regression() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import miniunicorn.tools.message; import miniunicorn.tools.self"],
        check=False,
    )
    assert result.returncode == 0


def test_registry_identity_via_new_path() -> None:
    from miniunicorn.tools import ToolRegistry, registry

    assert registry.ToolRegistry is ToolRegistry
