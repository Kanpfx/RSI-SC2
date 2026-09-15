# 架构与数据流

RSI 是固定的进化调度器，一个自主代码 Agent 负责修改 `bot/`。模型决定如何改进 Bot；外层负责版本、检查、真实对局和节点选择。

## 全流程

```text
Seed 提交 → 5 局基线评测 → Archive
                              ↓
                        选择未扩展父节点
                              ↓
              从父提交创建独立候选 worktree
                              ↓
                  Agent 阅读、分析、patch 修改
                              ↓
                  finish 检查 ← 失败后修复
                              ↓ 通过
                     框架提交 → 5 局评测
                                   ↓
                                Archive
```

每轮先冻结父节点列表，再依次完成各父节点的候选尝试。候选生成与归档串行，每个版本内部的 5 局评测并发。达到轮数上限或没有可扩展节点时结束。

## 模块职责

| 模块 | 职责 |
|---|---|
| `bot/main.py`、`bot/modules/` | Bot 入口及经济、战斗、策略、观测、日志；当前进化范围 |
| `prompts/improve.md` | 改进目标、单次核心方向、边界与完成方式 |
| `rsi/loop.py` | 创建实验、基线评测、选择父节点、生成候选、提交和归档 |
| `rsi/agent/context.py` | 汇集入口、文件目录、父节点、血缘、兄弟候选及失败摘要 |
| `rsi/agent/runner.py` | 组装工具 schema、分发调用、回传结果、保存对话和判断完成 |
| `rsi/llm/client.py` | 模型调用及累计 token、费用估算 |
| `rsi/tools/edit.py` | 文件读取、搜索和 patch；父版本反馈只读，编辑仅限 Bot |
| `rsi/tools/bash.py` | 受限命令与 finish 检查 |
| `rsi/tools/git.py` | 框架版本操作、修改范围检查及模型只读 git_view |
| `rsi/tools/sc2_api.py` | 查询本地已安装的 SC2 API |
| `rsi/evaluation/runner.py` | 环境预检、并发对局调度、日志归档与汇总 |
| `rsi/evaluation/game.py` | 独立进程运行单局，捕获游戏错误 |
| `rsi/evaluation/metadata.py` | 校验和统计胜、负、平、崩溃 |
| `rsi/evolution/archive.py` | 节点归档、排名、父节点选择和血缘查询 |
| `rsi/evolution/state.py` | JSON 原子替换保存与已知密钥脱敏 |
| `rsi/process.py` | 子进程环境、输出捕获、超时及进程树终止 |
| `rsi/config.py` | 配置读取和实验参数校验 |

工具的描述、schema 和调用示例定义在各工具模块，`rsi/tools/__init__.py` 提供共同的 schema 构造函数。

## 候选生成与检查

每个父节点执行 `branch_factor` 次独立尝试，均从父提交开始。初始上下文不注入全部源码；Agent 按需读取代码及 `feedback/` 下的父版本结果。后续尝试可以看到先前兄弟候选的摘要，但不会继承其代码。

提示词引导每次候选围绕主要问题形成完整优化，允许必要的跨模块调整；没有独立分析阶段或硬性改动数量限制。节点的 `direction` 保存 Agent 完成摘要。

`max_steps` 按模型调用次数计数，错误与重试也消耗预算。普通文字回复不会结束任务。`finish` 必须单独调用，检查修改范围，并执行编译、接口 Smoke Test 和导入。失败返回 Agent 修复，成功结束工具循环。

外层复核修改范围后提交并评测，不重复执行已成功的 Smoke Test。生成或检查失败记录在 `failures.json`；只有完成评测和归档的节点进入 Archive。

## 评测与日志

每局使用独立 Bot 副本、工作目录、临时目录和子进程，5 局每隔 3 秒启动一局。结果按局号汇总，超时计为崩溃。游戏 worker 捕获异常和 SC2 库的 ERROR 日志，进程输出另行保存。

Bot 日志出口为工作目录中的 `telemetry.json`，约定使用合法 JSON；字段、结构、采样方式及调用位置可由 Agent 修改。初始 Logger 仅记录资源，是可进化的起点。

框架只将文件原样归档为 `game_NN.telemetry.json`，不解释或验证其内容。单局结果的 `telemetry` 为 `{file, error}`，汇总复用逐局结果；缺失或复制失败不改变胜负。Agent 通过现有文件工具读取父节点的这些日志，无需额外分析工具。

配置、环境、节点、对话、检查、战绩、用量和运行状态保存在 `runs/<run_id>/`。候选 Git worktree 在尝试结束后清理，候选分支及节点目录内的单局工作目录保留。产物路径和调试入口见 [README.md](README.md#日志与调试)。

## 树状搜索

Archive 保留所有完成评测的节点，包括低分和有崩溃的节点；选择时排除有崩溃的节点，按胜场降序、树深度升序、创建顺序升序排名。

每轮从整个历史 Archive 中选择最多 `beam_width` 个尚未扩展的父节点。每个父节点尝试完成后标记 `expanded`，不再扩展。`max_generations` 表示扩展轮数，节点的 `generation` 表示树深度。正常结束时从全部合格节点中选出最佳版本。

这是简单的归档与树状选择机制，不含训练或框架自修改；当前不支持自动续跑。

## 能力边界

- Agent 可通过文件工具改动 `bot/`，保留可安全导入的 `bot.main.SeedBot`（BotAI 子类）；内部模块、架构和日志内容均可进化。
- 框架、提示词、依赖、测试、评测设置和 Git 历史不属于 Agent 的修改范围。
- 命令限于固定检查和搜索；提交、对局评测及节点选择由框架控制。
- 上下文由初始摘要和后续工具结果累积，没有自动压缩或长期记忆机制。
- 运行保障是本地实验约束，不是恶意代码安全沙箱；候选 Python 仍以本机用户权限执行。
