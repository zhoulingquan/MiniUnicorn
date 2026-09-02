"""W6-1b 回归测试:agent/call_ledger + turn_budget → miniunicorn/ledger 外置(纯搬家)验收。

固化三项验收:
1. 冷导入 `miniunicorn.ledger` 后 sys.modules 不含任何 `miniunicorn.agent` 模块;
2. `__init__` 门面 re-export 9 个公共名完整;
3. `miniunicorn.ledger.turn_budget` 可独立导入且不拉入 agent。
"""

from __future__ import annotations

import subprocess
import sys

PUBLIC_NAMES = (
    "CallPurpose",
    "CallRecord",
    "CallLedger",
    "TurnBudget",
    "current_call_ledger",
    "bind_call_ledger",
    "reset_call_ledger",
    "call_purpose",
    "allow_call_ledger_child_tasks",
)

# 拼接构造,避免本测试源文件自身命中被扫描的字符串
_AGENT_PREFIX = "miniunicorn" + ".agent" + "."


def test_cold_import_package_does_not_pull_agent() -> None:
    code = (
        "import sys, json; import miniunicorn.ledger; "
        f"print(json.dumps(sorted(m for m in sys.modules if m == '{_AGENT_PREFIX[:-1]}' or m.startswith('{_AGENT_PREFIX}'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_all_public_names_reexported() -> None:
    import miniunicorn.ledger as ledger

    for name in PUBLIC_NAMES:
        assert hasattr(ledger, name), name


def test_turn_budget_imports_standalone() -> None:
    code = (
        "import sys, json; import miniunicorn.ledger.turn_budget; "
        f"print(json.dumps(sorted(m for m in sys.modules if m == '{_AGENT_PREFIX[:-1]}' or m.startswith('{_AGENT_PREFIX}'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
