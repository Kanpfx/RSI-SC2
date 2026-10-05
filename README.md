# Minimal SC2 Agent

基于 Ares 与 python-sc2 的 LLM 人族 Agent，在本地 StarCraft II 中与内置 AI 实时对战。项目重点是模型决策与游戏执行之间的接口，为后续 RSI 经验更新提供上下文和对局记录。

## 运行方式

```text
游戏观测 → 上下文组装 → 异步 LLM → DSL 解析 → 当前状态校验 → Ares 执行
    ↑                                                        │
    └──────────────── 执行结果与错误反馈 ──────────────────────┘
```

- 最多一个模型请求在途；等待回复时，游戏、基础自动化和已接受的持续指令继续运行。
- 模型只看到当前可用的动作与参数，回复到达后按执行时的状态重新校验。单条动作失败不影响同批合法动作，错误反馈给下一轮决策。
- 禁用脚本化开局，经济、科技和战斗目标由模型决定。基础自动化负责采矿、瓦斯分配、补给和降补给站；工人生产目标由模型指定。开局 60 秒后且至少有 12 个工人时，自动派一个工人先到敌方出生点，再循环巡视扩张点；每个路点最多停留 10 秒，阵亡后等待 10 秒再补派。
- 保留 Ares Behavior 自带的生产与科技前置步骤处理。跨局经验提取和 RSI 自动更新尚未实现。

## 目录

```text
rsi_sc2_agent/
├── run.py                  # 对局启动入口
├── agent/                  # Python 实现
│   ├── config.py           # 游戏与模型配置
│   ├── paths.py            # 项目与资源路径
│   ├── harness/            # 上下文组装、模型调用与决策调度
│   ├── game/               # Ares 生命周期、自动化与侦察
│   │   ├── observation/    # 状态采集、压缩与历史
│   │   └── actions/        # DSL 解析、动作校验与执行
│   └── logger/             # 日志记录与快照代码
├── resources/
│   ├── knowledge/          # 通用经验、tactics/ 战术与 actions/ 动作定义
│   └── prompts/            # working/、actions/ 提示词与共用观测说明
├── tools/                  # 日志查看器
├── logs/                   # 对局记录与录像
├── tests/                  # 本项目测试
├── ares-sc2/               # 本地 Ares 源码
├── pyproject.toml          # 当前依赖与 uv 源设置
└── requirements.txt        # 旧版环境依赖快照
```

`resources/` 是后续 RSI 修改 Prompt 与经验的主要边界；`agent/logger/` 独立保留，便于单独管理日志工具权限。实际对局数据位于 `logs/`。

## 环境准备

使用 Python 3.11–3.12；依赖与本地 Ares 源设置见 [pyproject.toml](pyproject.toml)，无需 Poetry。需要本机安装 StarCraft II、对局地图、Git 和 uv。以下命令在项目根目录的 PowerShell 中执行。

首次准备环境，先获取最新 Ares 源码，再安装依赖：

```powershell
git clone --depth 1 https://github.com/AresSC2/ares-sc2.git ares-sc2
uv sync --python 3.12
```

当前本地源码为 Ares 3.15.0，commit `70a151a66fdd00d6c1ee44718735d0da396b902e`。该版本要求 `cython-extensions-sc2 ^0.18.0`；已有 Conda 环境需要匹配新版依赖。[requirements.txt](requirements.txt) 保留为旧版环境快照，不适用于本次升级后的依赖安装。新环境安装流程仍需在目标机器上验证。

[run.py](run.py) 自动加载本地 `ares-sc2/src` 与 `ares-sc2/`，以包含源码根目录中的 `sc2_helper`。uv 使用本地 editable Ares 包。

## 配置与启动

首次配置时，将 [.env.example](.env.example) 复制为 `.env`，填写模型服务信息；已有 `.env` 可直接使用。

```dotenv
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=your-api-key
```

模型服务需兼容 Chat Completions 接口。已有 shell 环境变量优先于 `.env`；请求超时、输出长度和动作限制等默认值见 [config.py](agent/config.py)。

```powershell
uv run python run.py --map_name Simple64 --difficulty VeryHard --enemy_race Terran --tactic Simple64
```
添加示例：在 `PylonAIE_v4` 上对抗 VeryHard Terran AI，并使用
BattleCruiserRush 战术。

```powershell
uv run python run.py `
  --map_name PylonAIE_v4 `
  --difficulty VeryHard `
  --build_mode RandomBuild `
  --enemy_race Terran `
  --tactic BattleCruiserRush
```

使用依赖匹配的已有 Conda 环境时，也可以直接运行 `python run.py`。

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--map_name` | 本机已安装的地图名 | 必填 |
| `--difficulty` | 内置 AI 难度 | `Hard` |
| `--enemy_race` | `Terran`、`Zerg` 或 `Protoss` | `Terran` |
| `--build_mode` | 内置 AI 开局类型 | `RandomBuild` |
| `--tactic` | `resources/knowledge/tactics/` 中的 Markdown 文件名，不含扩展名 | `BattleCruiserRush` |

己方目前仅支持 `Terran`，对局使用实时模式。完整参数及可选值可通过 `python run.py --help` 查看。

## 记忆与日志

动作表位于 `resources/knowledge/actions/`，采用 `individual_actions.json`、`group_actions.json` 和 `macro_actions.json`，共 47 个 Ares Behavior。表内使用类名作为动作标识；仅 `source: model` 的参数允许模型提交，`fixed` 参数按表内值逐实例注入，`runtime` 参数根据当前帧计算。单位归属、可用性、参数限制与持续执行由代码管理。可选模型参数也会展示，例如 `UseAbility.target`。

动作表沿用 `ares_base_agent/aciton_list/` 的 Ares 3.14.0 定义，已针对本地 Ares 3.15.0 核对构造参数和必填性。固定默认值包含 `SpawnController.over_produce_on_low_tech=true`、`UpgradeController.auto_tech_up_enabled=true`，遵循表内约定。持续 Behavior 复用实例以保留内部状态；混合空地群体需要拆分后提交，网格由 runtime 选择。`PlacePredictiveAoE` 保持关闭，未记录路径时不会推断路径。

提示词在 `resources/prompts/` 中按职责组织：

- `working/system.md`：第一轮角色，依据全局规则与经验、战术和观测生成自然语言指导。
- `actions/system.md`：第二轮角色，以 working 为核心，结合全局规则与经验、观测、动作表和反馈生成 DSL 动作。
- `observation.md`：观测字段说明，随当前观测一起提供。
- `working/output.md`：第一轮输出要求，仅生成“当前阶段”和“具体指导”。
- `actions/output.md`：第二轮输出要求，依据第一轮指导生成 DSL 动作。

通用记忆保存在 `resources/knowledge/general.md`，包含全局约束、控制范围与通用经验；战术经验保存在 `resources/knowledge/tactics/`。这些文件开局加载，修改后下一局生效。每个决策周期独立调用两轮，共享同一份公共上下文，依次为通用规则、观测和动作表。错误与提示统一放在观测的 action_history 中，分别归入 failed 和 notice，包含事件时间、原始动作及原因，不再单独拼接 execution_feedback。第一轮在公共上下文后追加完整战术、上一轮 working（`previous_decision`）和输出要求；上一轮决策仅作为历史参考，需结合当前观测与反馈修正，不代表动作已执行，首轮显式标记无上一轮决策。回复保留在内存和系统日志中，不单独生成 `working.md`，不额外校验格式或长度。第二轮在公共上下文后追加本轮 working（`current_decision`）和输出要求，仅生成动作，不携带第一轮对话或战术表。周期之间不累积完整对话。动作回复只接受 `# actions` 段，不兼容旧的 `# working` 段。

每局生成 `logs/<时间戳>/`，主要内容包括：

- `obs.jsonl`、`model.jsonl`：观测、模型请求与回复。
- `accepted_actions.jsonl`、`events.jsonl`：采纳动作、校验与执行事件。
- `settings.json`：运行设置。
- `metadata.json`：对局信息与结果。
- `console.log`、`replay.SC2Replay`：控制台输出与对局录像。

系统日志采用 schema version 3，每条 JSONL 记录包含 `run_id`。模型两轮请求分别标记 `working` / `actions`，具有独立 `request_id`，重试使用所属请求的 `attempt_id`。观测与执行分别记录迭代编号、可获取的 SC2 game loop 和游戏时间；异步响应只关联原观测，不推测当前游戏帧。

所有日志直接保存在对局目录，不生成 `system/` 或 `context/` 子目录，也不保存资源快照或其 hash；模型日志仍保留实际发送的完整 messages。metadata 保存代码及 Ares commit、工作区修改状态、Python/关键依赖版本和可获取的 SC2 base build；未知版本与未显式配置的随机种子记为 null。未提交代码只记录修改状态，不保存代码补丁，因此不能仅凭 commit 完整还原修改中的工作区。后续 RSI 数据预留在 `data/`，目前只有未接入的接口框架。`tools/日志查看器.html` 读取平铺日志，也兼容旧的 `system/` 目录布局；请选择对局日志目录。

## 测试

在项目根目录、已激活的 Conda 环境中执行：

```powershell
python -c "import run, unittest; unittest.main(module=None, argv=['unittest', 'discover', '-s', 'tests', '-v'])"
```

先导入 `run`，复用入口的本地 Ares 路径；实现代码使用标准 `agent` 包，使测试不依赖项目文件夹名称。测试使用本地模拟回复，不调用模型服务或启动 SC2。
