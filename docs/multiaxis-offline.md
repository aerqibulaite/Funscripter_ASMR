# 多轴离线数据契约与数值草稿

这是阶段 8 的准备工作：复用已保存项目的时长、分析音频零点和原有 L0 限值，用显式数值配置生成多轴测试草稿。当前不把字幕模型结果自动映射到其他轴；也没有完成播放器或设备验收。

## 使用

使用一个新的合成项目，避免覆盖已有成果：

```powershell
python tools/make_demo.py
python tools/run.py create --config workspace/demo-input/config.yaml --output workspace/axis-source-new
python tools/run.py multiaxis-draft workspace/axis-source-new --profile config/multiaxis-offline-example.yaml --output workspace/axis-draft-new
```

输出目录必须是新目录，且位于源项目之外。[示例配置](../config/multiaxis-offline-example.yaml) 需要至少 10 秒时长，只启用 L0 和 R0，其余四轴不输出任何脚本。[JSON Schema](../schemas/multiaxis-profile.schema.json) 描述字段，运行时另行校验启用关系、环和数值约束。

此命令支持 `--task-file`、`task-status`、`task-resume`，也可以作为批次中的 `command: multiaxis-draft`，参数为 `project`、`profile`、`output`。此前归档 wheel 不包含本命令，部署时需从当前源码重新构建。

## 配置

`schema_version` 为字符串 `"1"`，与原单轴项目配置独立。`basename` 默认 track，只允许可移植的字母/数字/下划线/短横线名称；路径分隔符和保留设备名会被拒绝。

`axes` 仅接受 L0、L1、L2、R0、R1、R2。每轴默认 `enabled: false`，至少显式启用一轴；L0 也可以不启用，不会因启用辅助轴而隐式补出主轴。

| 字段 | 行为 |
| --- | --- |
| `mode: independent` | 必须提供 `generation`，沿用现有 initial_position、presets 和 segments；无分段区域保持当前位置 |
| `mode: linked` | 必须提供 `link`，不能同时填写独立 generation |
| `link.source_axis` | 跟随另一已启用轴；允许链式跟随，禁止自环、环和跟随未启用轴 |
| `link.scale` / `link.offset` | `目标位置 = round(offset + scale × 源位置)`，负 scale 可反向，时间戳完全保留 |
| `device` | 本轴的脚本位置范围、归一化速度限制和 linear 插值；未填写不能视为完整限值检查 |
| `calibration_reference` | 人工记录的校准出处，仅存档，不自动将 hardware_verified 改为 true |

联动使用有理数计算后取最近整数，中点采用 Python round 的偶数舍入规则；链中每一轴跟随上一轴的实际整数点。超出 0–100 的位置不静默截断，会使整组导出失败。独立生成继续采用现有周期曲线规则，不增加平滑或不规则生成器。

L0 的有效范围取 profile 与源项目范围的交集，有效速度上限取两者中较小值；因此新配置可以收紧原 L0 限制，不能放宽它。范围冲突时输出失败报告。辅助轴分别使用本轴配置，不继承主轴的物理方向假设。

源项目存在已保存的人工覆盖或锁定时拒绝生成，等待修订保护流程处理。独立目录不会覆盖原项目、原草稿或外部修订。

## 可选的数值联动约束

`position_budgets` 可显式设置归一化位置预算：

```yaml
position_budgets:
  - rule_id: numeric_example
    references: {L0: 50, R0: 50}
    max_total_deviation: 20.0
```

含义是任意时刻 `|L0位置 − 50| + |R0位置 − 50| ≤ 20`。至少涉及两个已启用轴，规则 ID 不重复。这只是用户定义的数值条件，不表示设备的机械碰撞、实际行程或动力学约束。

检查基于各轴全部转折点的并集，在相同零点上用有理数计算线性插值。每个相邻并集区间内，绝对仿射函数之和是凸函数，最大值在端点取得，所以无需按视频帧率稀疏采样。报告同时保存精确分数、显示数值、峰值时刻和检查点数。

## 输出与失败行为

| 文件 | 含义 |
| --- | --- |
| `profile.resolved.json` | 独立配置快照 |
| `candidate_curves.json` | 每个启用轴的候选点集；无法生成的轴为 null，原因在报告中 |
| `validation.json` | 每轴格式/范围/速度、有效限值、联动规则和整体阻断结果 |
| `multiaxis_manifest.json` | 输出修订 ID、源项目修订、音频与配置哈希、启用轴、文件映射和产物哈希 |
| `*.funscript` | 所有启用轴与联动规则都通过数值检查时，才一次性导出整组 |

任一轴或联动规则失败时，保留候选和诊断，但不输出任何轴脚本；CLI 返回 2。成功生成离线草稿返回 0，任务执行状态 completed，业务状态仍为 needs_review。

文件名按仓库开发方案约定：L0 为 `track.funscript`，L1 为 `track.surge.funscript`，L2 为 `track.sway.funscript`，R0 为 `track.twist.funscript`，R1 为 `track.roll.funscript`，R2 为 `track.pitch.funscript`。使用其他 basename 时替换 track。实际外部工具的绑定仍需测试。

清单固定说明 `purpose: offline_axis_contract_fixture`、`semantic_plan_applied: false`、`playback_verified: false`、`hardware_verified: false`。填写全部数值限值不改变这些事实。

## 验证与剩余工作

2026-11-09：新增 13 项测试，全量 **115 项通过**。覆盖六轴命名、独立与链式跟随、未启用轴不输出、主轴限值继承、超速/越界/非法分段、精确联动预算、失败保留诊断、人工锁定、禁止覆盖及批量任务接入。

实际 CLI 示例使用新生成的 12 秒 PCM 项目：只输出 `track.funscript` 和 `track.twist.funscript`，其他轴缺席；数值联动规则通过。旧 `workspace/demo-project/project.json` 首次运行时出现 Permission denied，未修改其权限或内容，改用独立合成项目完成验证。

后续仍需：真实单轴同步验收；确认目标设备的轴方向、行程与联动限制；外部播放器多轴绑定、暂停、跳转、倍速；与真实语义融合计划、诊断视频及多轴人工修订保护整合；长轨多轴资源测量。当前没有声道差自动映射或模型自行生成六轴轨迹。
