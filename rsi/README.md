# RSI 代码导航

RSI 顺序执行指定轮数：初始 parent 对局一次 → 诊断 → 候选编辑 → 静态验证 → candidate 对局 → 复用 parent 结果比较 → 接受或拒绝。接受后直接继承 candidate 代码与对局结果。

业务模块保留在根目录，已有诊断子包保持原样；基础能力集中在 `tools/`，实验辅助集中在 `experiments.py`。

## 逻辑模块

| 文件 | 职责 |
| --- | --- |
| `run.py` | `run_experiment()` 管理实验及版本继承；`Run.execute()` 管理单轮诊断、编辑、静态验证和候选对局，以及整轮预算和产物 |
| `config.json` | 常用实验设置、运行预算、公共及各角色 LLM 设置 |
| `analyzer.py` | 从原始日志计算状态指标、单位损失、动作执行统计和描述性事件；输出静态单局及批次报告 |
| `retriever.py` | 按范围查询分析结果和原始日志，分页限制输出，读取源码、冻结参考资料及经验 |
| `diagnosis/session.py` | 诊断 Prompt、工具输入格式及五个诊断工具的装配 |
| `diagnosis/investigate.py` | 问题与假说状态、查询预算与记录、假说修订、结论和代码目标检查 |
| `editor.py` | 编辑 Prompt、候选任务材料与原始调查记录引用、文件编辑器的候选写入边界 |
| `evaluator.py` | 固定指标和接受阈值、批次有效性检查、胜率与核心指标比较 |
| `memory.py` | 保存精简开发经验及产物路径，按条件检索近期经验 |

## 基础工具

| 文件 | 职责 |
| --- | --- |
| `tools/artifacts.py` | 项目路径与存储路径转换、JSON 读取和原子写入 |
| `tools/config.py` | 读取配置、校验实验参数 |
| `tools/console.py` | 显示进度、工具操作与结果摘要；显示错误不影响研究流程 |
| `tools/llm.py` | 模型设置、工具选择、OpenHands 会话、模型预算与用量、worker 进程 |
| `tools/sdk.py` | 将普通 Python 函数适配为 SDK 工具，提供共享参考资料读取格式 |
| `tools/matches.py` | 批次并行、单局 worker、超时终止、对局结果与基础设施错误记录 |
| `tools/validate.py` | 候选修改范围、语法与隔离导入检查 |
| `tools/versions.py` | 内容哈希快照、候选复制和差异文件 |

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

从项目根目录执行离线验证：

```powershell
python -B -m unittest scripts.test.test_rsi -v
```

该测试使用脚本化模型与对局数据，不启动 SC2 或访问模型服务。
