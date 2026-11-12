# RSI-SC2

基于规则 Bot 的递归改进实验。RSI 从基础简报设计观测，运行采样对局，再用 Python/NumPy 分析轨迹、诊断问题、提出独立修改，并在相同运行条件下比较结果。

## 代码结构

| 目录 | 职责 |
| --- | --- |
| `bot/` | 对局中的规则 Bot；架构见 `bot/README.md` |
| `runtime/matches/local-play-bootstrap/` | 外部 Bot 对战环境，使用 AiArena 镜像；也提供默认地图 |
| `knowledge/python-sc2/` | 固定版本的 API 源码，作为 Agent 的参考资料 |
| `rsi/` | 完整 RSI 研究系统：Agent、实验决策、版本选择、Memory、评估、工具和分析 |
| `rsi/agents/` | 四个角色各自包含研究逻辑、Prompt、输入 Schema 和工具绑定；`common.py` 保存共享研究操作 |
| `rsi/workflow.py` | 单轮研究流程：角色流转、补充采样、阶段退回与预算 |
| `rsi/experiment.py` | 多轮实验：版本池、胜率向量与父版本选择 |
| `rsi/contracts.py` | 跨角色交接格式与研究约束 |
| `runtime/models/tools.py` | 通用 SDK 工具适配、参考资料读取和文件编辑器包装 |
| `runtime/matches/evidence.py` | 遥测读取、完整性检查和证据索引 |
| `runtime/` | 比赛、模型会话、进程、Docker 环境初始化、候选工作区、快照和产物管理 |
| `knowledge/` | SC2 静态数据（适配暂停）与固定版本的 API 参考源码 |
| `tests/` | 离线回归：研究流程、工具、分析、比赛适配及文件边界 |
| `outputs/` | 新实验产物，和系统代码分开 |
| `archives/` | 历史归档，不参与新实验执行 |

`archives/RSI-SC2-v0/` 保存旧版源码、实验及 Git 历史；新代码不导入它的模块。本次迁移的验证摘要和对局录像保存在 `archives/migration-2026-11-10/`，临时验证产物已删除。原 `runtime/local_play/` 和 `runtime/sc2-techtree/` 已原样移至 `archives/runtime-legacy/`，历史文件内部引用未重写。

`rsi/agents/` 的四个角色为 Discovery、Hypotheses、Investigation、Resolution。每个角色的定义集中在一个文件，采集编辑由 Discovery 和 Investigation 自己完成，策略编辑由 Resolution 自己完成。`rsi/` 决定研究步骤、检验和版本选择；`runtime/` 提供会话、工具适配、比赛执行、文件校验和数据存取。SDK 会话通过 job 中一个显式角色模块入口装配 Prompt 和工具，系统运行层不导入具体 RSI 角色。

```text
rsi/
  agents/
    discovery.py
    hypotheses.py
    investigation.py
    resolution.py
    common.py
  workflow.py
  experiment.py
  contracts.py
  evaluation.py
  memory.py
  config.py
  config.json
  __main__.py
```

`runtime/matches/` 管理比赛执行并包含原样保留的 `local-play-bootstrap/`；`runtime/models/` 管理模型会话和工具；`runtime/workspace/` 管理候选、版本、参考资料及产物。共用的 `processes.py`、`console.py` 和 Docker 环境初始化脚本留在 `runtime/` 顶层。

## 环境

使用 Python 3.12，在项目根目录运行命令。已有 Conda 环境可复用：

```powershell
conda activate sc2rsi
python -m pip install -e .
```

依赖固定在 `pyproject.toml`。默认内置 AI 对战需要本机安装 SC2，使用 `python-sc2` 的 `Computer(..., ai_build=...)` 接口；自定义安装位置通过 `SC2PATH` 指定。默认优先使用 bootstrap 目录中的地图。外部 Bot 对战仍使用 Docker Compose 和已有 AiArena 镜像，不自动拉取镜像，也不改写 bootstrap 的基础配置。

实验兼容基准是 `aiarena/arenaclient-sc2:v0.8.0` 中的 Linux 环境和 SC2 4.10.0.75689，宿主机 Python 环境不作为依赖选型或实验验证的依据。该镜像没有 Python 或 burnysc2；Python 依赖独立安装在 volume 中。旧声明中的 `burnysc2==7.1.3` 要求 protobuf <4，与 OpenHands 1.41.0 的依赖链冲突；当前 7.3.0 已在原镜像内通过短对局验证。`agent-client-protocol==0.12.1` 用于满足 SDK 对 `acp.helpers` 的调用，已在容器内通过 SDK 工具验证。`knowledge/python-sc2/` 仍是 7.1.3 参考快照，与当前运行 API 版本不同；实际 API 和引擎版本分别记录在 manifest 中。

实验与单场比赛统一读取 `rsi/config.json`。`runtime.matches` 支持 `--config` 选择配置，命令行参数只覆盖本次比赛条件。两种比赛入口均保留，当前 RSI 使用本地 `bot.run_local`；独立 Bot 的低层启动参数仍由比赛执行器显式传入。

仅检查比赛环境，不启动游戏或调用模型：

```powershell
python -B -m runtime.matches --check
```

运行一次单场检查，和 RSI 使用完全相同的比赛实现：

```powershell
python -B -m runtime.matches --ai-build Macro --difficulty Hard --time-limit 150
```

## Docker 完整实验

直接复用本机已有的 `aiarena/arenaclient-sc2:v0.8.0`，不构建派生镜像，也不复制 SC2。镜像内是 Debian 12 和 SC2，没有 Python。初始化脚本下载固定版本的 Linux Python 3.12.16，校验 SHA-256 后解压到 Docker volume `rsi-sc2-python`，再在其中安装 `pyproject.toml` 的依赖；所有操作使用原镜像，根文件系统只读。宿主机需要 Docker Compose 2.24+ 和 PowerShell；初始化需要访问 GitHub 和 Python 包索引。在项目根目录执行：

```powershell
.\runtime\setup-docker.ps1
docker compose run --rm experiment --rounds 5
```

初始化通常只需执行一次；重复执行会复用已下载的 Python 和匹配的依赖环境，并检查包依赖和 SDK 导入。依赖变更后重新执行初始化，源码、配置、Knowledge 或地图变更后直接重新运行程序即可。volume 中的 `requirements.txt` 记录实际安装版本，`environment.json` 记录 Python 版本与项目配置哈希；下载缓存位于 `outputs/setup/`。

Compose 覆盖镜像原有的 `sc2_controller` 入口，直接运行 `python -m rsi`，默认参数为 `--rounds 5`。整次实验的调度、Agent、候选编辑、分析、Bot 和 SC2 都在同一个容器中执行；每次 `run` 创建独立容器，退出后删除。不挂载 Docker socket，不启动内层容器。`init` 负责回收子进程，超时或取消时现有进程管理逻辑停止对应的 Bot 与 SC2 进程组。OpenHands Terminal 使用 SDK 自带的 subprocess 后端和镜像中的 bash，无需另装 tmux。

`bot/`、`rsi/`、`runtime/`、`knowledge/`、`tests/` 和 `pyproject.toml` 按原目录结构只读挂载到 `/app/`；Python volume 正常运行时只读。宿主只有 `outputs/` 以可写方式挂载，所有快照、候选、分析脚本、会话、日志和录像都保存在 `/app/outputs/`，删除容器后仍保留。其余容器文件系统只读，`/tmp` 提供临时空间。`.env` 仅通过环境变量注入，宿主 Git 目录、历史归档和其他目录不挂载。Agent 可以写入整个 `outputs/`，候选之间继续通过独立副本、修改范围校验和快照哈希管理。

本地与 Docker 共用 `rsi/config.json`，不再保留 `execution`、`match_image` 或 `editor_image` 配置；运行位置由 Python 或 Compose 入口决定。默认仍为固定地图、难度和三种风格各五局，`match_parallelism=2` 限制同时运行两组 Bot/SC2 进程；每局启动新的进程并使用独立输出目录，非可视化、非实时，不发布宿主端口。

初始化后可检查环境、运行短对局或进入 Linux shell：

```powershell
docker compose run --rm --entrypoint python experiment -B -m runtime.matches --check
docker compose run --rm --entrypoint python experiment -B -m runtime.matches --time-limit 150
docker compose run --rm --entrypoint bash experiment
```

Python volume 由初始化脚本创建，声明为 external，`docker compose down` 或 `down -v` 都不会删除它。迁移到其他机器时保留项目、现有 AiArena 镜像和 `outputs/`，单独提供 `.env`，再运行初始化脚本。移入已有快照后可通过 `--parent outputs/versions/<hash>` 开始新实验；不提供运行中会话的自动断点续跑。本地与 Linux 引擎的历史成绩仍按环境身份校验，不能混用。外部 Bot 的 Bootstrap Compose 路径保留为宿主机上的独立入口。

## RSI 实验

复制 `.env.example` 为 `.env`，填写 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`；也可通过进程环境变量提供。密钥不写入实验产物。

```powershell
python -B -m rsi --help
python -B -m rsi --rounds 1
python -B -m rsi --config rsi/config.json --parent outputs/versions/<hash> --rounds 5
```

本地入口为 `python -m rsi`，Docker 入口在容器内运行同一模块。配置位于 `rsi/config.json`；默认使用 `AcropolisAIE`，对手是 Terran、Hard 难度的内置 AI。`evaluation_styles` 默认为 `Macro`、`Timing`、`Rush`，`trials` 默认为 5，指定每种风格的重复次数和一轮内的额外采样总局数；每个版本评估 15 局。`match_parallelism` 独立控制本地及 Docker 比赛的同时运行局数，默认 2，不足时按实际局数运行；可设为 1 以降低资源峰值，不影响评估局数或种子。地图、种族和难度分别通过 `matches.map`、`opponent_race`（`T`、`Z`、`P`）、`difficulty` 配置。

初始父版本和每个新候选都完成整套风格评估。`runtime.matches` 在冻结版本目录中启动现有 `bot.run_local`，直接传入内置 AI 的风格参数，并统一保存 manifest、结果、遥测和录像。需要外部 Bot 对战时，可单独运行 `python -B -m runtime.matches --opponent loser_bot`，仍经过独立 Compose 项目。

内置 AI 每种风格的 `trial=1..5` 同时作为游戏随机种子，各版本使用相同的条件集合。manifest 记录地图和引擎哈希、API 版本与实际引擎信息；条件或环境不一致的评估不能进入同一个版本池。旧的单对手批次需要重新评估。外部 Bot 的 bootstrap 入口仍不保证固定游戏种子。

每轮核心流程为：**问题发现 → 假说生成 → 假说检验与完善 → 问题解决**。四个模块分别拥有问题、假说、机制解释和解决任务的提交权限。

- **Discovery** 从父版本基线开始，自主定义指标并运行 Python/NumPy 分析。现有数据不足时申请采样，收到新数据后继续分析；数据足够时可以直接提交问题，无需额外对局。结果直接包含问题文本、发生条件、证据、影响和指标；有具体局限时再填写不确定性。
- **Hypotheses** 根据发现的问题、证据和相关代码提出竞争解释，明确每个解释的支持与反驳预测。
- **Investigation** 围绕竞争解释设计检验，自主增加指标、编辑采集定义、申请补充采样、记录检验结果和修订假说。修改机制后，旧检验不能继续支持该机制。具有当前假说的支持记录时，才可提交机制解释及干预后的预期变化。
- **Resolution** 根据受支持的机制设计独立修改任务，自己从原始父版本逐个生成候选、编辑策略并完成固定对手评估。任务需要说明机制和原问题的验证方式；对战胜率不自动证明因果机制。任务设计和实施使用同一个 Resolution 角色及模型配置。

所有角色复用 OpenHands 官方 `file_editor`、`glob`、`grep`、`terminal`，使用文件工具读取代码和资料，通过 Terminal 执行 Python 分析与离线检查。指标、问题、假说、检验、诊断和任务统一在会话结束时返回 JSON，由程序按角色校验、保存并交接，不再分别定义维护 JSON 的工具。资料路径和分析脚本目录集中在输入简报的 `paths` 中提供。

Discovery 和 Investigation 使用官方文件工具编辑同一份独立采集副本，再调用唯一的 RSI 专用工具 `run_sampling(matches, mode="reobserve", ai_build=None)`。工具通过控制器校验采集代码、检查并扣除预算、固定版本、运行对局并登记证据；返回新增证据目录和剩余额度，Agent 在同一会话继续分析，无需结束和重建会话。可修正的采集校验错误直接返回工具，等待 Agent 修改后重试；失败对局仍计入预算。采集编辑范围为 `bot/telemetry/logger.py` 的采集定义与钩子；策略、Recorder 和生命周期受保护。`reobserve` 用于重采字段，允许新采集版本重放已有条件；`repeat` 用于增加独立重复，种子从固定评估集之后开始。可用 `ai_build` 指定已配置风格，省略时均衡安排；相同采集版本不重复已执行的条件，独立重复也不复用此前采样的条件。单纯增加重复对局可以复用未修改的采集器。采集版本记录原始策略父版本，不进入版本选择池；Resolution 的策略候选始终从原始父版本开始，编辑范围为 `bot/components/{behaviors,tactics}/`。

每次研究会话通过最终 JSON 显式交接，普通说明文本不能代替结构化结果。模型输入直接提供当前需要的 `problem`、`hypotheses`、`metrics`、`tests`，不使用泛化的 `state` 字段。执行次数与结果由程序记录在会话的 `execution.json`，不接受模型自行填报，也不附加到研究输出。完整证据索引只在新增数据时更新，模型按需读取；已有匹配条件的经验通过可选路径提供，阶段报告引用已有角色产物。Hypotheses 负责首次提出解释，后续由 Investigation 内部修订并保留未改变机制的有效检验；问题定义无效时退回 Discovery，Resolution 可退回 Investigation 补足机制解释。回退会使下游结论失效，并直接提供待修正的问题或诊断及原因；历史产物继续保留。

`max_research_steps` 限制一轮内研究会话总数，包括阶段退回，默认 12；补采样不新增会话。`max_analyses` 统一设为 16，各研究角色分别累计 Terminal 发起的新命令次数，轮询和向运行中的命令发送输入不重复扣次，回退后不重置计数；通过官方文件和搜索工具读取资料不消耗该额度。Discovery 与 Investigation 共享 `trials` 局额外采样额度，仍计入 `max_matches`，并预留至少一组候选评估局数。`use_memory` 控制是否读取已有经验。研究步数耗尽或 Agent 明确放弃时结束为 `unresolved`，保留已获得的证据。

分析执行和输出保存复用 SDK Terminal，命令结果与完整 SDK 事件落盘；内部执行记录包含命令、退出码和结果路径，研究结果只包含有意义的研究字段。四个角色默认共用 `model` 配置，token、费用、交互次数、输出长度和请求/会话超时限制均保留；采样等待时间不扣模型会话时限，仍受整轮及对局时间预算限制。需要不同模型时才填写相应的 `model.<role>` 覆盖项，默认不保留空对象。`resolution_max_tasks` 限制独立修改任务数量。

### 模型输入组织

每个角色先用自然语言说明身份、职责、当前任务和完成标准，随后依次提供：

1. `<brief>`：少量 JSON，包含必要对局条件，以及代码、证据索引、参考资料、分析目录和解释器等材料入口。对局列表、表结构和原始数据保留在索引及文件中，不直接展开。
2. `<bot_readme>`：完整的当前 Bot README，研究阶段使用父版本说明，实施阶段使用候选说明。python-sc2 API 源码按需读取，不直接拼接整份参考资料。静态知识库暂不加载、不复制到实验快照；`runtime/workspace/references.py` 的 `knowledge_files()` 保留 TODO 接口，等待新格式适配。
3. `<research_context>`：本角色需要的上游研究结果；没有内容时省略。回退提供待修正内容和原因，实施只提供单个任务及其关联假说和检验，修复时附上失败检查。
4. `<working_rules>`：本角色的操作边界、必要注意事项及工具使用要点；只提示影响当前决策的命令余额、采样额度或任务数量限制。官方工具参数由 SDK 提供，不在正文重复。
5. `<output_requirements>`：简洁的输出模板和关键约束。完整类型校验留在程序中，不把展开的 JSON Schema 塞进提示词。实施会话按要求返回文字报告。

运行时的 `job.json` 用于程序执行和审计，不会整包拼给模型。首次实施没有空的反馈占位；空的研究材料段也不会出现。补采样工具返回更新后的 `evidence_index` 路径、当前批次证据路径和剩余额度，模型在同一会话读取新增数据并继续分析。

### 研究输出约定

字段定义与程序校验位于 `rsi/contracts.py`。所有证据条目统一为 `{"source":"具体对局/数据表/帧范围或分析结果文件","observation":"实际观察到的事实或比较结果"}`。可选字段没有实际内容就省略；不输出空列表、空对象或套话。成功输出如下：

| 阶段 | 顶层字段 | 内容约束 |
|---|---|---|
| Discovery | `action="problem"`、`problem`、`conditions`、`evidence`、`impact`、`metrics` | `problem` 直接是文本；`uncertainty` 可选。指标包含 `name`、`definition`、`inputs`、`units`，`window`、`missing`、`comparison` 按需填写。 |
| Hypotheses | `action="hypotheses"`、`hypotheses` | 每个假说包含 `id`、`explanation`、`evidence`、`predictions.support/refute`、`verification`；已知时填写 `code_targets`。证据说明提出解释的依据，不表示解释已经成立。 |
| Investigation | `action="diagnosis"`、`tests`、`diagnosis` | 每条检验包含单个 `hypothesis_id`、实际执行的 `method`、`evidence`、`outcome`、`interpretation`。诊断包含 `mechanism`、`hypothesis_ids`、`expected_change`，`uncertainty` 可选；证据沿假说 ID 追溯到检验，不在诊断中重复。 |
| Resolution | `action="resolution"`、`tasks` | 每个任务包含 `id`、`hypothesis_ids`、`change`、`preserve`、`verification`、`code_targets`，直接返回顶层 `tasks`。验证同时覆盖机制响应与原问题改善。 |

Investigation 默认沿用上游假说和指标，只输出本次完成的检验及诊断。有假说修订时才额外返回完整的 `hypotheses` 替换集合；有指标新增或修订时才返回对应的 `metrics`。Worker 校验输出结构，控制器统一校验业务引用、合并指标并保留有效旧检验，变更机制的旧支持失效。检验结果仅允许 `supported`、`refuted`、`inconclusive`；诊断所引用假说的最新有效检验必须为 `supported`。

无法继续时返回 `{"action":"unresolved","reason":"具体证据缺口或障碍"}`；Investigation 返回 Discovery 使用 `action="discovery"`，Resolution 返回 Investigation 使用 `action="investigation"`。结束或回退仍可保留已取得的证据；Discovery 还可保留已定义的指标，Investigation 可保留已完成的检验和明确修订。各角色的 `<role>.json` 只保存研究输出，运行记录单独保存；`response.json` 中的结果路径与用量仅供控制器使用。

候选筛选仅使用每种风格的胜率，不再定义五项专家指标、5% 改善阈值或父版本/冠军双重晋升。所有有效评估的版本均入池：每种风格达到池内最高胜率的版本获得一个权重，并列最佳也计入；下一轮按累计权重随机选择父版本。胜率向量完全相同时，只让演化深度更大的版本参与选择；深度仍相同则按版本哈希稳定决胜。非领先版本保留记录，但选择权重为零。相同源码复用评估，不通过重复生成相同代码增加深度。

平局和达到时间限制的结果不算获胜；崩溃、执行异常、证据损坏或缺少对局不会被当作正常比赛计分。Agent 自定义指标只用于研究，不改变胜率筛选。每轮报告记录候选的 `scores` 和 `selection_weight`；`archive.json` 保存所有版本及深度，`frontier` 给出当前可选版本及权重。`selection_seed` 控制父版本抽样的可复现性。

默认每个版本评估 15 局。`max_matches=65` 覆盖初始父版本 15 局、最多三个候选各 15 局及 `trials=5` 局额外采样；已有评估会复用。配置只要求覆盖父版本和至少一个候选的完整评估，允许减少局数预算；补采样预留一组候选评估，剩余候选按实际预算执行。每轮时间预算默认 10800 秒，局数或时间预算耗尽时跳过尚未启动的候选。

## 产物

```text
outputs/
  experiments/<timestamp>/     # experiment.json、archive.json、各轮报告、Memory、批次证据
  runtimes/<timestamp>/        # SDK 会话、worker 日志、候选 work/
  versions/<hash>/            # 冻结 Bot
  refs/<hash>/                # 冻结固定 python-sc2 API
  matches/<id>/               # 独立单场检查
```

单局包含 `manifest.json`、标准化 `result.json`、`overview.json`、NumPy 二进制表 `*.bin`、录像与运行日志；内置 AI 对局直接写入单局目录，另存原始 `bot_result.json`，不再复制轨迹和录像；外部 Bot 对战另保存原始 `arena_results.json` 和控制器日志。轮次保存研究历史、各次采集请求与 patch、批次索引，并直接引用各会话中的问题、假说、检验、诊断与任务；会话另存分析脚本、命令输出、`execution.json` 和模型用量。正常轮次批次归档为 ZIP，查询时按需恢复。失败和中断保留现场；超时、对手崩溃和基础设施错误不作为正常获胜。

`runtime.matches.evidence.read_dataset(directory, name)` 返回 structured array；每个表保留自己的原生帧轴。`read_telemetry` 同时读取 overview 和各表。不保留旧的分析、采集和编辑角色模块别名。

## 离线验证

```powershell
python -B -m unittest discover -s tests -v
```

这些测试使用脚本化模型和原生格式遥测，不启动 SC2，也不访问付费模型；Compose 配置检查不依赖 Docker daemon。真实比赛使用上述单场检查入口验证。
