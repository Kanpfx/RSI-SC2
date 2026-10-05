# Ares 动作表约定

动作表用于生成模型动作说明，并作为参数解析到 Ares Behavior 的对照接口。模型决定“哪些单位、做什么、对谁或去哪里”；其余参数使用 Ares 默认值或运行时计算。

动作表当前标注为 Ares **3.15.0**，已针对本地对应版本核对构造参数和必填性。`group_actions.json` 包含 6 个具体群体动作，`macro_actions.json` 包含 macro 目录下 15 个具体行为，`individual_actions.json` 包含 26 个具体单体动作。原始定义来源于 Ares 3.14.0，RSI 项目已接入解析、校验和执行调度。

## 字段格式

文件顶层为 `ares_version` 和 `actions`。每个动作包含相同字段：

| 字段 | 含义 |
| --- | --- |
| `name` | Ares 类名，同时作为唯一动作标识 |
| `description` | 简洁英文描述，以实际执行语义为准 |
| `import` | Ares 类的完整导入路径 |
| `enabled` | 是否向模型展示并允许模型调用 |
| `params` | 完整构造参数列表，模型必填参数排在前面 |

不设置额外的 `id`、`actors` 或 `choices`。每个参数固定包含以下 6 个字段，不省略；不适用的值填 `null`：

| 字段 | 含义 |
| --- | --- |
| `name` | Ares 构造参数名 |
| `type` | 解析后的类型，如 `Unit`、`Point2`、`list[Unit]` |
| `description` | 简洁英文描述 |
| `source` | `model`、`fixed` 或 `runtime` |
| `required` | 按 Ares 构造函数填写：无默认值为 `true`，有默认值为 `false` |
| `value` | 模型参数填 `null`；固定参数填实际值；运行时参数填解析器名称 |

`required` 与来源无关：内部参数也可能必填。允许 `null` 不等于允许省略，例如 `GroupUseAbility.target` 仍为必填。

## 参数来源与控制粒度

| 来源 | 分配原则 | 示例 |
| --- | --- | --- |
| `model` | 模型选择执行者、操作目标和技能 | 己方单位 ID、目标坐标或 ID、敌方 ID 列表、技能名称 |
| `fixed` | 使用对应版本 Ares 的原有默认值，不额外调参 | 同步施法、寻路精度、危险阈值 |
| `runtime` | 从本帧状态和已解析的模型参数计算 | tags、群体中心、敌群中心、影响网格 |

只有 `source: "model"` 的参数展示给模型并允许提交。单位以字符串 ID 表示，坐标使用 `{ "x": 28, "y": 60 }`，技能使用 `AbilityId` 名称。

例如，下面的参数由内部提供，但对 Ares 来说仍是必填：

```json
{
  "name": "group_tags",
  "type": "set[int]",
  "description": "SC2 tags derived from group.",
  "source": "runtime",
  "required": true,
  "value": "group_tags"
}
```

解析器名称直接存入 `value`，不单设 `resolver` 字段。当前取值约定如下：

| `value` | 取值规则 |
| --- | --- |
| `group_tags` | 从选定己方单位提取真实 SC2 tags |
| `group_center` | 选定己方单位当前位置的算术中心 |
| `enemy_center` | 选定 `enemies` 的位置算术中心；列表为空时跳过该行为 |
| `group_grid` | 全地面群体用 `mediator.get_ground_grid`，全空中群体用 `mediator.get_air_grid`；混合空地群体要求拆分 |

这些取值规则是本项目的适配约定。Ares Behavior 本身可能包含自动寻路、选敌或拉扯决策；使用默认参数不代表行为没有自动化。

## Macro 补充约定

宏观动作沿用相同字段。模型提供必要的建筑/兵种/升级类型、基地坐标、目标总数或军队配比；可选参数全部保留 Ares 默认值，不由 runtime 推断经济或战术目标。`to_count` 表示目标总数，不是本次新增数量。

- `UnitTypeId`、`UpgradeId` 使用枚举名称；`army_composition_dict` 如 `{"MARINE": {"proportion": 0.7, "priority": 0}, "MEDIVAC": {"proportion": 0.3, "priority": 1}}`。配比之和为 1，优先级为 0–10，数值越小越优先。
- `AddonSwap.structure_needing_addon` 对模型只接受己方建筑 ID；`type` 保留 Ares 的完整类型。附件由 Ares 按类型自动选择。
- `SpeedMining` 虽继承单体战斗接口，但位于 macro 目录并由该模块导出，因此收录于此。`target` 对模型只接受矿块或己方气矿建筑 ID；源码虽标注 `Point2 | Unit`，执行时实际读取 Unit 属性，不能直接传坐标。
- `SpeedMining.worker_position` 使用 runtime `worker_position`，取选定工人的当前位置。`resource_target_pos` 使用同名 runtime：矿块从 `mediator.get_mineral_target_dict[target.position]` 获取接近点，缺失时跳过；己方气矿建筑沿目标到工人的方向偏移 `2.75 * 1.21`，与 Ares Mining 的计算一致。
- `BuildStructure.base_location` 是选址参考基地，不是精确落点；工人和最终位置由 Ares 选择。扩张、造气矿、生产、科技和升级控制器也保留各自的自动选择逻辑。
- `Mining` 没有模型参数，默认每气矿 3 个工人，并自动分配采矿、避险和自卫；当前表不开放经济比例调节。
- 神族专用 `ProtossStaticDefence`、`RestorePower` 保留在表中，但人族项目默认 `enabled: false`。`MacroBehavior` 是接口，`MacroPlan` 是调度容器，不导出为动作。
- 为完整对照构造函数，内部状态字段也保留为 `fixed` 默认值。双下划线字段使用实际构造参数名（如 `_SpawnController__build_dict`）；空集合以 `[]` 表示，解析时按 `type` 转为 set，tuple 同理。可变默认容器须逐实例新建，持续行为中的内部状态不能每帧重置。

## Individual 补充约定

模型选择己方单位 ID、目标单位 ID 或坐标、候选单位列表和技能。敌方列表使用当前可见单位；治疗和装载列表使用对应己方/友方单位。列表类型标注保留 Ares 原型，JSON 输入统一为 ID 数组。

| runtime `value` | 取值规则 |
| --- | --- |
| `unit_grid` | 根据执行单位 `unit.is_flying` 选择 `mediator.get_air_grid` 或 `mediator.get_ground_grid` |
| `unit_position` | 执行单位当前位置；用于填充当前源码未使用的必填位置参数 |
| `aoe_min_targets` | 沿用 `AutoUseAOEAbility` 的门槛：腐蚀胆汁、死神手雷为 1；EMP 对非神族为 2；其余为 4 |
| `unit_planned_path` | 执行层已记录的该单位移动路径，转换为非空 `list[Point2]`；没有路径时拒绝该动作，不推断目的地 |
| `aoe_ability_delay` | 已核实的技能延迟表；目前仅记录 `KD8CHARGE_KD8CHARGE: 34`，来自 Ares `ReaperGrenade`；其他技能拒绝，不猜测延迟 |

- `UseAbility.target` 是模型可选参数，`required: false`；省略或 `null` 表示无目标施法。不能因其有默认值就固定为 `null`，否则无法指定施法目标。
- 所有显式 `grid` 都由 runtime 提供，包括 `StutterUnitBack.grid`（`required: false`），避免其默认回退到地面网格而错误处理空军。
- `UseAOEAbility.min_targets` 没有构造默认值，因此仍为 `required: true`；runtime 套用上述 Ares 现有策略，这是适配约定，不是该类的默认参数。若要精确指定技能落点或目标，使用 `UseAbility`。
- `DropCargo.target`、`ReaperGrenade.retreat_target`、`TumorSpreadCreep.target` 当前源码不读取，统一填入 `unit_position`。卸载发生在当前位置；运输到指定位置使用 `PickUpAndDropCargo`。
- `AttackTarget.extra_range`、`NydusPathUnitToTarget.large` 当前未使用；`MoveToSafeTarget` 未转发 `danger_distance` 和 `danger_threshold`。表中保留原参数及默认值，并在描述中注明。
- `GhostSnipe` 原生实现只在对手为虫族时工作；狙击、治疗、AOE、架坦克等专用行为仍含自动选目标或时机判断。`AMove` 默认距离目标小于 7 时不再发出攻击命令；`UseAOEAbility` 默认不避开己方单位，均保留上游行为。
- 虫族专用 `NydusPathUnitToTarget`、`QueenSpreadCreep`、`TumorSpreadCreep`、`UseTransfuse` 默认关闭。`PlacePredictiveAoE` 也先关闭：上游标记为开发中，且独立调用需要执行层维护移动路径和技能延迟表。RSI 已实现 runtime 参数注入；没有记录的路径或已知技能延迟时拒绝相关动作。
- `CombatIndividualBehavior` 是接口，不导出。`SpeedMining` 已在 macro 表中收录，不重复导出。

## 后续对齐规则

- 以对应版本源码的构造函数和执行代码为准，核对完整参数、必填性、默认值及实际语义。
- 通用解析负责 ID、坐标和枚举转换，再注入 fixed/runtime 值；必要的专属检查留在代码中，不在 JSON 中扩展规则语言。
- 先解析模型参数，再计算依赖它们的 runtime 值；参数排列用于阅读，不决定解析依赖顺序。
- `enabled` 是静态开放开关，不保证当前可执行；可用性由运行时判断。
- 持续执行、终止和重新校验属于执行层。升级 Ares 时重新核对表内固定默认值。
