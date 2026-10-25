# RSI 代码导航

RSI 顺序执行指定轮数：初始 parent 对局一次 → 诊断 → 候选编辑 → 静态验证 → candidate 对局 → 复用 parent 结果比较 → 接受或拒绝。接受后直接继承 candidate 代码与对局结果。

业务模块保留在根目录，已有诊断子包保持原样；跨 Bot/RSI 的执行能力集中在项目根目录的 `runner/`，模型接入与候选检查保留在 `tools/`，实验辅助集中在 `experiments.py`。

## 逻辑模块

| 文件 | 职责 |
| --- | --- |
| `run.py` | `run_experiment()` 管理实验及版本继承；`Run.execute()` 管理单轮流程及预算，显式返回 `IterationResult(report, baseline, candidate)` |
| `config.json` | 常用实验设置、运行预算、公共及各角色 LLM 设置 |
| `analyzer.py` | `analyze_run/analyze_batch` 只计算；`analyze_evidence` 统一保存新批次与复用批次的分析，历史输入保持只读 |
| `retriever.py` | 按范围查询分析结果和原始日志，分页限制输出，读取源码、冻结参考资料及经验 |
| `references.py` | 每个实验冻结一次 Knowledge/API；按显式源码根目录读取父版本或候选资料，不依赖版本字典 |
| `diagnosis/session.py` | 诊断 Prompt、工具输入格式及五个诊断工具的装配 |
| `diagnosis/investigate.py` | 问题与假说状态、查询预算与记录、假说修订、结论和代码目标检查 |
| `editor.py` | 编辑 Prompt、候选任务材料与原始调查记录引用、文件编辑器的候选写入边界 |
| `evaluator.py` | 固定指标和接受阈值、批次有效性检查、胜率与核心指标比较 |
| `memory.py` | 保存精简开发经验及产物路径，按条件检索近期经验 |

## 基础工具

| 文件 | 职责 |
| --- | --- |
| `../runner/artifacts.py` | 项目路径与存储路径转换、JSON 读取和原子写入；路径函数支持显式 `root` |
| `tools/config.py` | 读取配置、校验实验参数 |
| `tools/console.py` | SDK 工具操作与结果摘要；普通进度输出复用 `runner/console.py` |
| `tools/llm.py` | 模型设置、OpenHands 会话、模型预算与用量、worker 进程；角色准备和工具装配由 Diagnosis/Editor 提供 |
| `tools/sdk.py` | 将普通 Python 函数适配为 SDK 工具，提供共享参考资料读取格式 |
| `../runner/matches.py` | 独立 Bot 与 RSI 共用的批次并行、单局 worker 和标准对局产物 |
| `../runner/processes.py` | 对局与模型 worker 共用的等待、取消和终止；不解释业务结果 |
| `tools/validate.py` | 候选修改范围、语法与隔离导入检查 |
| `../runner/versions.py` | 单一内容哈希算法、快照、校验、候选复制和差异文件；编辑范围策略留在 RSI |

## 实验辅助

`experiments.py` 只保存诊断消融的 Prompt 调整和固定证据包；接受规则直接定义在 Evaluator，不调用额外模型生成评估方案。

## 修改约定

- 流程分支放在 `Run`，指标计算放在 Analyzer，查询行为放在 Retriever，晋升规则放在 Evaluator。
- SDK 输入格式留在工具边界；内部函数使用明确参数和稳定返回结构。阶段返回业务产物，不定义通用阶段协议。
- 冻结版本、候选目录和参考资料各有独立用途；只有接受后才更新本实验 `experiment.json` 中的当前版本和批次，不使用全局版本指针；Memory 按实验隔离。
- 实验配置只保存到 `experiment.json`；每轮 `iteration.json` 保存状态、摘要及产物路径，不另存 started/final 文件。Knowledge/API 每个实验冻结一次，每轮单独冻结 Memory 输入。
- 模型配置、预检查和 Analyzer 在实验内复用；会话 Token/费用与整轮预算仍逐次计算。每批固定运行 `parallel_matches` 局，seed 为 `1..parallel_matches`。
- Memory 查询显式指定每轮冻结的 `memory.json`，不读取共用 context 中的旧 Memory。
- 单局分析只保存一份，批次 `analysis.json` 保存结果计数、事件计数和单局分析路径，指标统计从单局报告按需计算；Diagnosis 的唯一完整结果为 `sessions/diagnosis/investigation.json`，Editor 直接引用该文件的 `queries`。SDK 原生对话记录保留。
- 候选差异文件与静态验证共用一次文件读取和差异计算，保留完整 patch 及各项验证结果。
- 自动编辑范围为 `bot/components/{behaviors,tasks,tactics}/` 下的 Python 文件。
- 保留缺失数据、执行错误和不确定结果的区别；修改代码结构时不改变指标、预算及评估语义。
- 单局返回值与 `result.json` 使用同一结构；批次只保留单局索引和简短结果。Evaluator 的稳定 `code` 用于流程分支，`reason` 用于解释，不通过英文文案控制流程。
- 指标时间窗口、失败分类与 Editor 修复次数保持原语义；单局有效性检查与双批次比较复用同一检查规则。

从项目根目录执行离线验证：

```powershell
python -B -m unittest scripts.test.test_rsi -v
```

该测试使用脚本化模型与对局数据，不启动 SC2 或访问模型服务。
