# SC2-RSI MVP

StarCraft II Bot 的最小自主进化系统。固定 RSI 框架选择父版本，LLM 通过工具修改 `bot/`，检查后运行真实对局，再根据成绩扩展进化树。当前只进化 Bot，不修改 RSI 框架自身。

## 安装与配置

需要 Python（建议 3.12）、Git、完整 StarCraft II 安装及地图。依赖版本见 [requirements.txt](requirements.txt)。

在项目根目录创建环境并安装依赖，后续使用同一解释器：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

也可使用已有 Conda 环境。可选安装 `rg`，未安装时 Agent 仍可使用内置 `search`。

将 [.env.example](.env.example) 复制为 `.env`，填写：

- `LLM_API_KEY`：模型服务密钥。
- `LLM_MODEL`：支持 Chat Completions 工具调用的模型名称。
- `LLM_BASE_URL`：可选兼容服务地址，未设置时使用 SDK 默认地址。
- `SC2PATH`：需要时指定 SC2 安装路径。

启动、预检和手动评测会自动加载项目根目录的 `.env`，已有环境变量优先。可选设置 `LLM_PRICE_INPUT_PER_MILLION` 和 `LLM_PRICE_OUTPUT_PER_MILLION`，用于输入、输出 token 的简单费用估算；未完整配置时 `cost` 为 `null`。

## 启动进化

项目需要独立 Git 仓库、初始提交和 Git 用户身份。正式运行及预检要求工作区干净，先提交代码与配置修改。

```powershell
# 仅检查环境，不调用模型或启动游戏，不要求模型密钥
.\.venv\Scripts\python.exe -m rsi.loop --config config.yaml --preflight

# 调用模型并运行真实 SC2 对局
.\.venv\Scripts\python.exe -m rsi.loop --config config.yaml
```

默认配置见 [config.yaml](config.yaml)：

| 设置 | 当前值 |
|---|---|
| 扩展轮数 | 10 |
| 每轮选择父节点数 | 1 |
| 随机探索概率 / 搜索种子 | 0.2 / 0 |
| 每个父节点的独立候选数 | 2 |
| 每次 Agent 的模型调用上限 | 30 |
| 每个版本评测局数 | 5 |
| 地图 | AbyssalReefLE |
| 对局 | 非实时，Terran vs Terran / CheatVision（Lv8） / RandomBuild |
| 单局墙钟超时 | 1800 秒 |
| 工具命令超时 | 60 秒 |

5 局及对局模式由配置校验固定。地图需安装在 SC2 的地图目录中。默认最多评测 21 个节点（含 Seed），共 105 局；失败或无可选父节点时可能提前结束。

每个版本的 5 局并发运行，启动间隔 3 秒；各局共用候选代码，使用独立工作目录和临时目录。结果按局号排列。超时计为崩溃并终止对应进程树。保留游戏默认随机性，不设置固定随机种子。

## 改进流程

1. 评测当前 Git 提交中的 Seed Bot，建立根节点。
2. 从尚未扩展、无崩溃的节点中选择一个父节点：80% 选最高胜场组（同分随机），20% 从全部候选随机选择。
3. 每次尝试从父提交创建独立 worktree。Agent 阅读代码、父版本日志与历史摘要，选择核心优化方向。
4. Agent 使用 patch 修改 Bot，调用 `finish` 执行范围检查、编译、接口 Smoke Test 和导入检查；失败可在预算内修复。
5. 检查通过后，框架提交候选、评测 5 局并归档，继续后续轮次。

每次候选围绕一个主要问题形成完整改进，允许跨模块配套修改。该要求由 [核心提示词](rsi/analysis/prompts/improve.md) 引导。普通文字回复不会结束候选，必须单独调用 `finish`。

最终结果展示按胜场降序、节点深度升序、创建顺序升序排列；选父采用上述概率策略。搜索种子和每轮选择记录在 run.json，保存状态不会重新抽签；frontier 仅列出可选节点。成绩差的节点也保留，有崩溃的节点不参与排名；每个父节点只扩展一次。5 局成绩用于简单选择，不代表统计显著性。

## 日志与调试

```text
runs/<UTC时间戳>/
  run.json             配置、环境、状态、用量及运行汇总
  tree.json            节点树索引（父子关系、深度与扩展状态）
  nodes/a0/            Seed
    node.json          节点信息、检查与逐局结果
    game_NN.json       Bot 原始轨迹
  nodes/a1/
    node.json          节点信息、检查与逐局结果
    changes.patch      相对父节点的 Git diff
    agent.json         模型对话与工具调用
    game_NN.json       Bot 原始轨迹
```

节点使用 a0、a1…，父子关系保存在 tree.json。每个候选五局共用一份临时代码，各局工作目录独立；结束后清理单局目录、候选 worktree 及空父目录。

排查先看 run.json 的运行状态及 tree.json 的节点树，再看 nodes/aN/node.json 的 failure、agent 和 evaluation，再查看 agent.json 与轨迹。检查成功只留简要结果；崩溃保留进程输出，正常局保留 stderr。总记录在关键阶段及各局结果收集后原子保存，用量随之更新。

Agent 的 feedback/metadata.json 从父节点 node.json 生成，只读且不重复落盘；轨迹通过 feedback/game_NN.json 读取。

### 可进化的 Bot 日志

初始 Logger 位于 `bot/telemetry.py`，每 10 次 `on_step` 迭代采样一次迭代编号、矿和气，每 500 条采样写盘，并在正常结束时保存。异常中止可能丢失尚未落盘的采样。

日志出口约定为游戏工作目录中的 `telemetry.json`，内容应为合法 JSON。RSI 可修改字段、结构和调用位置，逐步增加状态、决策、动作或行为轨迹。框架原样归档，不解析字段或抽样；逐局结果的 `telemetry` 保存 `file/error`。采集失败不改变胜负结果，损坏文件也会保留以便调试。

Agent 通过只读 `feedback/` 路径按需读取父版本汇总、逐局结果、进程输出和 Bot 日志。

### 版本与恢复

候选分支为 `candidate/<run_id>/aN`，不自动合并到主工作目录。可用以下命令查看：

```powershell
git show <commit>:bot/main.py
git diff <parent_commit> <commit> -- bot
```

初版直接引用 nodes/a0/node.json 的 commit，不额外复制或压缩 bot。任意候选可从对应 node.json 的 commit 在 Git 中恢复；也可从初版提交沿 parent_id 祖先链应用 changes.patch。tree.json 的 nodes 列表记录 id、parent_id、generation、status、expanded，即整棵进化树；节点详情保存在各自 node.json。

当前不支持自动续跑，再次启动会创建新实验。强制中止可能遗留 worktree，可用 `git worktree list` 检查后通过 Git 清理。手动评测和定向验证产物放在 `runs/tmp/`，与正式运行目录分开。

## 工具与边界

| 定义文件 | 模型工具 |
|---|---|
| `rsi/tools/edit.py` | `read_file`、`search`、`apply_patch` |
| `rsi/tools/bash.py` | `run_command`、`finish` |
| `rsi/tools/git.py` | `git_view` |
| `rsi/tools/sc2_api.py` | `tech_tree`、`entity_info`、`api_query` |

工具描述、参数 schema 和示例与实现在同一文件中，tools 统一组装，analysis runner 分发调用。

SC2 工具基于本地库：`tech_tree` 查询生产前置及潜在解锁，`entity_info` 查询实体能力和研究项，`api_query` 按包、模块、类和成员逐层浏览或搜索。静态能力不代表对局中可立即使用；生命值、护甲等离线缺失数值返回 `null`，当前不采集运行时属性快照。API 函数默认只返回签名和简短说明，include_source=true 可读取完整说明和分页源码。

- 文件读取支持 offset/limit 字符范围；搜索支持 glob 文件过滤、字面匹配及 offset/limit 分页，返回 matches 和 next_offset。长匹配行会截短并标记，可继续读取原文件。补丁采用 `*** Begin Patch` / `*** End Patch` 包裹的上下文格式，使用 `@@` 分块，无需行号或行数；支持新增、修改、删除和移动，仅写入 `bot/`。旧内容须唯一匹配，全部校验后写入。
- Bot 内部模块与日志内容可进化，保留可安全导入的 `bot.main.SeedBot`（BotAI 子类）入口。
- 命令限于固定编译、Smoke Test、导入及 `rg` 搜索，不接受任意 Shell 字符串。Git 只向模型开放 status/diff；提交和真实对局由外层控制。
- 框架检查修改范围及符号链接/junction；子进程移除 `LLM_*`、`OPENAI_*` 环境变量，日志遮盖已配置的 API key。这是本地实验约束，不是恶意代码安全沙箱。

详细职责和数据流见 [architecture.md](architecture.md)。

## 开发验证与手动评测

开发时按改动选择相关测试，例如：

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_core.py -k '<相关测试>' --basetemp runs/tmp/validation-<名称>
```

`tests/test_core.py` 覆盖工具边界、进程、Agent、归档及控制流程；`tests/test_bot_smoke.py` 检查 Bot 接口。控制流程测试使用替代模型和评测器，不调用模型或启动 SC2。

手动评测复用正式 Evaluator，运行 5 局，无需 LLM 密钥，不更新进化树：

```powershell
# 当前 bot/，包含未提交修改
.\.venv\Scripts\python.exe tests/bot_benchmark.py

# 指定包含 main.py 的 Bot 目录
.\.venv\Scripts\python.exe tests/bot_benchmark.py --bot-dir E:/Bots/my-version/bot

# 只读导出某个提交或分支
.\.venv\Scripts\python.exe tests/bot_benchmark.py --ref HEAD
```

可加 `--config path/to/config.yaml`。结果保存在 `runs/tmp/benchmark-<timestamp>_<id>/`。目录快照记录 `commit: null`，`--ref` 记录解析后的提交。退出码 0 表示无崩溃，1 表示准备失败或出现崩溃。

### 基础上下文管理

完整对话仍保存于 agent.json。工作上下文超过约 60000 字符时，仅省略最近六轮之前的大块检索结果，保留调用配对与错误；不额外调用模型。这是轻量清理，不是严格 Token 上限。需要时重新查询，文件内容可能已变化。

通过 feedback/history/<节点ID>/ 可按需读取本次实验已有节点的 node.json、changes.patch 和对局轨迹，保持只读。

服务器脚本 run.sh 使用 /home/zrshan/projects/RSI-SC2 和 Conda why 环境。Slurm 输出为 RSI-SC2-BOT-<jobid>.out/.err；控制台按 Run、Round、Candidate、Agent、Result、Completed 标记阶段。每行以 `HH:MM:SS` 开头并带节点 ID，可用 `grep 'a1]'` 抽出单条候选的完整轨迹；`Result` 行给出逐局符号、胜负与耗时。`Evaluate`、`Game`、`Agent` 的每步和每局细节默认隐藏，设 `RSI_VERBOSE=1` 打开。进化上限为 10 轮（并非保证每条分支深度达到 10），Slurm 作业时限仍为 2 小时。
