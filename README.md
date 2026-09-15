# SC2-RSI MVP

最小可工作的 StarCraft II Bot 自进化系统。固定 RSI 框架通过真实 LLM 工具调用修改 `bot/`，每个版本评测 5 局，使用 Git 保存版本，通过 Archive + Beam Search 选择后续父节点。

## 安装

需要 Python **3.10–3.13**（推荐 3.12）、Git、完整 StarCraft II 安装和地图。锁定的 `burnysc2 7.1.0` 不支持系统 Python 3.14。

在项目根目录，用合适的 Conda 环境创建项目虚拟环境。例如本机已有 `StarWM`：

```powershell
conda activate StarWM
python -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

也可使用独立 Python 3.12 创建普通 venv 并安装同一依赖清单。后续始终使用该环境的解释器。`rg` 是可选搜索命令；没有它时 Agent 仍可使用内置 `search_text`。

启动实验、预检和手动 Benchmark 时，均自动加载项目根目录的 `.env`（示例见 `.env.example`）。已有环境变量优先；文件不存在时跳过。也可直接设置环境变量：

```powershell
$env:SC2PATH = 'E:\Program Files\Battle.net\StarCraft II'
$env:LLM_BASE_URL = 'https://api.openai.com/v1'
$env:LLM_MODEL = '<支持 Chat Completions 工具调用的模型名称>'
$env:LLM_API_KEY = '<你的 API key>'
```

`LLM_BASE_URL` 可指向兼容服务，未设置则使用 SDK 默认地址。不在代码中固定模型。接口按照 [OpenAI 官方工具调用文档](https://developers.openai.com/api/docs/guides/function-calling)实现，服务必须支持 `tools`、`tool_calls` 和 `tool_call_id`。

## 启动

运行前需要项目自己的 Git 仓库、初始提交和已有 Git 身份。此实现交付时已初始化；复制为全新目录时执行：

```powershell
git init -b main
# 仅在尚未配置身份时，使用自己的 name/email 配置 Git。
git add .
git commit -m 'Initial SC2-RSI implementation'
```

先检查环境，不调用 LLM，不启动游戏，也不要求模型密钥：

```powershell
.\.venv\Scripts\python.exe -m rsi.loop --config config/mvp.yaml --preflight
```

检查通过后，以下命令会调用付费模型并运行真实 SC2 对局：

```powershell
.\.venv\Scripts\python.exe -m rsi.loop --config config/mvp.yaml
```

在已激活的对应环境中，等价入口是 `python -m rsi.loop --config config/mvp.yaml`。

### 固定实验设置

- 默认地图 `AbyssalReefLE`，位于本机 SC2 的 `Maps/` 下；需要换地图时，在实验开始前修改配置并提交。
- 5 局、非实时、Terran vs Terran、CheatInsane（等级 10）、RandomBuild。第一版校验并固定这些设置。
- `max_generations: 5` 表示 **5 轮扩展**，Seed 深度为 0；`beam_width: 2`、`branch_factor: 2`。全部成功时最多评测 19 个节点，共 95 局。
- 每局默认 1800 秒**墙钟时间**上限，超时计为崩溃并终止本局进程树；不增加游戏内平局时限。
- 保持游戏默认随机性，未新增固定随机种子。5 局是共同评测协议，不保证逐局随机情形相同；同分以深度、创建顺序决定，不宣称统计显著性。
- 启动前必须提交代码和配置修改；框架会冻结本次配置，并记录解释器、依赖版本、SC2 和地图路径。

## 运行产物

```text
runs/<run_id>/
  config.json          本次配置副本
  environment.json     环境与 Seed commit
  archive.json         所有已评测节点、血缘和战绩
  state.json           轮数、状态和下一批 Parent
  failures.json        生成、测试等失败的候选（有失败时创建）
  summary.json         最佳节点、commit、分支和结果目录
  nodes/<node_id>/
    node.json
    analysis.json      分析对话（节点被扩展时）
    agent.json         工具调用对话（生成的候选）
    agent_result.json
    smoke.json
    metadata.json
    game_01.json ... game_05.json
    game_01.process.json ... game_05.process.json
  worktrees/           临时工作目录，完成或失败后清理
```

`runs/` 下除正式运行目录以外的中间产物都集中在 `runs/tmp/`：pytest 的每用例临时目录、手动 Benchmark 输出、各类验证运行。清理时删除整个 `runs/tmp/` 即可，不影响任何正式结果。

评测先 Smoke Test、再 Git Commit、再 5 局。每局 `result` 为 `win/loss/tie/crash`，`duration` 为秒，另有 `crashed/error`；四类计数总和为 5。框架会捕获 SC2 库记录的错误，避免将启动异常返回的 Defeat 当作正常输局。

每个版本保存在 `candidate/<run_id>_nNNNN` 分支。候选独立从 Parent commit 创建 worktree，不改动主工作目录、不自动合并。评测差的节点也保留；有崩溃的节点不参与排名。父节点只扩展一次，每轮从**整个历史 Archive**中选择尚未扩展的最佳节点。

查看最佳版本可使用 `git show <commit>:bot/main.py` 或 `git diff <parent_commit> <commit> -- bot`。退出后 `state.json` 保留最后状态，第一版不支持自动续跑；再次运行创建新实验。进程强制中止可能留下 worktree，可用 `git worktree list` 确认后通过 Git 清理。

## 工具与边界

- Agent 可读相关项目源码；`write_file/replace_text` 只能写 `bot/`，拒绝路径穿越、符号链接及 junction。
- 命令工具仅开放固定编译、固定 Smoke Test、固定导入以及项目内 `rg` 搜索，使用参数数组，不接受 Shell 字符串。
- Git 只向 Agent 开放 status/diff，提交由 Evolution Loop 执行。Smoke Test 前后检查修改范围，包括被 `.gitignore` 忽略的非缓存文件；允许正常 Python/pytest 缓存产物。
- 子进程不继承 `LLM_*`、`OPENAI_*` 密钥环境；日志遮盖已知 API key。不要把密钥写入源码或提示词。
- 这是本地实验约束，**不是恶意代码安全沙箱**。候选 Python 仍以本机用户权限执行，进程隔离和事后检查不能防止任意恶意文件或网络操作。

## 最小验证

候选每次仅运行编译、导入以及一个接口 Smoke Test，不运行框架测试。开发验证命令（`pytest.ini` 已把临时目录默认指向 `runs/tmp/pytest`，可用 `--basetemp` 覆盖）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_core.py tests/test_bot_smoke.py
```

需要保留某次验证的现场时，加 `--basetemp runs/tmp/validation-<名称>-<日期>`，产物同样落在 `runs/tmp/` 下。

覆盖写入边界、命令限制与超时清理、工具回传、模型输出错误、5 局汇总、Archive 排序、Git 父版本隔离，以及一轮完整控制流程。闭环测试用替代 LLM 和评测器，执行真实编辑、Git 提交和 Smoke Test，不连接模型、不启动 SC2。

交付不包含真实对局战绩；Bot 的实际游戏表现和模型服务兼容性需要配置后单独运行确认。没有 Memory、RAG、多 Agent、训练、复杂搜索或框架自修改。


## Manual Bot Benchmark

Run explicitly; pytest, Smoke Test and the RSI loop do not invoke this script.
Each invocation runs 5 real games using the same config, Evaluator and metadata
as formal evaluation. Results are retained under `runs/tmp/benchmark-<timestamp>_<id>/`
and printed on completion. Git, Archive, Evolution State and formal runs/ are untouched.

```powershell
# Current bot/, including uncommitted edits
.\.venv\Scripts\python.exe tests/bot_benchmark.py

# A directory containing main.py exporting SeedBot
.\.venv\Scripts\python.exe tests/bot_benchmark.py --bot-dir E:/Bots/my-version/bot

# Read-only export of a commit or branch; no checkout
.\.venv\Scripts\python.exe tests/bot_benchmark.py --ref HEAD
```

Optional: `--config path/to/config.yaml`. No LLM key is required.
Directory snapshots record `commit: null`; `--ref` records the resolved commit.
Exit code 0 means no crashes; exit code 1 means setup failure or crashes.


### Parallel evaluation

Formal evaluation and the manual Benchmark both run the five games concurrently,
starting one every 3 seconds (approximately 0, 3, 6, 9, 12 seconds). Each game
uses its own Bot copy, working directory, temporary directory and result files.
SC2 chooses an available API port dynamically. Results remain in game-number order;
Git, Archive and evolution updates remain sequential. Interrupting evaluation
cancels its active game process trees. Speedup depends on available CPU and RAM.
