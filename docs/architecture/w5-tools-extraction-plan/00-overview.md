# W5:工具库外置(agent/tools → miniunicorn/tools)

> 系列目的:完成 W0 时推迟的"Agent Core + Tool Library"拆分的最后一步——把 `miniunicorn/agent/tools/`(55 文件)物理外置为顶层包 `miniunicorn/tools/`,实现真正的"核心 + 库"形态。
> 本方案取代 `docs/architecture/agent-core-tool-library-split-plan.md`(W0 时代的完整设想,其核心思想由本方案以更小步幅落地;该旧文档保留为历史记录)。
> 基线:HEAD `a7763b8f`,全量测试 **4129 passed / 0 failed**,工作区干净。

## 一、现状侦察结论(2026-09-01,全部程序化核实)

| 侦察项 | 结论 |
|---|---|
| 工具包规模 | 55 个 .py 文件(含 3 个子包 web_search/deep_research/image_generation),约 11000 行 |
| 包内导入风格 | **全部绝对导入**(`from miniunicorn.agent.tools.X import ...`),零相对导入——重写为纯前缀字符串替换 |
| 外部消费者 | 约 20 个源码文件(agent 核心 13、channels 1、cli 1、composition 2、config 1、webui 2) |
| 测试引用面 | 63 个测试文件;其中约 70 处 `patch()/monkeypatch.setattr()` **字符串目标**(如 `"miniunicorn.agent.tools.shell._IS_WINDOWS"`) |
| 字符串字面量路径 | `config/schema.py` 8 处 `_lazy_default("miniunicorn.agent.tools.web", ...)` 懒加载默认值——**纯 import 重写会漏掉** |
| loader 动态发现 | 基于 `self._package.__name__`/`__path__`(相对导入 `f".{module_name}"`),移包后自适应;仅 `loader.py:35` 默认包导入需改写;插件 entry_points 组名 `miniunicorn.tools` 与新命名空间天然一致 |
| 顶层名占用 | `miniunicorn/tools` 未被占用;pyproject 打包为扁平 `packages=["miniunicorn"]`,子包自动包含,零打包改动 |
| Rule C 私有访问 | 移动后模拟扫描:零新增跨包下划线私有访问(唯一命中为已豁免的 composition/gateway 项) |
| Rule B 守护豁免 | `("channels/websocket/channel", "miniunicorn.agent.tools.mcp")` 移动后自然失效——channels 不再依赖 agent,**豁免条目删除即收益** |
| 循环导入风险 | **存在,需前置批解除**:`message.py` 与 `self.py` 运行时导入 `agent.safety_policy`,而二者均在 `agent/__init__` 导入闭包内(经 loop→turn_orchestrator/response、_mcp_lifecycle)。外置后冷启动 `import miniunicorn.tools.message` 会触发部分初始化 ImportError |
| 注释路径残留 | 2 处注释引用旧路径(web_search_api.py:23、circuit_breaker.py:3),随批更新 |

## 二、关键裁决

| # | 候选 | 裁决 | 理由 |
|---|---|---|---|
| 1 | 外置为顶层平铺包 `miniunicorn/tools/` | **采纳** | 与 memory_*/providers/channels 等既有平铺家族一致;名字已被 loader 插件组使用 |
| 2 | `agent/tools/` 子包目录形式保留 | 否决 | 即现状,与"核心+库"目标不符 |
| 3 | 保留 `miniunicorn.agent.tools` 兼容门面 | **否决** | 深路径面太大(约 84 文件、70+ 字符串目标);保深路径需 sys.modules 注册式 hack,违背可维护性。本方案不删除任何既有 re-export(`agent/tools/__init__.py` 原样搬走),与"禁止删除 compat re-export"教训不冲突——那是关于保留既有 re-export 完整性,不是要求为搬家新增门面 |
| 4 | 消费者 import 全量重写(`miniunicorn.agent.tools` → `miniunicorn.tools`) | **采纳** | 单一前缀替换,rg 可证零残留;终态无僵尸命名空间 |
| 5 | RiskLevel 下沉 `miniunicorn/security/risk.py` | **采纳**(前置批 W5-0) | 解除循环导入阻塞的最低成本路径:security 是空 `__init__`、零 miniunicorn 依赖的纯叶子包;RiskLevel 是 10 行 str-Enum;safety_policy re-export 保 17 个既有消费者零改动 |
| 6 | 反转 core→tools 依赖(loop/turn_orchestrator/runner 对 registry/MessageTool/FileStateStore 的 5 处点导入改注入) | **否决,记录** | 依赖注入缝属新抽象,违背 W2"抽象税"裁决;core 引用库的 registry 类型是务实的 stdlib 式用法。tools→agent 的治理词汇依赖(RiskLevel/call_purpose/Plan)是声明方向:库依赖核心词汇,合法且无环(W5-0 后) |
| 7 | tests/agent/tools/ 测试目录随源码迁移 | 否决 | 测试按导入路径耦合,不按目录;只重写 import/patch 路径,文件不动 |
| 8 | docs 历史规划文档同步改写 | 否决 | 历史文档记录当时状态,不改;仅 module-boundaries.md(守护测试引用的活文档)同步豁免清单 |

## 三、批次索引

| 批次 | 内容 | 依赖 |
|---|---|---|
| W5-0 | RiskLevel 下沉 security/risk.py + message/self 改导入源 | 无 |
| W5-1 | `git mv agent/tools → tools` + 全量前缀重写 + 守护豁免删除 + module-boundaries.md 同步 + 零残留扫描测试 + 冷导入回归测试 | W5-0 已合并 |

W5-1 不可再拆:目录移动是原子操作,半程状态导入图必断。

## 四、全局红线

1. **纯搬家**:包内 55 文件除 import 前缀外零改动;loader 的 `_SKIP_MODULES`、发现顺序、注册逻辑不动
2. 消费者只改 import/字符串路径,逻辑零改动
3. 守护测试仅授权 W5-1 删除豁免条目 `("channels/websocket/channel", "miniunicorn.agent.tools.mcp")` 一项;任何其他守护失败=停下报告
4. 测试仅改 import/patch 路径与字符串目标,断言零修改
5. 不删任何既有 re-export;不引入新抽象;不"修复"任何顺手发现的问题
6. `git mv` 保证历史可追溯;禁止 copy+delete
7. 测试必须用 `.venv\Scripts\python.exe`(3.12);系统 Python 3.10 缺 typing.Self 会在收集阶段报 14 个 ImportError
8. 每批一 commit,验证门一过立即提交(先提交后写报告)

## 五、验证门

- 全量 pytest **0 failed**(基线 4129 + 各批新增)
- ruff check / format --check 零告警
- W5-1 追加:`rg "miniunicorn\.agent\.tools" miniunicorn/ tests/` 零命中(以测试形式固化)
