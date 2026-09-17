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

每轮只选择一个父节点，再依次生成两个子代，均从同一父提交开始。候选生成与归档串行，每个版本内部的 5 局评测并发。达到轮数上限或没有可扩展节点时结束。

## 模块职责

五个业务模块：context（上下文与记忆）、analysis（问题分析与方案）、tools（工具执行与修改）、evaluation（测试与评估）、evolution（搜索与调度）。llm、process、config 和 console 为共享基础设施。

| 模块 | 职责 |
|---|---|
| `bot/main.py`、`strategy.py`、`economy.py`、`combat.py`、`telemetry.py` | 生命周期、目标选择、经济执行、部队控制、采样与事件日志；当前进化范围 |
| `rsi/analysis/prompts/improve.md` | 改进目标、单次核心方向、边界与完成方式 |
| `rsi/loop.py` | 命令入口、环境预检与启动 |
| `rsi/evolution/runner.py` | 创建实验、基线评测、选择父节点、生成候选、提交和归档 |
| `rsi/context/builder.py` | 汇集入口、文件目录、父节点、血缘、兄弟候选及失败摘要 |
| `rsi/analysis/runner.py` | 分析、工具调用、修复与总结循环，保存对话和判断完成 |
| `rsi/context/feedback.py` | 父节点反馈索引、元数据视图与只读加载 |
| `rsi/tools/__init__.py` | Schema 公共定义与工具组装 |
| `rsi/evaluation/checks.py` | 编译、导入及 Bot 冒烟检查 |
| `rsi/llm/client.py` | 模型调用及累计 token、费用估算 |
| `rsi/tools/edit.py` | 文件读取、搜索和 patch；父版本反馈只读，编辑仅限 Bot |
| `rsi/tools/bash.py` | 受限命令与 finish 检查 |
| `rsi/tools/git.py` | 框架版本操作、修改范围检查及模型只读 git_view |
| `rsi/tools/sc2_api.py` | 本地科技关系、实体能力及分层 API 查询；静态映射和源码索引按进程缓存 |
| `rsi/evaluation/runner.py` | 环境预检、并发对局调度、日志归档与汇总 |
| `rsi/evaluation/game.py` | 独立进程运行单局，捕获游戏错误 |
| `rsi/evaluation/metadata.py` | 校验和统计胜、负、平、崩溃 |
| `rsi/evolution/archive.py` | 节点归档、排名、父节点选择和血缘查询 |
| `rsi/evolution/state.py` | JSON 原子替换保存与已知密钥脱敏 |
| `rsi/process.py` | 子进程环境、输出捕获、超时及进程树终止 |
| `rsi/config.py` | 配置读取和实验参数校验 |
| `rsi/console.py` | 控制台进度行格式：时间戳、阶段与节点标签、逐局符号；过程细节由 RSI_VERBOSE 控制 |

编辑采用单一上下文补丁格式，不要求模型计算行号或行数；旧内容必须精确、唯一匹配，报错指出文件和修改块。

工具的描述、schema 和调用示例定义在各工具模块，`rsi/tools/__init__.py` 提供共同的 schema 构造函数和工具组装入口。

## 候选生成与检查

每个父节点执行 `branch_factor` 次独立尝试，均从父提交开始。初始上下文不注入全部源码；Agent 按需读取代码及 `feedback/` 下的父版本结果。后续尝试可以看到先前兄弟候选的摘要和成绩，但不会继承其代码；提示优先选择不同方向，或明确修正已有尝试的不足。

提示词引导每次候选围绕主要问题形成完整优化，允许必要的跨模块调整；没有独立分析阶段或硬性改动数量限制。节点的 `direction` 保存 Agent 完成摘要。

`max_steps` 按模型调用次数计数，错误与重试也消耗预算。普通文字回复不会结束任务。`finish` 必须单独调用，检查修改范围，并执行编译、接口 Smoke Test 和导入。失败返回 Agent 修复，成功结束工具循环。

外层复核修改范围后提交并评测，不重复执行已成功的 Smoke Test。生成或检查失败记录在对应节点的 `node.json`；只有完成评测和归档的节点进入 Archive。

## 评测与日志

同一候选五局共用一份临时代码，各局使用独立工作目录、临时目录和子进程，5 局每隔 3 秒启动一局。结果按局号汇总，超时计为崩溃。游戏 worker 捕获异常和 SC2 库的 ERROR 日志，退出状态、错误及必要输出合并到总记录。

Bot 日志出口为工作目录中的 `telemetry.json`，约定使用合法 JSON；字段、结构、采样方式及调用位置可由 Agent 修改。初始 Logger 仅记录资源，是可进化的起点。

框架只将文件原样归档为 `game_NN.json`，不解释或验证其内容。单局结果的 `telemetry` 为 `{file, error}`，汇总复用逐局结果；缺失或复制失败不改变胜负。Agent 通过现有文件工具读取父节点的这些日志，无需额外分析工具。

配置、环境、状态、用量和运行汇总写入 `runs/<时间戳>/run.json`，节点树索引独立保存到 `tree.json`。节点使用 a0、a1…，每个节点的 node.json 保存详细元数据、检查及逐局结果；另存 Agent 对话、相对父节点的 diff 和逐局原始轨迹。初版引用 Git commit，不额外保存代码副本。各局临时目录、候选 worktree 和空父目录在结束后清理，候选 Git 分支暂时保留。feedback/metadata.json 从父节点 node.json 生成，不重复落盘。

## 树状搜索

Archive 保留所有完成评测的节点，包括低分和有崩溃的节点；选择时排除有崩溃的节点，按胜场降序、树深度升序、创建顺序升序排名。

每轮从无崩溃、尚未扩展节点中选择一个父节点：默认 80% 从最高胜场组随机选，20% 从所有候选随机选。search.seed 控制独立搜索随机数，search.exploration_rate 控制探索比例；不改变游戏随机性。每轮选择保存到 run.json 的 selections，状态保存只列可选 frontier，不消耗随机数。每个父节点尝试完成后标记 `expanded`，不再扩展。`max_generations` 表示扩展轮数，节点的 `generation` 表示树深度。正常结束时从全部合格节点中选出最佳版本。

这是简单的归档与树状选择机制，不含训练或框架自修改；当前不支持自动续跑。

## 能力边界

- Agent 可通过文件工具改动 `bot/`，保留可安全导入的 `bot.main.SeedBot`（BotAI 子类）；内部模块、架构和日志内容均可进化。
- 框架、提示词、依赖、测试、评测设置和 Git 历史不属于 Agent 的修改范围。
- 命令限于固定检查和搜索；提交、对局评测及节点选择由框架控制。
- 完整轨迹与工作上下文分离；context/window.py 按字符阈值清理较早的大块检索输出，保留最近六轮和调用配对，不增加模型摘要调用，也不保证严格 Token 上限。历史节点记录通过 feedback/history/ 按需只读访问。
- 运行保障是本地实验约束，不是恶意代码安全沙箱；候选 Python 仍以本机用户权限执行。
