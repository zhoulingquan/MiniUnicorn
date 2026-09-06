# W8 阶段:核心纯化——依赖方向收口

> 日期:2026-09-02 · 前置:W7 收官(agent 45 文件/12661 loc 纯编排核心、tools/memory/ledger 三库外置)
> 数据来源:程序化勘察(corrected AST 扫描 + 运行时构造点定位 + 守护测试源码核查)

## 背景与勘察结论

W5–W7 完成了"库外置"(tools/memory/ledger),agent 核心已是纯编排。但勘察发现两类残留:

### 发现一:composition 守护存在盲区,且有两处真实违规

`test_business_modules_do_not_import_composition` 的匹配逻辑是
`target.split(".")[0] == "composition"`——只匹配裸 `import composition.X`,
**漏掉 `from erza.composition.X import Y` 全限定形式**。
module-boundaries.md §1.1 第一行声称"✅ 现状无",实为守护失明。

修正扫描后的真实违规清单(全仓仅 2 处,均在 agent):

| 文件 | 行 | 内容 |
|---|---|---|
| agent/loop.py | 49 | 顶层 import McpRuntime;**582 运行时构造** `cfg.mcp_runtime or McpRuntime(...)` |
| agent/loop_builder.py | 39 | 顶层 import McpRuntime(仅类型标注) |

(cli/commands.py、cli/_gateway_runner.py 也 import composition,但 cli 是入口层,不受限,合法。)

**根因**:`composition/mcp_runtime.py` 本身就不是装配代码——56 loc,依赖只有
`tools.registry.ToolRegistry` 与 `tools.mcp.connect_missing_servers`,是纯库代码
寄居在装配层。归位到 tools 后,agent→tools 即为既有的合法方向。

### 发现二:模型元数据目录寄居 cli 入口层,造成 5 处反向导入

`cli/models.py`(956 loc)内含 HF/ModelScope 网络自动查询 + 学习表的模型目录服务,
其中 `get_model_context_limit` / `DEFAULT_CONTEXT_LIMIT` 被下层反向消费:

| 消费方 | 层级 | 位置 |
|---|---|---|
| providers/factory.py | **基础层 → 入口层** | 226(惰性)——最严重,sink 包竟咬 cli |
| agent/loop.py | 业务 → 入口层 | 446/452(惰性) |
| agent/_provider_switching.py | 业务 → 入口层 | 53(惰性) |
| agent/model_presets.py | 业务 → 入口层 | 56(惰性) |

webui/model_settings_api 与 `__main__.py` 是入口层消费,合法。

### 附带发现(不属 W8,记录备查)

- runner.py 1735 loc、loop.py 1483 loc:核心两个最大文件,拆分需专项评估(拟 W8-3)
- command 横向耦合(loop.py:379/381、dispatch.py:114 构造 CommandRouter/CommandApplicationService):
  command 与 agent 同层,不违反分层规则,仅是横向便利构造,不动

## 批次划分

### W8-1:守护修复 + McpRuntime 归位(本批,小而锋利)

纯搬家 + 守护修复,零逻辑改动,爆炸半径 ~8 文件。详见 `01-w8-1-taskbook.md`。

### W8-2:模型目录下沉(下一批,先专项勘察)

把 `get_model_context_limit` / `DEFAULT_CONTEXT_LIMIT` 及其支撑数据从 cli/models.py
下沉至 providers 层(模型元数据是 provider 层知识)。勘察要点:

1. cli/models.py 956 loc 的内部分区:目录数据/查询函数 vs CLI 交互面(需精确切割线)
2. 候选归属:providers(自然)vs 独立顶层 model_catalog(若网络查询+持久化学习表过重)
3. cli/models.py 保留 CLI 面,re-export 下沉部分(webui 可直连新家)
4. 收尾:新增基础层禁 import cli 守护规则(此时全绿才可加)

### W8-3:runner.py 拆分评估(暂缓,触发条件出现再做)

1735 loc。可见缝:safety 违规分类(SSRF/workspace,1592–1656)、usage 记账
(1439–1451)、消息/历史工具(1671–1735)、注入排水(292–475)。按反抽象税原则,
只有缝两侧真正内聚才动刀;先评估后立项。

## W8-1 任务书

见 `01-w8-1-taskbook.md`。
