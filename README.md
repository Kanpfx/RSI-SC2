# RSI SC2 Agent

## 项目简介

基于 Ares 与 python-sc2 的 LLM 人族 Agent，在本地 StarCraft II 中与内置 AI 实时对战。模型通过 planner 和 excutor 两个阶段制定目标、生成动作，系统负责观测、校验、执行和日志记录。

## 文件结构

```text
rsi_sc2_agent/
├── run.py                  # 对局启动入口
├── agent/
│   ├── config.py           # 游戏与模型配置
│   ├── paths.py            # 项目路径
│   ├── harness/            # 上下文、模型请求与决策调度
│   ├── game/
│   │   ├── bot.py          # Ares 生命周期接入
│   │   ├── automation.py   # 基础自动化
│   │   ├── scouting.py     # 工人侦察
│   │   ├── observation/    # 观测构建与历史
│   │   └── actions/        # 动作解析、校验与执行
│   └── logger/             # 对局日志与数据采集接口
├── resources/              # 提示词、知识与执行资源
├── tools/                  # 日志查看器
├── tests/                  # 本项目测试
├── logs/                   # 生成的对局记录与录像
├── ares-sc2/               # 本地 Ares 源码
├── .env.example            # 模型连接配置示例
├── .gitignore              # 本地配置与生成文件的忽略规则
├── .gitattributes          # 文本换行与二进制文件规则
└── pyproject.toml          # 项目依赖与 uv 配置
```

## 运行流程

```text
当前观测 → planner 更新指导 → excutor 按最新指导与观测生成 DSL → 解析与校验 → Ares 执行
    ↑                                                                  │
    └──────────────────── 动作历史与执行反馈 ───────────────────────────┘
```

1. 游戏帧更新后，采集资源、单位、生产科技、敌方记忆和动作历史。
2. planner 结合静态知识、战术资料与最近两次决策生成指导，请求上限为 10 现实秒。
3. planner 成功返回后，excutor 按本轮指导和最新观测生成 `## actions` DSL，请求上限为 10 现实秒。
4. 系统解析动作，按执行时的状态校验，交给 Ares Behavior 执行；一次性动作只尝试一次，持续动作运行到下一轮决策收到。
5. 动作状态和错误进入后续观测，供下一轮决策参考；日志保存整个过程。

两者串行调用，不重试。excutor 回复应用完成后等待 2 游戏秒，再启动下一轮 planner；planner 超时或失败时跳过 excutor，excutor 超时或失败时保留当前动作，均等待 2 游戏秒后进入下一轮。等待期间，游戏、基础自动化和持续动作继续运行。调度与观测年龄使用游戏秒，请求耗时与超时使用现实单调计时；iteration 仅用于循环编号。动作历史中 excutor 看上一执行周期，planner 看最近四个执行周期。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `run.py` | 加载环境、解析启动参数、创建对局并保存控制台输出与录像 |
| `agent/config.py`、`agent/paths.py` | 游戏与模型配置、项目资源路径 |
| `agent/harness/` | 组装两个阶段的 messages、请求模型、调度决策与游戏帧之间的交接 |
| `agent/game/bot.py` | 接入 Ares 生命周期，在每帧调用控制器，在结束时记录结果 |
| `agent/game/observation/` | 采集和格式化战局，维护单位 ID、敌方记忆与动作历史 |
| `agent/game/actions/` | DSL 解析、动作过滤与校验、参数解析、Ares 适配和动作生命周期管理 |
| `agent/game/automation.py`、`scouting.py` | 基础经济操作、工人生产目标与自动侦察 |
| `agent/logger/` | 记录观测、模型交互、动作事件、配置和对局结果；预留研究数据采集接口 |
| `resources/` | 提示词、静态知识、战术资料与执行资源 |
| `tools/`、`tests/` | 日志查看器与本项目测试 |
| `ares-sc2/` | 本地 Ares 源码与 Behavior 实现 |

`harness/controller.py` 连接观测、模型、自动化和动作执行。`resources/` 提供决策与执行约定，`logger/` 保存各阶段的数据到 `logs/`，供日志查看器复盘。

## 控制粒度与设计

- **规划层**：planner 管理经济、扩张、科技、生产和战斗方向，每条指导描述一个具体目标。
- **动作层**：excutor 将指导转成宏观、群体或单体动作，指定单位 ID、目标 ID、坐标和必要参数。
- **执行层**：Ares Behavior 处理选址、生产前置、寻路及动作自带的微操；系统维护当前决策周期内的持续动作实例。
- **基础自动化**：负责采矿、瓦斯分配、补给、降补给站和工人侦察；工人生产数量由模型设定。

观测中的己方和可见敌方单位逐个展示 ID、位置、状态与关键属性，敌方记忆采用精简格式。动作历史使用 `active`、`submitted`、`failed`；excutor 看上一执行周期，planner 看最近四个执行周期，各类按时间排序。`active` 表示当前周期内持续执行，`submitted` 表示一次性动作成功下达，`failed` 表示未接受或提交失败；成功下达不代表游戏内任务完成。

系统根据当前战局提供可用动作，并在执行前校验实体、参数、资源和控制冲突。收到新决策时清除上一轮持续动作，再接收本轮动作；想继续的持续动作必须重新提交。空决策同样结束旧周期，请求失败则保留旧周期。撤销执行器不会停止已提交的游戏指令。

## 环境准备

需要安装 Python、StarCraft II、对局地图、Git 和 uv。项目直接依赖以下库，其余依赖由安装工具自动解析：

| 库 | 用途 |
| --- | --- |
| `ares-sc2` | 游戏管理、自动化与 Behavior 执行 |
| `burnysc2`（python-sc2） | StarCraft II 接口、单位与地图数据 |
| `loguru` | 运行日志 |

[pyproject.toml](pyproject.toml) 统一管理依赖和本地 Ares 源。以下命令在项目根目录的 PowerShell 中执行。

首次准备环境时，获取 Ares 源码并安装依赖：

```powershell
git clone --depth 1 https://github.com/AresSC2/ares-sc2.git ares-sc2
uv sync
```

已有 `ares-sc2/` 目录时可直接执行 `uv sync`。使用 Conda 环境时，按 `pyproject.toml` 配置依赖。

[run.py](run.py) 自动加载本地 Ares 与 `sc2_helper`；uv 使用本地 editable Ares 包。

## 配置与启动

首次配置时，将 [.env.example](.env.example) 复制为 `.env`，填写模型服务信息；已有 `.env` 可直接使用。

```dotenv
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=your-api-key
```

模型服务需兼容 Chat Completions 接口。已有 shell 环境变量优先于 `.env`；请求超时、输出长度和动作限制等默认值见 [config.py](agent/config.py)。

所有模型的两阶段均使用 `low` 推理，上限均为 1024 tokens，不指定温度或额外路由参数。仅按服务地址转换推理参数格式，两阶段请求上限均为 10 现实秒。

例如，在 `PylonAIE_v4` 上对抗 VeryHard Terran AI，使用 BattleCruiserRush 战术：

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

## 决策资源

| 路径 | 内容 |
| --- | --- |
| `resources/knowledge/terran.md` | 人族科技树、单位建筑及基础机制 |
| `resources/knowledge/tactics/` | 启动参数选择的战术资料 |
| `resources/prompts/planner/`、`excutor/` | 各阶段的角色、规则和输出要求 |
| `resources/prompts/observation.md` | 共用观测说明 |
| `resources/prompts/argument_definitions.md` | 动作参数约定与示例 |

上下文按阶段组装：

- planner：通用规则 → 静态知识 → 战术资料 → 动作能力（名称与简介）→ 最近两次决策 → 当前观测 → 输出要求。
- excutor：通用规则 → 静态知识 → 动作 DSL 与参数约定 → 最新已完成指导 → 当前观测 → 输出要求。

提示词、知识和战术资料在开局加载。

## 日志与复盘

每局生成 `logs/<时间戳>/`，主要内容包括：

- `obs.jsonl`、`model.jsonl`：观测、模型请求与回复。
- `accepted_actions.jsonl`、`events.jsonl`：采纳动作、校验与执行事件。
- `settings.json`：运行设置。
- `metadata.json`：对局信息与结果。
- `console.log`、`replay.SC2Replay`：控制台输出与对局录像。

日志使用 schema version 3，通过 `run_id`、决策与请求标识关联观测、模型回复和动作事件。`model.jsonl` 保留完整 messages 和原始响应，`metadata.json` 记录运行版本与对局结果。

打开 [日志查看器](tools/日志查看器.html)，选择对局日志目录，即可查看观测、planner 指导、可读思考内容、excutor 动作与执行反馈。`agent/logger/information/` 预留 RSI 数据采集接口，目前处于接口框架阶段。

## 测试

在项目根目录、已激活的 Conda 环境中执行：

```powershell
python -c "import run, unittest; unittest.main(module=None, argv=['unittest', 'discover', '-s', 'tests', '-v'])"
```

测试通过导入 `run` 加载本地 Ares 路径，使用模拟观测和模型回复验证动作、上下文与运行调度。

## 动作表

动作定义位于 `resources/actions/`，分为 `individual_actions.json`、`group_actions.json` 和 `macro_actions.json`，共 47 个动作，按 Ares **3.15.0** 核对接口。模型可用动作由 `enabled` 字段和运行时条件共同决定。

参数分为模型提供的 `model`、固定注入的 `fixed` 和当前帧计算的 `runtime`。planner 接收动作名称与简介，excutor 接收 DSL 与参数说明。

字段约定见[动作表说明](resources/actions/README.md)。`AGENT_KNOWLEDGE_ROOT` 可指定动作表目录，或包含 `actions/` 的父目录。
