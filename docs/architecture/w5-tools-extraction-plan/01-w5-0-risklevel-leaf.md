# W5-0:RiskLevel 下沉 security/risk.py(工具库外置前置批)

> 前置依赖:HEAD `a7763b8f`(4129 passed)。本批为 W5-1 解除循环导入阻塞,自身独立可验证。
> 行号锚点按当前代码;**行号为参考值,定位以符号为准**。

## 一、为什么需要本批(背景,Cline 必读)

W5-1 将把 `miniunicorn/agent/tools/` 外置为顶层包 `miniunicorn/tools/`。已程序化核实:**`message.py` 与 `self.py` 运行时导入 `miniunicorn.agent.safety_policy`,而二者均在 `agent/__init__.py` 的传递导入闭包内**(agent/__init__ → loop → turn_orchestrator/response → tools.message;agent/__init__ → … → _mcp_lifecycle → tools.self)。

外置后,冷启动 `import miniunicorn.tools.message` 会触发:tools.message(部分初始化)→ agent.safety_policy → `agent/__init__.py` → … → `from miniunicorn.tools.message import MessageTool` → **ImportError: cannot import name from partially initialized module**。

解法:把 RiskLevel(10 行 str-Enum)下沉到 `miniunicorn/security/risk.py`——security 是空 `__init__`、零 miniunicorn 依赖的纯叶子包,tools 引用它不触发 agent/__init__,环即断。safety_policy re-export 使全部既有消费者零改动。

## 二、现状锚点

| 锚点 | 行号(参考) | 说明 |
|---|---|---|
| `class RiskLevel(str, Enum)` | safety_policy.py 13-16 | `LOW="low"`、`MEDIUM="medium"`、`HIGH="high"` |
| safety_policy 其余内容 | 同文件 | SafetyVerdict、SafetyPolicy 类——**不动** |
| message.py 导入 | 7 | `from miniunicorn.agent.safety_policy import RiskLevel`;RiskLevel 用于 152-153 行 `risk_level` property 注解与返回值(方法级,非类体) |
| self.py 导入 | 10 | 同上;用于 240-241 行 `risk_level` property |
| security 包 | — | `__init__.py` 为空;network.py / workspace_access.py / workspace_policy.py 零 miniunicorn 导入 |

## 三、变更方案

1. 新建 `miniunicorn/security/risk.py`:

```python
"""Risk vocabulary shared by agent core and the tools library.

Standalone leaf on purpose: importing it must not trigger
``miniunicorn.agent`` package init (import-order safety for the tools
library, see docs/architecture/w5-tools-extraction-plan/).
"""

from __future__ import annotations

from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
```

2. `miniunicorn/agent/safety_policy.py`:删除 13-16 行类定义;在 import 区加 `from miniunicorn.security.risk import RiskLevel`——**RiskLevel 经此 re-export 保持从 safety_policy 可导入**(全库 17 个 `from miniunicorn.agent.safety_policy import ...` 消费者零改动;SafetyVerdict/SafetyPolicy 引用 RiskLevel 的代码零改动)。
3. `miniunicorn/agent/tools/message.py:7` → `from miniunicorn.security.risk import RiskLevel`
4. `miniunicorn/agent/tools/self.py:10` → 同上

**禁止**:改 SafetyVerdict/SafetyPolicy;改其他 tools 文件的 safety_policy 导入(它们不在 agent/__init__ 闭包内,无环,统一性留待 W5-1 后按需处理);在 risk.py 加任何 miniunicorn 导入;动 safety_policy.py 的 TYPE_CHECKING 块。

## 四、新测试(新建 `tests/security/test_risk_level.py`)

1. `test_risklevel_identity_compatibility`:`from miniunicorn.agent.safety_policy import RiskLevel as A`、`from miniunicorn.security.risk import RiskLevel as B`,断言 `A is B`,且三枚举值字符串分别为 "low"/"medium"/"high"(防漂移)
2. `test_risk_module_is_leaf`:读取 `miniunicorn/security/risk.py` 源码文本,断言不含 `"from miniunicorn"` 与 `"import miniunicorn"`(以 `"miniunicorn" + ".agent"` 拼接方式在断言中引用,防测试文本误匹配自身)——固化叶子属性

## 五、验收清单

- [ ] 全量测试绿(passed ≥ 4129 + 新增 2)且既有断言零修改;ruff 零告警
- [ ] `from miniunicorn.agent.safety_policy import RiskLevel` 与 `from miniunicorn.security.risk import RiskLevel` 同一对象
- [ ] message.py / self.py 不再导入 safety_policy;tools 其余文件未动
- [ ] safety_policy.py 中 SafetyVerdict / SafetyPolicy 逻辑零改动(diff 仅删类定义 + 加一行 import)
