# 历史 lint 债清理(单批)

> 状态:任务书已备,待执行。
> 基线:HEAD `7a83f6b4`,全量测试 **4135 passed / 0 failed / 29 skipped**。
> ruff 版本:0.15.21(与 uv.lock 一致;2026-09-01 已将 .venv 从漂移的 0.16.0 对齐回来,后续验证一律用 `.venv\Scripts\python.exe -m ruff`)。

## 一、债务清单(2026-09-01 程序化核实)

### A. check 错误 16 个(全部在 tests/,3 个文件)

| 文件 | 数量 | 明细 |
|---|---|---|
| `tests/agent/test_prompt_telemetry_consumer.py` | 12 | I001 x3(行 6/78/145)、F401 x5(typing.Any、unittest.mock.patch、loguru.logger、ToolCallRequest、sys)、F811 x2(行 78/145 的 logger 局部导入遮蔽行 12 未用的模块级导入)、F841 x1(行 122)、W292 x1(行 171 缺文件尾换行) |
| `tests/agent/test_step_acceptance_verifier.py` | 3 | F401 x3(行 18:39 Plan、18:55 StepStatus、19:69 StepEvidence) |
| `tests/agent/test_tool_receipts.py` | 1 | F401 x1(行 11 `import json`) |

### B. format 待重排 52 个文件(全部在 tests/,零生产代码)

清单见 `ruff format --check miniunicorn/ tests/` 输出;典型:`tests/agent/test_call_ledger.py`、`tests/agent/bench_schema_crop.py`、`tests/architecture/test_dependency_direction.py`、`tests/tools/test_tool_validation.py` 等(52 个均为 tests/ 路径)。

## 二、红线

1. **只动 tests/**:16 个错误与 52 个 format 文件全部位于 tests/ 下;若修复后 `git status` 出现任何 miniunicorn/ 路径的改动,立即停下报告
2. **纯机械修复**:只用 `ruff check --fix`(安全修复)+ 一处手工编辑 + `ruff format`;零逻辑改动、零断言改动、零删测试
3. **F841 手工修复**(行 122,唯一非自动项):`result = await runner.run(_spec(context_window_tokens=None))` → 去掉 `result = ` 赋值、保留 await 调用(调用有副作用,不能整行删)。禁止使用 `--unsafe-fixes` 批量跑
4. **F811 修复语义**:ruff 删除的是行 12 模块级未用的 `from loguru import logger`;行 78/145 的函数内局部导入保留(ruff 顺带会重排这几个局部导入块)
5. 不新增测试、不改 pyproject.toml 的 ruff 配置、不升级 ruff 版本
6. 修复顺序:`ruff check tests/ --fix` → 手工 F841 → `ruff format miniunicorn/ tests/`(全树跑,只重写需要重排的文件)→ 验证门
7. 测试必须用 `.venv\Scripts\python.exe`(3.12);系统默认 Python 3.10 会在收集阶段报 14 个 ImportError

## 三、验证门(全过才允许 commit)

1. `.venv\Scripts\python.exe -m pytest tests/ -q` → **4135 passed / 0 failed**(数量必须与基线一致,不增不减——本批不新增测试)
2. `.venv\Scripts\python.exe -m ruff check miniunicorn/ tests/` → 零输出
3. `.venv\Scripts\python.exe -m ruff format --check miniunicorn/ tests/` → 零输出
4. 若个别测试失败:先单独重跑确认是否 flaky;若 format 后出现真实失败,回滚该文件(`git checkout -- <file>`)并停下报告,不许为过测试改断言

## 四、Git 纪律

- 一个 commit;`git add tests/`(改动应全部在 tests/);绝不 `git add .`
- commit message:`chore(lint): clear historical ruff debt in tests (16 errors + 52 format files)`
- 验证门一过立即提交,先提交后写报告

## 五、中断防护(必守)

- 全量 pytest 约 7-8 分钟:用 `Start-Process` 分离式后台启动并重定向日志,随后轮询
- **每次轮询命令必须互不相同**(交替 `-Tail 2`/`-Tail 5`、或命令里带递增计数器/时间戳):会话在"相同工具名+相同参数连续 5 次"时被强制中止,原样重复同一条轮询命令必死
- 单条 shell 命令控制在 25 秒内返回
