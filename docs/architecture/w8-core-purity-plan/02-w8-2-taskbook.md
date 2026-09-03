# W8-2 任务书:模型目录下沉 providers,消除 6 处反向导入

> 性质:纯搬家(git mv 整文件)+ import 改写 + 新增守护规则,零逻辑改动
> 规模:~10 文件;预计 25-35 分钟(含全量 pytest)

## 0. 背景与裁决

`cli/models.py`(1101 loc)是自包含的模型元数据目录服务(仅 1 处惰性
`config.paths` 导入,providers→config 为合法方向),却被下层反向消费:

| 消费方 | 行 | 形态 |
|---|---|---|
| providers/factory.py | 226 | 惰性(sink 包咬入口层,最严重) |
| agent/loop.py | 446/452 | 惰性 ×2 |
| agent/_provider_switching.py | 53 | 惰性 |
| agent/model_presets.py | 56 | 惰性 |
| webui/model_settings_api.py | 102/135/150 | 惰性 ×3(含 3 个私有函数) |
| cli/onboard.py | 19-23 | 顶层(入口层内部,合法但随搬家改指向) |

**裁决:整文件 git mv 至 `miniunicorn/providers/model_catalog.py`,cli/models.py 删除。**
理由:模型 ID/上下文窗口/HF-ModelScope 查询是 provider 层知识;文件尾部两个 CLI 面
函数中 `get_model_suggestions` 是恒返 `[]` 的空桩(内建表已删)、`format_token_count`
是 3 行格式化,均仅 onboard 消费,不构成保留残壳的理由。

## 1. 红线

1. 只用 `git mv`;函数体一字不动(含 docstring)
2. 旧路径 `miniunicorn.cli.models` 彻底消失,不留 shim/re-export
3. 新增守护规则只禁不豁免;全绿后才允许加
4. 历史文档不动;新写文档不出现裸 legacy journal 文件名
5. 验证门一过立即 commit

## 2. 手术清单

### 2.1 搬家(1 文件)

```powershell
git mv miniunicorn/cli/models.py miniunicorn/providers/model_catalog.py
```

### 2.2 消费方改指向(7 文件)

| 文件 | 行 | 改写 |
|---|---|---|
| agent/loop.py | 446/452 | `from miniunicorn.cli.models import ...` 改为 `from miniunicorn.providers.model_catalog import ...` |
| agent/_provider_switching.py | 53 | 同上 |
| agent/model_presets.py | 56 | 同上 |
| providers/factory.py | 226 | 同上 |
| webui/model_settings_api.py | 102/135/150 | 同上(保持原有私有函数导入) |
| cli/onboard.py | 19-23 | 三符号全部改指 `miniunicorn.providers.model_catalog` |
| config/schema.py | 130/194 | **仅注释**:把 `cli.models.get_model_context_limit` 字样更新为 `providers.model_catalog.get_model_context_limit` |
| tests/conftest.py | 26 | `from miniunicorn.cli import models as models_module` 改为 `from miniunicorn.providers import model_catalog as models_module`(变量名保留以最小化 diff) |

### 2.3 新增守护规则(1 文件)

`tests/architecture/test_dependency_direction.py` 新增测试函数
`test_base_layer_does_not_import_cli`:对 sink 包集合
(providers, utils, security, config, bus, ledger, memory, tools)AST 扫描,
禁止 `from miniunicorn.cli...` / `import miniunicorn.cli...` 两种形式
(**注意匹配全限定前缀**——W8-1 刚修过裸形式盲区,别再犯)。
风格参照文件内既有 sink 守护(六包禁 agent 规则)。

### 2.4 文档(1 文件)

`docs/architecture/module-boundaries.md`:
- cli 小节模块清单若有 models.py 则移除
- providers 小节登记 `model_catalog.py`(自 cli/models.py 归位,W8-2,与 McpRuntime 同款格式)
- 注明新守护:sink 八包禁 import cli

## 3. 验证门

```powershell
# 门 1:零残留(注意 | 在 powershell 会解析为管道,用多 -e 或引号)
rg -n -e "cli\.models" -e "cli/models" -e "cli import models" miniunicorn/ tests/
# 期望零命中(退出码 1)

# 门 2:守护
.venv\Scripts\python.exe -m pytest tests/architecture/test_dependency_direction.py -q
# 期望 5 passed(4 旧 + 1 新)

# 门 3:冷导入
.venv\Scripts\python.exe -c "import miniunicorn.agent, miniunicorn.providers.model_catalog; print('ok')"

# 门 4:相关测试(conftest 装置路径 + 工厂)
.venv\Scripts\python.exe -m pytest tests/agent/test_loop_init_phase_split.py tests/providers/test_custom_provider.py -q

# 门 5:全量(后台 Start-Process + 轮询变体)
.venv\Scripts\python.exe -m pytest tests/ -q
# 期望 4147 passed / 0 failed / 29 skipped(4146 基线 + 1 新守护测试)

# 门 6:双 ruff 零 + 历史追踪
.venv\Scripts\python.exe -m ruff check miniunicorn/ tests/
.venv\Scripts\python.exe -m ruff format --check miniunicorn/ tests/
git log --follow --oneline miniunicorn/providers/model_catalog.py   # 期望多条
```

## 4. 提交

```
refactor(providers): sink model catalog from cli entry layer

Whole-file move of cli/models.py (1101 loc, self-contained model metadata
service: context-limit resolution, HF/ModelScope auto-lookup, learning
table) to providers/model_catalog.py. Closes 6 reverse imports from
providers/agent/webui into the cli entry layer. New guard: sink packages
(eight) must not import cli.
```
