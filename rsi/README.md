# RSI 代码导航

RSI 按指定轮数扩展归档：选择 parent → 诊断并提出多个独立任务 → 各自编辑、验证和对局 → 比较 parent 与当前最佳 → 记录谱系与归档。每次 Diagnosis/Editor 会话独立使用模型额度；对局数和运行时长按轮限制。较弱但有效的版本可以继续探索，当前最佳单独维护。

业务模块保留在根目录，已有诊断子包保持原样；跨 Bot/RSI 的执行能力集中在项目根目录的 `runner/`，模型接入与候选检查保留在 `tools/`，实验辅助集中在 `experiments.py`。

## 逻辑模块

| 文件 | 职责 |
| --- | --- |
| `run.py` | `run_experiment()` 管理归档、当前最佳及探索 parent；`Run.execute()` 管理单轮多任务与共享预算，返回 `IterationResult(report, baseline, candidate, children)` |
| `config.json` | 常用实验设置、运行预算、公共及各角色 LLM 设置 |
| `analyzer.py` | `analyze_run/analyze_batch` 只计算；`analyze_evidence` 统一保存新批次与复用批次的分析，历史输入保持只读 |
| `retriever.py` | 按范围查询分析结果和原始日志，分页限制输出，读取源码、冻结参考资料及经验 |
| `references.py` | 按内容哈希跨实验复用 Knowledge/API 快照并校验完整性；按显式源码根目录读取父版本或候选资料 |
| `workspace.py` | 短路径分配、SDK 落盘预检查、临时工作区归属与安全清理 |
| `diagnosis/session.py` | 诊断 Prompt、工具输入格式及五个诊断工具的装配 |
| `diagnosis/investigate.py` | 问题与假说状态、查询预算与记录、假说修订、结论和代码目标检查 |
| `editor.py` | 编辑 Prompt、候选任务材料与原始调查记录引用、文件编辑器的候选写入边界 |
| `evaluator.py` | 固定指标和接受阈值、批次有效性检查、胜率与核心指标比较 |
| `search.py` | 按内容哈希索引有效版本及证据引用；交替扩展当前最佳与较少探索的历史版本 |
| `memory.py` | 保存精简开发经验及产物路径，按条件检索近期经验 |

## 基础工具

| 文件 | 职责 |
| --- | --- |
| `../runner/artifacts.py` | 项目路径与存储路径转换、JSON 读取和原子写入；路径函数支持显式 `root` |
| `tools/config.py` | 读取配置、校验实验参数 |
| `tools/console.py` | SDK 工具操作与结果摘要；普通进度输出复用 `runner/console.py` |
| `tools/llm.py` | `ExperimentRuntime` 持有实验级 SDK worker；逐次创建独立 OpenHands 会话、记录模型预算与用量，角色准备和工具装配由 Diagnosis/Editor 提供 |
| `tools/sandbox.py` | 可选实验级 Docker Editor 容器的预检查、固定挂载、逐任务路径转换和容器回收；不更改会话内部流程 |
| `tools/sdk.py` | 将普通 Python 函数适配为 SDK 工具，提供共享参考资料读取格式 |
| `../runner/matches.py` | 独立 Bot 与 RSI 共用的批次并行、单局 worker 和标准对局产物 |
| `../runner/processes.py` | 对局与模型 worker 共用的等待、取消和终止；不解释业务结果 |
| `tools/validate.py` | 候选修改范围、语法与独立子进程导入检查，不提供系统权限隔离 |
| `../runner/versions.py` | 单一内容哈希算法、快照、校验、候选复制和差异文件；编辑范围策略留在 RSI |

## 实验辅助

`experiments.py` 只保存诊断消融的 Prompt 调整和固定证据包；接受规则直接定义在 Evaluator，不调用额外模型生成评估方案。

## 修改约定

- 流程分支放在 `Run`，指标计算放在 Analyzer，查询行为放在 Retriever，晋升规则放在 Evaluator。
- SDK 输入格式留在工具边界；内部函数使用明确参数和稳定返回结构。阶段返回业务产物，不定义通用阶段协议。
- `experiment.json` 中的 `current_parent/current_batch` 表示当前最佳；`archive.json` 保存版本节点、证据引用、扩展次数与谱系边。每轮报告的 `parent` 是实际探索版本。Memory 与归档按实验隔离，不使用全局版本指针。
- 实验配置只保存到 `experiment.json`；每轮 `iteration.json` 保存状态、摘要及产物路径，不另存 started/final 文件。Knowledge/API 按内容保存在共用 `refs/<hash>`，实验和轮次记录引用；每轮单独冻结 Memory 输入。
- 一次命令对应一次实验；`run_experiment` 持有唯一 `ExperimentRuntime` 并传给所有轮次，结束、异常和中断时统一关闭。本地顺序复用一个 SDK worker；Docker 模式额外复用一个 Editor 容器及其 worker。每次调用仍新建并关闭 Conversation、LLM 和工具，不改变修复对话；候选导入验证和对局保留独立子进程。
- 模型配置、预检查和 Analyzer 在实验内复用。`model.max_total_tokens/max_cost/max_iterations` 是每次会话的默认上限，`model.diagnosis/editor` 可分别覆盖；每个独立任务及修复会话重新获得完整额度，历史消耗只统计、不扣减后续额度。每批固定运行 `parallel_matches` 局，seed 为 `1..parallel_matches`。
- Memory 查询显式指定每轮冻结的 `memory.json`，不读取共用 context 中的旧 Memory。
- 实验目录使用本地时间到秒的 `YYYYMMDD_HHMMSS`，同秒重名追加顺序编号；runtime 与实验同名。轮次为 `iteration_01`，batch、candidate 和各角色会话在实验内连续编号，单局在 batch 内为 `run_01`。候选清理后不重用编号；版本和参考资料的内容哈希不变。
- 单局分析只保存一份，批次 `analysis.json` 保存结果计数、事件计数和单局分析路径，指标统计从单局报告按需计算；Diagnosis 的唯一完整结果为该会话的 `investigation.json`，Editor 从显式证据路径读取 `queries`。SDK 原生对话记录保留在 `runtimes/<timestamp>/sessions/diagnosis_01` 或 `editor_01`，归属写入 job，迭代报告保存引用；历史产物不迁移。
- 候选差异文件与静态验证共用一次文件读取和差异计算，保留完整 patch 及各项验证结果。
- `conclude` 输出 `{tasks:[{id,hypothesis_ids,change,preserve,verification,code_targets}]}`，默认最多 3 项。每项从同一 parent 独立编辑，任务 ID 经过路径安全检查。Editor 只接收自己的任务和相关当前假说，原始查询共用调查文件。
- 自定义配置需包含正整数 `diagnosis.max_tasks`；`max_matches` 须覆盖该数量的候选批次，以及未复用时的初始 parent 批次。设置 `max_tasks=1` 可限制每轮一个任务，归档与历史探索仍然启用。
- 正常且可比较的子代按代码哈希归档并复用证据，即使暂未优于 parent。静态失败及 Bot 错误不入归档。候选先与所选 parent 比较，再用原有规则与当前最佳比较。
- 各任务使用 `t01` 等短编号保存 patch、validation、evaluation、promotion，`branches` 保留完整 task_id。临时候选位于 `runtimes/<timestamp>/work/candidate_01/code`，归属记录在外层 `workspace.json`，不参与代码哈希；静态通过并冻结后安全回收，失败保留。未开始的任务可因共享预算耗尽而跳过；后续故障不丢弃已完成的有效子代。
- `editor_execution.mode` 明确选择 `local`（默认）或 `docker`。Docker 依赖镜像按根 README 构建；不可用时提前失败。保持现有 LocalConversation worker，容器只隔离 Editor 与其离线检查，不迁移 Diagnosis、控制端验证或 SC2。
- 自动编辑范围为 `bot/components/{behaviors,tasks,tactics}/` 下的 Python 文件。
- 保留缺失数据、执行错误和不确定结果的区别；修改代码结构时不改变指标、预算及评估语义。
- Bot 日志 v3 使用原生 `frame`：state 按每 iteration 一次分块记录，events 保存原生回调，trace 紧凑保留全部调用和动作变化。Analyzer 单局计算不读取 manifest/result，产物只含派生列、派生事件、配置和质量信息。批次索引分别引用 source 和 analysis，复用历史证据不改原文件。查询使用 `frame=[start,end]`，新增 `state` 查询；quality 按 batch/run 区分。旧原始日志仍支持，旧分析需重新生成。格式见 [Bot](../bot/README.md)。
- 单局返回值与 `result.json` 使用同一结构；批次只保留单局索引和简短结果。Evaluator 的稳定 `code` 用于流程分支，`reason` 用于解释，不通过英文文案控制流程。
- 指标时间窗口、失败分类与 Editor 修复次数保持原语义；单局有效性检查与双批次比较复用同一检查规则。

从项目根目录执行离线验证：

```powershell
python -B -m unittest scripts.test.test_rsi -v
```

该测试使用脚本化模型与对局数据，不启动 SC2 或访问模型服务。
