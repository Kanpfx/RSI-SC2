# RSI 代码导航

RSI 按指定轮数扩展归档：选择 parent → 诊断并提出多个独立任务 → 各自编辑、验证和对局 → 比较 parent 与当前最佳 → 记录谱系与归档。每次 Diagnosis/Editor 会话独立使用模型额度；对局数和运行时长按轮限制。较弱但有效的版本可以继续探索，当前最佳单独维护。

`experiment/` 集中实验调度、单轮执行、配置校验、工作区与 Memory；`analysis/` 集中日志解码、指标分析、证据读写和查询。跨 Bot/RSI 的执行能力位于项目根目录的 `runner/`，模型接入与候选检查位于 `tools/`，诊断消融辅助位于 `diagnosis/session.py`。

## 逻辑模块

| 文件 | 职责 |
| --- | --- |
| `run.py` | 命令行参数解析，调用实验入口；常用配置仍位于 `config.json` |
| `experiment/run.py` | `Experiment` 与 `run_experiment()` 管理共享资源、版本归档、证据索引、当前最佳及探索 parent |
| `experiment/iteration.py` | `Run.execute()` 管理单轮多任务与共享预算，返回 `IterationResult(report, baseline, candidate, children)` |
| `analysis/evidence.py` | 批次分析组织、保存与加载，结合 manifest/result 拼装查询视图；历史输入保持只读 |
| `config.json` | 常用实验设置、运行预算、公共及各角色 LLM 设置 |
| `analysis/analyzer.py` | 单局日志的指标、事件和执行统计计算；不加载对局元信息或保存分析产物 |
| `analysis/logs.py` | 当前 Bot 日志格式解码、来源定位和格式质量检查 |
| `analysis/retriever.py` | 按范围查询分析结果和原始日志，分页限制输出，读取源码、冻结参考资料及经验 |
| `references.py` | 按内容哈希跨实验复用 Knowledge/API 快照并校验完整性；按显式源码根目录读取父版本或候选资料 |
| `experiment/workspace.py` | 短路径分配、SDK 落盘预检查、临时工作区归属与安全清理 |
| `experiment/batches.py` | 完成轮次的批次 ZIP 归档、按需恢复与解压副本清理 |
| `diagnosis/session.py` | 诊断 Prompt、工具输入格式及五个诊断工具的装配 |
| `diagnosis/investigate.py` | 问题与假说状态、查询预算与记录、假说修订、结论和代码目标检查 |
| `editor.py` | 编辑 Prompt、候选任务材料与原始调查记录引用、文件编辑器的候选写入边界 |
| `evaluator.py` | 固定指标和接受阈值、批次有效性检查、胜率与核心指标比较 |
| `experiment/memory.py` | 保存精简开发经验及产物路径，按条件检索近期经验 |

## 基础工具

| 文件 | 职责 |
| --- | --- |
| `../runner/artifacts.py` | 项目路径与存储路径转换、JSON 读取和原子写入；路径函数支持显式 `root` |
| `experiment/config.py` | 读取配置、校验实验参数 |
| `tools/console.py` | SDK 工具操作与结果摘要；普通进度输出复用 `runner/console.py` |
| `tools/llm.py` | `ExperimentRuntime` 持有实验级 SDK worker；逐次创建独立 OpenHands 会话、记录模型预算与用量，角色准备和工具装配由 Diagnosis/Editor 提供 |
| `tools/sandbox.py` | 可选实验级 Docker Editor 容器的预检查、固定挂载、逐任务路径转换和容器回收；不更改会话内部流程 |
| `tools/sdk.py` | 将普通 Python 函数适配为 SDK 工具，提供共享参考资料读取格式 |
| `../runner/matches.py` | 独立 Bot 与 RSI 共用的批次并行、单局 worker 和标准对局产物 |
| `../runner/processes.py` | 对局与模型 worker 共用的等待、取消和终止；不解释业务结果 |
| `tools/validate.py` | 候选修改范围、语法与独立子进程导入检查，不提供系统权限隔离 |
| `../runner/versions.py` | 单一内容哈希算法、快照、校验、候选复制和差异文件；编辑范围策略留在 RSI |

## 实验辅助

`diagnosis/session.py` 同时保存诊断消融的 Prompt 调整和固定证据包；接受规则直接定义在 Evaluator，不调用额外模型生成评估方案。

## 修改约定

- 流程分支放在 `Run`，指标计算放在 Analyzer，查询行为放在 Retriever，晋升规则放在 Evaluator。
- SDK 输入格式留在工具边界；内部函数使用明确参数和稳定返回结构。阶段返回业务产物，不定义通用阶段协议。
- `experiment.json` 中的 `current_parent/current_batch` 表示当前最佳；`archive.json` 保存版本节点、证据引用、扩展次数与谱系边。每轮报告的 `parent` 是实际探索版本。Memory 与归档按实验隔离，不使用全局版本指针。
- 实验配置只保存到 `experiment.json`；每轮 `iteration.json` 保存状态、摘要及产物路径，不另存 started/final 文件。Knowledge/API 按内容保存在共用 `refs/<hash>`，实验和轮次记录引用；每轮单独冻结 Memory 输入。
- 一次命令对应一次实验；`Experiment` 统一准备配置、Versions、冻结参考、Analyzer 和模型设置，并持有唯一 `ExperimentRuntime`，各轮引用同一实验资源。结束、异常和中断时统一关闭；清理错误单独记录，仍保存最终报告并保留原始失败原因。本地顺序复用一个 SDK worker；Docker 模式额外复用一个 Editor 容器及其 worker。固定工具类型和输入 schema 在 worker 内复用；Conversation、LLM 和工具执行器逐次创建，自定义工具直接加入当前会话，不向全局注册表绑定诊断对象。候选导入验证和对局保留独立子进程。
- 模型配置、预检查和 Analyzer 在实验内复用。`model.max_total_tokens/max_cost/max_iterations` 是每次会话的默认上限，`model.diagnosis/editor` 可分别覆盖；每个独立任务及修复会话重新获得完整额度，历史消耗只统计、不扣减后续额度。每批固定运行 `parallel_matches` 局，seed 为 `1..parallel_matches`。
- Memory 查询显式指定每轮冻结的 `memory.json`，不读取共用 context 中的旧 Memory。
- 实验目录使用本地时间到秒的 `YYYYMMDD_HHMMSS`，同秒重名追加顺序编号；runtime 与实验同名。轮次为 `iteration_01`，batch、candidate 和各角色会话在实验内连续编号，单局在 batch 内为 `run_01`。候选清理后不重用编号；版本和参考资料的内容哈希不变。
- 单局分析只保存一份，批次 `analysis.json` 保存结果计数、事件计数和单局分析路径，指标统计从单局报告按需计算。实验长期只索引证据路径，当前 parent、最佳版本和本轮候选按需保留加载结果。Diagnosis 的唯一完整结果为该会话的 `investigation.json`；response 返回问题、当前假说、任务和查询数摘要，并引用完整调查，控制端不重新加载查询历史。Editor 从显式证据路径读取 `queries`。SDK 原生对话记录保留在 `runtimes/<timestamp>/sessions/diagnosis_01` 或 `editor_01`，归属写入 job，迭代报告保存引用；历史产物不迁移。
- 候选差异文件与静态验证共用一次文件读取和差异计算，保留完整 patch 及各项验证结果。
- `conclude` 输出 `{tasks:[{id,hypothesis_ids,change,preserve,verification,code_targets}]}`，默认最多 3 项。每项从同一 parent 独立编辑，任务 ID 经过路径安全检查。Editor 只接收自己的任务和相关当前假说，原始查询共用调查文件。
- 自定义配置需包含正整数 `diagnosis.max_tasks`；`max_matches` 须覆盖该数量的候选批次，以及未复用时的初始 parent 批次。设置 `max_tasks=1` 可限制每轮一个任务，归档与历史探索仍然启用。
- 正常且可比较的子代按代码哈希归档并复用证据，即使暂未优于 parent。静态失败及 Bot 错误不入归档。候选先与所选 parent 比较，再用原有规则与当前最佳比较。
- 正常结束的轮次（accept/reject/unresolved）将新对局批次整体保存为同级 `batch_01.zip`，校验成功后移除原目录；错误或中断轮次的新批次保留原样。后续加载时按需解压回原路径，本实验恢复的副本在轮末移除，原 ZIP 不重复压缩。索引路径、日志格式和评估语义不变；归档失败记录到 `experiment.json` 的 `archive_errors`，不改变评估结果。历史 `parent_batch` 若为 ZIP 也可按原 `batch.json` 路径引用，恢复后保持内容只读。
- 各任务使用 `t01` 等短编号保存 patch、validation、evaluation、promotion，`branches` 保留完整 task_id。临时候选位于 `runtimes/<timestamp>/work/candidate_01/code`，归属记录在外层 `workspace.json`，不参与代码哈希；静态通过并冻结后安全回收，失败保留。未开始的任务可因共享预算耗尽而跳过；后续故障不丢弃已完成的有效子代。
- `editor_execution.mode` 明确选择 `local`（默认）或 `docker`。Docker 依赖镜像按根 README 构建；不可用时提前失败。保持现有 LocalConversation worker，容器只隔离 Editor 与其离线检查，不迁移 Diagnosis、控制端验证或 SC2。
- 自动编辑范围为 `bot/components/{behaviors,tasks,tactics}/` 下的 Python 文件。
- 保留缺失数据、执行错误和不确定结果的区别；修改代码结构时不改变指标、预算及评估语义。
- RSI 输出只整理字段：模块异常计数统一为 `exceptions`，Trace 查询不暴露内部 `module_ref`；完整 `metric_names` 仅在 `overview(detail="raw")` 中返回，详细 `result` 不重复顶层的输赢、终止原因和执行状态。Memory 保留分支产物路径，顶层 `artifacts` 不重复这些引用；未执行阶段不生成空摘要，验证错误只在存在时附带文件和行号。保留有效的 0、false、未知值 null 与质量提示，不改变 Bot 日志存储格式。
- Bot 日志 v3 使用原生 `frame`：state 按每 iteration 一次分块记录，events 保存原生回调，trace 紧凑保留全部调用和动作变化。Analyzer 单局计算不读取 manifest/result；产物保存派生列、派生事件、配置、质量信息，以及 `view` 中的标量观测、frame 轴、计数、原生事件和执行摘要。加载新分析不重读原始日志，详细日志在诊断内按需缓存、结束时释放。批次索引分别引用 source 和 analysis，复用历史证据不改原文件。查询使用 `frame=[start,end]`；quality 按 batch/run 区分。只支持当前 Bot 日志格式及含 `view` 的当前分析索引；旧分析需从当前格式日志重新生成，v1/v2 日志不再读取。历史文件保留原样。格式见 [Bot](../bot/README.md)。
- 单局返回值与 `result.json` 使用同一结构；批次只保留单局索引和简短结果。Evaluator 的稳定 `code` 用于流程分支，`reason` 用于解释，不通过英文文案控制流程。
- Editor 会话失败跳过对应候选，单轮运行错误保存后继续，连续 3 轮运行错误停止；正常拒绝和 unresolved 重置错误计数。认证、余额、API 配置错误、快照损坏和用户中断立即停止。请求沿用 SDK 退避，额外重试最多 2 次（总尝试 3 次），不重跑会话或对局。单局有效性检查与双批次比较仍复用同一检查规则。

从项目根目录执行离线验证：

```powershell
python -B -m unittest tests.test_rsi -v
```

该测试使用脚本化模型与对局数据，不启动 SC2 或访问模型服务。
