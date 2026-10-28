# SC2 RSI

面向持续代码进化的 StarCraft II 规则 Bot 研究项目。Bot 在游戏内按规则运行；RSI 在对局之间分析证据、修改候选代码并进行固定对照。

RSI 新产物中的项目内路径统一使用相对项目根目录的 `/` 路径，例如 `runtime/rsi/experiments/<experiment>/iterations/<id>/iteration.json`；读取时由代码按项目根目录解析，不依赖终端当前目录。本地 Editor 的辅助路径相对候选工作目录，Docker Editor 使用容器内固定路径。项目外的 Python、SC2 安装等路径保留绝对路径。已有历史产物不重写，旧绝对路径仍可读取。

控制台实时显示对局结果、Diagnosis 的问题/假说/查证目的/工具退回与结论，以及 Editor 的文件操作和检查结果；评估保存胜率和核心指标的父子结果。长内容只显示摘要，完整对话、证据及 SDK 诊断日志保留在对应会话文件中，实验汇总写入 `experiment.json`。

控制台固定文案使用英文；显示内容解析或写入失败时跳过该条提示，不中断 RSI 会话。SDK 参数校验错误继续按原流程反馈给模型纠正，原始对话和用量记录照常保存。

## 目录

```text
bot/                          # 模块化规则 Bot
  components/                 # behaviors / tasks / tactics，可编辑业务边界
  core/                       # 调用、单位归属与动作仲裁
  spatial/                    # 地图、路径与选址
  telemetry/                  # 原始游戏日志
runner/                       # Bot/RSI 共用的单局、批次、版本、进程与产物工具
rsi/
  run.py                      # 命令行入口
  experiment/                 # 实验调度、单轮执行、配置校验、工作区与 Memory
  analysis/                   # 日志解码、指标分析、证据读写与查询
  diagnosis/                  # 诊断工具组装、竞争假说与证据更新
  editor.py                   # 候选代码修改与编辑工具边界
  evaluator.py                # 固定对照与版本判断
  tools/                      # SDK 会话、Editor 执行与候选验证
  config.json                 # RSI 预算、实验配置与验收规则
environment.json              # SC2 路径与版本
pyproject.toml                # Python 版本、依赖与安装配置
knowledge/                    # 游戏机制、单位、能力、升级与科技树
scripts/                      # 独立 Bot、环境检查、知识导出与真实对局检查
tests/                        # Bot / RSI / Runner 离线测试与共享 fixture
run_iteration.py              # RSI 根目录入口，转调 rsi.run
runtime/                      # 运行产物
  bot/<id>/                   # 独立 Bot 对局
  rsi/versions/<hash>/        # 冻结 Bot 快照
  rsi/refs/<hash>/            # 跨实验复用的冻结 Knowledge / SC2 API，含来源版本
  rsi/runtimes/<timestamp>/   # 与实验同名，复用 SDK worker、可选 Editor 容器及其日志
    work/candidate_01/       # 临时工作区：workspace.json 记录归属，code/ 为候选代码
    sessions/diagnosis_01/   # 诊断 job、response、用量、SDK 对话、日志和 investigation.json
    sessions/editor_01/      # 编辑会话；按角色在整个实验内连续编号
    inputs/                 # Docker Editor 使用的只读诊断证据副本
  rsi/experiments/<timestamp>/ # 一次命令对应一个实验，例如 20261027_160000
    experiment.json          # 唯一实验配置、各轮简短索引、当前版本/批次及总消耗
    archive.json             # 版本节点、证据引用、扩展次数与谱系边
    iterations/iteration_01/
      iteration.json         # 本轮阶段、状态、版本、费用和产物路径
      memory.json            # 本轮开始诊断时冻结的精简历史经验
      overview.json          # 诊断任务摘要
      validation_t01_0.json  # 按短编号保存静态验证结果
      evaluation_t01.json   # 相对所选 parent 的详细评估
      promotion_t01.json    # 相对当前最佳的评估（必要时生成）
      patch_t01.diff        # branches 保存短编号与完整 task_id 的对应关系
      batches/batch_01.zip   # 正常轮次结束后整批归档，读取时恢复原目录
      batches/batch_01/      # 运行中或按需恢复的批次
        batch.json           # 对局条件、结果和单局路径
        analysis.json        # 批次统计和单局分析路径
        run_01/              # manifest、result、state、events、trace、analysis、录像和进程日志
    memory/                  # 本实验各轮开发经验
```

测试统一位于 `tests/`；RSI 配置统一使用 `rsi/config.json`，环境配置保留在 `environment.json`。

参考资料和 Bot 版本按内容哈希复用；实验只保存引用。Editor 静态验证通过、候选代码冻结后回收临时工作区，失败或中断则保留，启动时提示已有工作区；会话、patch、版本及评估历史不随清理删除。历史实验不迁移、不自动过期删除。启动时用 SDK 文件存储实际检查事件落盘，提前发现 Windows 路径或写入问题。

`editor_execution.mode` 默认是 `local`，使用宿主机环境；独立工作副本与文件编辑范围检查不构成宿主机权限沙箱。需要隔离 Editor 的 Terminal 时，先准备 Docker 并构建依赖镜像：

```powershell
docker build -f rsi/Dockerfile -t sc2rsi-editor:1 .
```

一次 `run_iteration.py --rounds N` 对应一次实验，所有轮次与版本切换共用实验级 SDK worker。本地模式顺序复用一个 worker；每次调用仍新建 LLM、Agent、Conversation 和任务工具，结束时关闭 Terminal 等资源。修复对话、每轮预算和独立候选验证保持原有逻辑。`experiment.json` 的 `runtime` 指向本次执行目录；worker 按首次使用启动，实验正常结束、异常或中断时统一回收，失败不自动重试。

实验目录使用本地时间到秒的 `YYYYMMDD_HHMMSS`；同秒重名追加 `_02`、`_03`。轮次使用 `iteration_01`；batch、candidate 和各角色会话在实验内连续编号，单局在 batch 内使用 `run_01`、`run_02`。候选清理后不重用编号。代码版本和参考资料继续使用内容哈希，历史目录不改名。

Bot 日志 v3 使用原生 `frame`：每次 iteration 开始记录观测，`state.jsonl` 按列分块写入，`events.jsonl` 保存原生回调，`trace.jsonl` 紧凑保留全部调用和动作变化。`manifest.json` 保存条件与环境，`result.json` 保存执行结果，`analysis.json` 保存派生列、派生事件、质量信息，以及 `view` 中供查询使用的标量观测、frame 轴、计数和执行摘要；不复制单位快照或调用历史。单局分析使用无缩进的紧凑 JSON，配置、汇总和批次索引保留缩进；字段与读取方式不变。RSI 只读取当前日志及分析格式；旧分析需从当前格式日志重新生成，v1/v2 日志不再支持。历史文件保留原样，不自动迁移。格式见 [Bot](bot/README.md)。

将配置中的 `editor_execution` 改为 `{"mode":"docker","image":"sc2rsi-editor:1"}` 可启用 Docker。Docker 不可用或镜像未构建时，在对局之前失败，不回退本地。所有 Editor 调用复用本次实验的同一个容器及容器内 SDK worker，Diagnosis 使用宿主 worker；实验结束时移除容器。容器仅挂载本实验的候选和会话目录，以及只读参考资料、诊断证据副本、RSI/runner 代码和测试；同一实验内的任务目录可被容器中的 Terminal 访问。不挂载宿主项目根目录或 `.env`。API Key 通过环境变量传入，不写入 job。依赖版本来自 `pyproject.toml`，更改依赖后重新构建镜像。

Diagnosis、控制端验证和 SC2 对战继续在宿主机执行。Docker 中的离线检查不替代控制端验证；该模式隔离 Editor，不代表整个实验执行链都被沙箱化。

## 环境准备

使用 Python 3.12，在项目根目录执行。已有 `sc2rsi` 环境可以直接复用：

```powershell
conda activate sc2rsi
python -m pip install -e .

# 首次配置：填写 .env 中的 LLM_MODEL、LLM_BASE_URL、LLM_API_KEY
Copy-Item .env.example .env
```

修改 `environment.json` 中的 `sc2_path` 以匹配本机安装，`check_map` 指定检查地图。`sc2_build` 和 `installed_game_version` 是可选版本提示，不匹配时提示但不阻止运行；对局记录实际游戏版本，RSI 对照仍要求实际环境一致。环境检查入口为 `python -B scripts/check_environment.py`，支持 Windows/Linux 客户端布局，`.build.info` 存在时才读取，不会启动对局或调用模型。

Linux 使用 Python 3.12 和相同的项目依赖，首次配置 `.env` 使用 `cp .env.example .env`。原生客户端需要 Linux 游戏包及地图；Wine 安装则额外设置 `SC2PF=WineLinux` 和 `WINE`，并将 `sc2_path` 指向 Wine 内的游戏安装目录。Editor 在 Windows 使用 PowerShell，在 Linux/Docker 使用 Unix 终端。不同客户端版本的策略表现需重新验证，历史产物中的 Windows 绝对路径不会自动迁移。

项目依赖统一写在 `pyproject.toml`，直接依赖固定版本；传递依赖由安装器解析。`.env`、编辑器设置、缓存和实验产物均由 `.gitignore` 排除。

## RSI 执行流程

一次实验按指定轮数扩展版本归档。初始 parent 对局只采集一批，也可通过 `parent_batch` 复用已有批次。每轮 Diagnosis 对一个问题提出最多 `diagnosis.max_tasks` 个独立任务（默认 3），各任务从同一 parent 创建候选，按顺序执行。`model.max_total_tokens/max_cost/max_iterations` 按每次会话独立限制，可在 `model.diagnosis/editor` 中分别覆盖；消耗累计记录，不扣减后续会话额度。对局数和运行时长仍按轮限制。

```mermaid
flowchart TD
    A["创建实验、冻结初始 Bot、准备初始对局分析"] --> B["选择 parent：交替扩展当前最佳与较少探索的历史版本"]
    B --> C["读取 parent 分析、生成概览、冻结本轮 Memory"]
    C --> D["Diagnosis：问题、竞争假说、取证 → 多个独立任务"]
    D -->|无结论| K{"还有下一轮？"}
    D --> T1["任务 T1 → 独立 candidate"]
    D --> T2["任务 T2 → 独立 candidate"]
    D --> T3["任务 T3 → 独立 candidate"]
    T1 --> V["各自编辑、静态检查与有限修复"]
    T2 --> V
    T3 --> V
    V -->|通过| E["候选对局与分析；相同代码复用已有证据"]
    V -->|失败| R["记录分支失败，继续其他任务"]
    E --> F["Evaluator：与 parent 比较，再与当前最佳比较"]
    F --> G["有效子代进入归档，记录任务与谱系"]
    F --> H["满足原有接受规则时更新当前最佳"]
    G --> K
    H --> K
    R --> K
    K -->|是| B
    K -->|否| L["保存实验汇总与版本归档"]
```

首轮扩展初始版本；后续偶数轮扩展当前最佳，奇数轮选择扩展次数最少的归档版本（同次数优先当前最佳，再按哈希确定顺序）。正常完成且证据有效的子代可进入归档，即使暂时不如 parent；静态失败、Bot 错误及证据无效的版本不进入归档。当前最佳与探索 parent 分开记录，较弱分支不会自动替换当前最佳。

静态失败只修复对应任务；candidate 的 Bot 错误只拒绝该分支。Editor 会话失败只跳过该候选；Diagnosis、基础设施或日志错误导致本轮无法继续时，保留有效子代和最佳结果并继续下一轮，连续 3 轮运行错误才停止。正常拒绝或 unresolved 会重置错误计数；认证、余额、API 配置错误、快照损坏和用户中断立即停止。预算耗尽时，未开始的任务标记为 `skipped`。任务的 `verification` 是核实建议，自动选择仍使用原有五项指标和胜率规则，不把任务有效等同于假说已被证明。

## 运行

```powershell
conda activate sc2rsi

# 查看参数，不启动游戏或模型
python -B run_iteration.py --help

# 从当前仓库 bot 开始，运行 5 轮；会启动 SC2 并调用配置的模型
python -B run_iteration.py --rounds 5

# 从指定快照开始；默认 1 轮
python -B run_iteration.py --parent runtime/rsi/versions/<hash> --rounds 5

# 其他设置统一放在 config
python -B run_iteration.py --config rsi/config.json --rounds 5

# 独立 Bot
python -B -m bot.run --time-limit 150

# 离线检查，不启动游戏或付费模型
python -B -m unittest discover -s tests -v
```

独立 Bot 与 RSI 共用 `runner.matches.run_match`，每局在独立进程中运行，统一保存 manifest、标准结果和 Replay。独立入口可用 `--wall-timeout` 设置现实时间上限（默认 600 秒），`--no-log` 只关闭遥测。基础 API 和产物字段见 [Runner](runner/README.md)。

- RSI 入口参数为 `--parent`、`--rounds`（默认 1）、`--config`。默认从当前仓库冻结初始 Bot，不读取历史全局版本指针。`current_parent/current_batch` 保存当前最佳；探索 parent 可以来自归档中的其他版本，轮数表示扩展轮数。
- `--config PATH` 指定实验配置；模型读取 `.env` 中的 `LLM_MODEL`、`LLM_BASE_URL`、`LLM_API_KEY`，不把密钥写入产物。
- `parallel_matches` 同时决定每批局数和并发数，默认 5；seed 自动生成 `1..parallel_matches`，所有版本使用同一组。初始 parent 采集一批，每轮最多运行 `max_tasks` 个候选批次；同一代码哈希在实验内复用已有有效结果，不重跑 parent，也没有额外验证对局。启动提交间隔 3 秒以分散客户端加载峰值。日志复用 Bot Logger；Analyzer 只生成静态分析结果，Retriever 统一提供 `overview / metrics / events / trace / compare` 日志查询和源码、API、Knowledge、Memory 检索。时间窗口使用游戏秒。
- Retriever 默认返回摘要，`detail=raw` 获取采样、Trace 状态或详细统计。`next_offset` 表示可翻页，`truncated` 表示内容被省略；日志引用保留在条目内，完整证据留在分析产物中。参考读取直接返回路径、行号或 key 及内容，失败统一返回 `error={code,message}`。
- 诊断问题使用 `{problem, evidence}`，假说使用 `{id, explanation, predictions:{support,refute}, code_targets}`；问题和假说可随证据调整，通过 `reason` 说明修改理由。假说修订历史保存在调查中，工具调用保留问题调整及理由。
- 自主查询分为 `query_logs(kind, purpose, params?)`、`read_reference(path, lines?, key?)`、`query_memory(text?)`，与 `select_issue`、`conclude` 一起组成五个诊断工具。日志查询单批次时自动补 `batch_id`，默认全部对局与时间。资料路径以 `source/`（父版本 `bot/`）、`api/` 或 `knowledge/` 开头；文本按 1-based 行号读取，每次最多 120 行，复用 SDK 的只读文件读取方法；JSON 用 `key`（支持点分隔）定位，不传则列出条目。Memory 按文本匹配本轮冻结经验。三类查询共用预算，记录统一保存为 `{tool, arguments, result}`；模型自主回顾和完善假说。
- 结论为 `{tasks:[...]}`；各任务包含 `{id, hypothesis_ids, change, preserve, verification, code_targets}`，ID 唯一且关联已存在的假说。`verification` 是自然语言核实建议，不转换成局部指标硬门槛。代码目标采用项目相对文件路径或 Python 模块名，可用 `::Class.method` 指明符号；提交时检查文件、符号及可编辑范围。无可执行建议时调用 `conclude(reason=...)`，本轮记为 `unresolved`。
- `overview.json` 保存诊断的核心任务数据：`runs / events / paths / limits`，包含对局条件、常用指标及每局各类诊断事件的首次发生。该对象原样作为会话 `job.json` 的 `task` 保存并拼入 Prompt，不再维护另一份展示格式。Bot、Knowledge 的简短说明直接拼入角色上下文。
- Editor 接收 `issue / hypotheses / task / paths / validator_feedback`，只包含自己的任务及相关假说的当前内容，保存并直接发送 `job.task`；`paths.evidence` 显式引用诊断会话的 `investigation.json`，其中 `queries` 保存查询记录，不从候选目录层级推导。会话 response 引用调查结果，不复制完整调查。`read_reference` 读取共用的冻结 API/Knowledge，`source/` 指向当前候选。Memory 保留问题、任务建议、各分支验证/评估摘要及产物路径，不嵌套假说历史、查询和完整指标。
- `iteration.json` 只保存运行摘要和产物路径，完整诊断、验证、评估保存在对应阶段文件。诊断进程直接读取本轮已保存的探索分析结果，不重复计算指标和事件；概览、指标和事件查询直接使用保存的分析，Trace、State 和局部执行细节按需读取原始日志，同一诊断内复用解码结果，会话结束后释放。磁盘日志及分析结果保留。
- config 的 `parent_batch` 可指向初始 parent 的已有 `batch.json`，默认 null；复用前校验版本、对局条件和记录身份，重新分析仅保存到新实验的 `batches/<id>/`，历史目录保持只读。批次分析只引用单局分析；实验内部直接复用已保存的分析。诊断的 Bot 说明读取冻结 parent。阶段开始、结束和失败统一更新 `iteration.json`，不另存 started/final 文件。批次中断时取消排队任务并终止运行中的 worker 进程树，`batch.json` 保留已完成结果及取消记录。
- parent 和 candidate 对局共用 `matches` 中的时间限制，默认每局最多 1800 游戏秒、600 现实秒。对局为非实时运行，实际速度受机器负载与 Bot 单步开销影响。
- 对局预算按轮重置，所有任务共享最多 24 局、7200 现实秒（默认首轮最多 5+3×5=20 局）；每次 Diagnosis、Editor 及修复会话分别使用 150 万输入/输出 Token 和 2 美元已知费用预算，角色配置可覆盖默认值。每个会话最多 80 步、1200 秒，单次请求最多 180 秒、输出最多 8192 Token。费用未知时无法依靠金额限制精确封顶。
- LLM 请求遇到 SDK 识别的连接错误、超时、限流等暂时性错误时额外重试最多两次，共最多三次尝试，沿用 SDK 的指数退避；不重跑整个会话或实验。OpenHands 1.41.0 的 `num_retries` 实际表示总尝试次数，因此设置为 `3`。默认每会话费用上限 $0.30、时间上限 600 秒，每轮时间预算 3600 秒，静态修复最多一次；重试及等待仍受这些预算约束。非致命会话失败清理后继续复用 worker；已退出的 worker 在下一任务前回收并重建。
- `model` 保存公共 LLM 设置和每会话额度，`model.diagnosis`、`model.editor` 分别覆盖角色参数；空对象表示继承公共设置。可分别设置 `model / base_url / api_key_env / reasoning_effort / temperature / top_p / top_k / seed` 及输出、步数、超时限制；未指定的模型和地址回退到 `.env`。默认 `reasoning_effort=high`，采样参数使用模型默认值，SDK 按模型能力转换或剔除参数。各会话消耗累计记录，不扣减后续会话额度。每个会话的 `job.json` 保存实际请求配置，`llm.json` 保存 SDK 构造后的配置（不含密钥）；服务端实际支持情况仍需实测。单会话费用上限复用 SDK `max_budget_per_run`，按已报告费用在步骤间检查，可能超出一次请求的费用。只有 SDK 正常完成的会话返回 `ok`，其他结束状态与错误原因保存到响应中。
- Diagnosis 使用单一 OpenHands 会话；每个任务有独立 Editor 会话，复用官方文件编辑器和 `TerminalTool`。终端使用当前 Python 环境，Windows 使用 PowerShell；Bot 测试位于候选目录外的 `tests/`，通过候选 `PYTHONPATH` 检查候选代码。仅允许修改 `bot/components/{behaviors,tasks,tactics}/` 中的 Python 文件。静态检查与对局仍由 Run 执行，符合相对当前最佳的接受规则后才更新最佳版本。
- config 只保留常用实验设置、运行预算和 LLM 参数。接受规则及五项指标固定在 `evaluator.py`，Analyzer 阈值保留在 `analyzer.py`，诊断返回条数和长度限制保留在 `diagnosis/investigate.py`。评估结果仍保存实际使用的规则和各指标数值。
- 接受规则：胜率严格提升就接受；胜率下降则拒绝；持平时，五项中至少三项相对提升 5% 才接受。不另加绝对阈值或退步否决规则。parent 为零、candidate 为正值算提升；缺失或持平不算提升，缺失项仍计入总项数。平局及游戏时间上限按未胜计。
- 五项指标为：敌方单位与建筑击杀总价值 `combat_value`、击杀价值占击杀加己方损失价值的比例 `trade_efficiency`、平均现存军队价值 `army_value`、累计军队投入 `army_investment`、平均每分钟采集资源 `resource_collection_rate`。统一按矿物加瓦斯估值，军队使用原生 army 分类。交换效率无交战时为零；5% 指相对提升，不是增加 5 个百分点。
- Logger 被动记录原生 `state.score`，敌方单位/建筑击杀和分类损失等原始字段以 `score.*` 指标保留。同 seed 双方按共同游戏时长计算过程指标，再按批次取均值；平均军队价值按时间加权。旧日志缺少 score 时不会补零，需使用带新记录字段的 Bot 采集基线，之后继续复用。
- 复用比较仍检查条件、环境、日志完整性和采样覆盖率。失败记录保留在对应会话、分支和轮次报告中；跳过候选或轮次不会清除已经确认的改进。无诊断结论时记为 unresolved，当前最佳不变，下一轮仍按归档策略选择 parent。每个实验独立保存归档、记录与 Memory，版本快照按内容哈希共享。
- 历史 `runtime/rsi_v1/` 等运行产物保留原样，新产物写入 `runtime/rsi/`。

当前已通过针对性的离线检查；新版完整真实对局与付费模型迭代仍需实测。

离线测试覆盖 Bot 执行契约、RSI 证据边界、评估规则及模拟迭代；OpenHands 接入使用脚本化模型，不访问模型服务。单独运行 Bot 或 RSI 测试可使用 `python -B -m unittest tests.test_bot -v` 或 `python -B -m unittest tests.test_rsi -v`。

真实 SC2 烟雾检查需手动执行 `python -B scripts/check_match.py`，不会随离线测试启动。它检查客户端、基础生产与 Replay，不用于判断策略强弱。

## 参考

- [Bot 使用与接口](bot/README.md)
- [Knowledge 索引与来源](knowledge/README.md)
