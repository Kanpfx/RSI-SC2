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
- 禁用脚本化开局，经济、科技、侦察和战斗目标由模型决定。基础自动化负责采矿、瓦斯分配、补给和降补给站；工人生产目标由模型指定。
- 保留 Ares Behavior 自带的生产与科技前置步骤处理。跨局经验提取和 RSI 自动更新尚未实现。

## 目录

```text
rsi_sc2_agent/
├── run.py                  # 对局启动入口
├── config.py               # 游戏与模型配置
├── runtime/
│   ├── bot.py              # Ares 生命周期
│   ├── controller.py       # 异步决策与执行调度
│   ├── automation.py       # 基础自动化
│   ├── llm.py              # 模型调用与决策结果
│   ├── parser.py           # 模型输出解析
│   ├── observation/        # 状态采集、压缩与历史
│   └── actions/            # 动作校验与执行，catalog/ 存放 JSON 定义
├── context/                # Prompt、记忆、经验与上下文组装
├── logger/                 # 日志记录与快照代码
├── logs/                   # 运行时生成的对局记录与录像
├── tests/                  # 本项目测试
├── ares-sc2/               # 固定版本的 Ares 源码
└── requirements.txt        # Python 依赖清单
```

`context/` 是后续 RSI 修改 Prompt 与经验的主要边界；`logger/` 独立保留，便于单独管理日志工具权限。实际对局数据位于 `logs/`。

## 环境准备

使用 **Conda 管理 Python 环境，pip 安装项目依赖**，无需 Poetry。需要本机安装 StarCraft II、对局地图和 Git。以下命令在项目根目录的 PowerShell 中执行。

已验证可运行的 Conda 环境可以直接沿用。新环境以 Python 3.12 为基准：

```powershell
conda create -n sc2-agent python=3.12 pip -y
conda activate sc2-agent
python -m pip install -r requirements.txt
python -m pip install --no-deps cython-extensions-sc2==0.15.0
```

`cython-extensions-sc2` 单独安装，以避免其依赖声明引入 Jupyter 等额外组件。依赖版本与 Git commit 由 [requirements.txt](requirements.txt) 固定；新环境安装流程仍需在目标机器上验证。

若项目中尚无 `ares-sc2/`，获取固定版本源码：

```powershell
git clone --depth 1 --branch v3.9.6 https://github.com/AresSC2/ares-sc2.git ares-sc2
```

[run.py](run.py) 自动加载本地 `ares-sc2/src` 与 `ares-sc2/`，以包含源码根目录中的 `sc2_helper`。无需另行安装 Ares 包。

## 配置与启动

首次配置时，将 [.env.example](.env.example) 复制为 `.env`，填写模型服务信息；已有 `.env` 可直接使用。

```dotenv
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=your-api-key
```

模型服务需兼容 Chat Completions 接口。已有 shell 环境变量优先于 `.env`；请求超时、输出长度和动作限制等默认值见 [config.py](config.py)。

```powershell
conda activate sc2-agent
python run.py --map_name Simple64 --difficulty VeryHard --enemy_race Terran --tactic Simple64
```

使用已有环境时，将 `sc2-agent` 换成对应环境名。

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--map_name` | 本机已安装的地图名 | 必填 |
| `--difficulty` | 内置 AI 难度 | `Hard` |
| `--enemy_race` | `Terran`、`Zerg` 或 `Protoss` | `Terran` |
| `--build_mode` | 内置 AI 开局类型 | `RandomBuild` |
| `--tactic` | `context/memory/tactics/` 中的 Markdown 文件名，不含扩展名 | `BattleCruiserRush` |

己方目前仅支持 `Terran`，对局使用实时模式。完整参数及可选值可通过 `python run.py --help` 查看。

## 记忆与日志

提示词在 `context/prompts/` 中按职责组织：

- `system.md`：身份与角色。
- `observation.md`：观测字段说明，随当前观测一起提供。
- `output.md`：输出格式与内容要求，包括包裹方式、DSL 参数写法和工作记忆更新约定。

通用记忆保存在 `context/memory/general.md`，包含全局约束、控制范围与通用经验；战术经验保存在 `context/memory/tactics/`。这些文件开局加载并归档，修改后下一局生效。工作记忆由模型在对局内更新，每局独立，上限 2000 字符。动态动作说明、观测和执行反馈由代码单独组装。

每局生成 `logs/<时间戳>/`，主要内容包括：

- `obs.jsonl`、`model.jsonl`：观测、模型请求与回复。
- `accepted_actions.jsonl`、`events.jsonl`：采纳动作、校验与执行事件。
- `context/`、`working.md`、`settings.json`：上下文快照、最新工作记忆与运行设置。
- `metadata.json`：对局信息与结果。
- `console.log`、`replay.SC2Replay`：控制台输出与对局录像。

## 测试

在项目根目录、已激活的 Conda 环境中执行：

```powershell
python -c "import run, unittest; unittest.main(module=None, argv=['unittest', 'discover', '-s', 'tests', '-v'])"
```

先导入 `run`，复用入口的本地 Ares 路径与 `agent` 包初始化，使测试不依赖项目文件夹名称。测试使用本地模拟回复，不调用模型服务或启动 SC2。
