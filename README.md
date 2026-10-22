# SC2 RSI

面向持续代码进化的 StarCraft II 规则 Bot 研究项目。Bot 在游戏内按规则运行；RSI 在对局之间分析证据、修改候选代码并进行固定对照。

## 目录

```text
bot/                          # 模块化规则 Bot
  components/                 # behaviors / tasks / tactics，可编辑业务边界
  core/                       # 调用、单位归属与动作仲裁
  spatial/                    # 地图、路径与选址
  telemetry/                  # 原始游戏日志
rsi/
  run.py                      # 顺序执行、预算和阶段产物
  analyzer.py                 # 日志指标、事件与查询
  diagnosis/                  # 单会话诊断、竞争假说与证据更新
  editor.py                   # 候选代码修改
  evaluator.py                # 固定对照与版本判断
  tools/                      # 检索、对局、验证、快照、模型接入
  memory.py                   # 文件化经验记录
  config.json                 # RSI 预算、实验配置与验收规则
environment.json              # SC2 路径与版本
pyproject.toml                # Python 版本、依赖与安装配置
knowledge/                    # 游戏机制、单位、能力、升级与科技树
scripts/                      # 独立 Bot、环境检查、知识导出与测试
  test/                       # Bot / RSI 离线测试和手动 SC2 烟雾检查
run_iteration.py              # RSI 根目录入口，转调 rsi.run
runtime/                      # 运行产物
  bot/<id>/                   # 独立 Bot 对局
  rsi/versions/<hash>/        # 冻结 Bot 快照
  rsi/iterations/<id>/        # 配置、日志、分析、会话、候选、diff 与评估
  rsi/memory/                 # 历轮开发经验
  rsi/current_version.json    # 仅接受候选后更新
```

测试统一位于 `scripts/test/`；RSI 配置统一使用 `rsi/config.json`，环境配置保留在 `environment.json`。

## 环境准备

使用 Python 3.12，在项目根目录执行。已有 `sc2rsi` 环境可以直接复用：

```powershell
conda activate sc2rsi
python -m pip install -e .

# 首次配置：填写 .env 中的 LLM_MODEL、LLM_BASE_URL、LLM_API_KEY
Copy-Item .env.example .env
```

修改 `environment.json` 中的 SC2 路径与版本以匹配本机安装。环境检查入口为 `python -B scripts/check_environment.py`，不会启动对局或调用模型。

项目依赖统一写在 `pyproject.toml`，直接依赖固定版本；传递依赖由安装器解析。`.env`、编辑器设置、缓存和实验产物均由 `.gitignore` 排除。

## 运行

```powershell
conda activate sc2rsi

# 查看参数，不启动游戏或模型
python -B run_iteration.py --help

# 只采集、分析探索对局，不调用模型
python -B -m rsi.run --collect

# 完整迭代：启动对局并调用 .env 配置的模型
python -B run_iteration.py --mode iterate --collect --enable-model

# 分析已有探索批次，不启动游戏或模型
python -B -m rsi.run --reuse-batch runtime/rsi/iterations/<id>/batches/<batch>/batch.json

# 独立 Bot
python -B -m bot.run --time-limit 150

# 离线检查，不启动游戏或付费模型
python -B -m unittest discover -s scripts/test -v
```

- RSI 默认 `mode=analyze_only`、`formal=false`，小样本功能对照返回 `inconclusive`，不会晋升版本。
- `--config PATH` 指定实验配置；模型读取 `.env` 中的 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY`，不把密钥写入产物。
- 每批默认并行 5 局。日志复用 Bot Logger，Analyzer 查询为 `overview / metrics / events / trace / compare`；时间窗口使用游戏秒。
- Analyzer 默认返回摘要；`detail=raw` 获取指标采样和 Trace 状态快照，`offset` 翻页。查询中的长引用列表只返回代表引用，完整证据保留在分析产物中。
- 诊断工具直接接收结构化业务字段；`investigate.params` 仅用于具体查询参数。参数错误可直接修正，合法查询结果需附简短理由更新假说；引用和未知项累积保存。
- 效果检查区分状态统计与事件计数，并在诊断结论提交时检查探索数据可用性。可修改结论需指定 `hypothesis_id`，同时描述修改和应保留的行为。
- Editor 接收相关证据摘录和失败反馈；Memory 保存经验摘要及完整调查、diff、评估产物路径，不嵌套保存历轮查询内容。
- `iteration.json` 只保存运行摘要和产物路径，完整诊断、验证、评估保存在对应阶段文件。协调进程在不再查询后释放原始日志，诊断进程独立读取探索证据；磁盘日志及分析结果保留。
- 探索和评估每局最多 1800 游戏秒、600 现实秒；验证短局为 300 游戏秒、180 现实秒。对局为非实时运行，实际速度受机器负载与 Bot 单步开销影响。
- 一轮最多 24 局、7200 现实秒；模型累计输入与输出共享 150 万 Token 和 2 美元已知费用预算。每个会话最多 80 步、1200 秒，单次请求最多 180 秒、输出最多 8192 Token。费用未知时无法依靠金额限制精确封顶。
- Diagnosis 使用单一 OpenHands 会话；Editor 复用官方文件编辑器和 `TerminalTool`，可在候选目录内执行离线检查并修复。终端使用当前 Python 环境，Windows 使用 PowerShell；Bot 测试位于候选目录外的 `scripts/test/`，通过候选 `PYTHONPATH` 检查候选代码。仅允许修改 `bot/components/{behaviors,tasks,tactics}/` 中的 Python 文件，固定验证和评估仍由 Run 执行，通过后才可能更新版本指针。
- 预期效果和评估规则在候选评估前固定；可选专项分析使用结构化指标检查。独立留出数据不进入诊断和 Memory。
- 历史 `runtime/rsi_v1/` 等运行产物保留原样，新产物写入 `runtime/rsi/`。

当前已通过针对性的离线检查；新版完整真实对局与付费模型迭代仍需实测。

离线测试覆盖 Bot 执行契约、RSI 证据边界、评估规则及模拟迭代；OpenHands 接入使用脚本化模型，不访问模型服务。单独运行 Bot 或 RSI 测试可使用 `python -B -m unittest scripts.test.test_bot -v` 或 `python -B -m unittest scripts.test.test_rsi -v`。

真实 SC2 烟雾检查需手动执行 `python -B scripts/test/check_match.py`，不会随离线测试启动。它检查客户端、基础生产与 Replay，不用于判断策略强弱。

## 参考

- [Bot 使用与接口](bot/README.md)
- [Knowledge 索引与来源](knowledge/README.md)
