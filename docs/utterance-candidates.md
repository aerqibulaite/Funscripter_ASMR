# 音频句尾候选与复核

新增独立阶段 `utterance-candidates`：读取已有声学特征，结合字幕搜索范围，提取分频能量下降候选；可选导入预先计算的 VAD 区间。结果默认只用于复核，不自动成为动作锚点，不重算 FFT，也不修改原项目或分析目录。

## 使用

已有项目和对应分析时：

```powershell
python tools/run.py utterance-candidates PROJECT --analysis ANALYSIS --profile config/boundary-example.yaml --output NEW_BOUNDARIES
```

PROJECT、ANALYSIS 和 NEW_BOUNDARIES 替换为实际目录。`--profile` 可省略，采用默认实验参数；输出目录必须是新目录且位于来源目录之外。支持 `--task-file`、任务查询/恢复和 YAML 批量任务。批次参数名为 `project`、`analysis`、`profile`、`vad_evidence`、`output`。

本阶段要求分析属于同一项目修订、字幕和 PCM 音频，特征覆盖与帧时间网格一致；只支持已记录的主轨、样本零点和 `left_edge_no_centering` 时间参考。修改字幕后，先创建新项目并通过 `analyze --reuse-analysis` 复用声学数据、重建摘要，再提取新候选。

上述命令使用当前源码入口；此前归档的 wheel 需要重新构建才包含新功能。

## 检测规则与参数

搜索范围从当前 cue 起点到其候选分段终点（下一非空字幕起点或音轨末尾）。范围只限制搜索，不代表台词持续发音，也不把范围终点当作已确认句尾。

| 参数 | 默认值与含义 |
| --- | --- |
| `bands` | `[mid]`；必须是该分析实际提供的频段名，可配置多个频段 |
| `energy_floor_dbfs` | -70；数字 RMS 下限，不是声压或耳语识别门限 |
| `relative_low_ratio` | 0.2；相对当前 cue 搜索范围峰值的低能量比例 |
| `min_active_ms` | 120；下降前至少持续活动的时间 |
| `min_quiet_ms` | 250；低能量至少持续的时间 |
| `min_delay_ms` | 120；排除过早、缺少当前台词上下文的候选 |
| `merge_window_ms` | 80；将接近的不同证据分组展示，不删除原始候选 |

低能量阈值取数字下限与相对阈值的较大值。按全局样本索引判断活动/低能量持续时间，避免分块累积漂移。候选时间取首个低能量帧的起点，同时保存相邻分析窗的证据支撑区间；该区间不是统计置信区间。

分频能量变化不能证明语音结束。较长句内停顿会保留为候选，并在后续仍有活动时标为 `possible_internal_pause`；多个不同候选组标为 `multiple_audio_end_candidates`。短停顿、过弱、持续活动或不足以支持结束的片段给出明确复核原因，不猜测填满结果。

排序分数使用低能量对比度，仅为 `uncalibrated_rank`，不是正确率或概率。分组也不代表多条证据已经共同确认真实句尾。当前阈值尚未用真实耳语、背景音乐和对白重叠素材校准。

## VAD 导入接口

可使用 `--vad-evidence FILE.json` 导入 [VAD 证据契约](../schemas/vad-evidence.schema.json)。当前不下载权重、不运行 Silero/PANNs 或其他 VAD 模型；实际推理适配器仍待实现。

文件必须记录：

- `schema_version: "1"`、保存的分析 PCM 的 `input_sha256`、`source_start_ms: 0`。
- `status` 为 available、failed 或 disabled，另有 extractor 和 version。
- available 时必须提供 model_sha256、config_sha256，以及整数毫秒 spans（start_ms、end_ms、唯一 evidence_id）。所有区间必须落在同一音轨内。
- failed/disabled 不得携带成功区间；available 但 spans 为空与缺失组件分别记录，不解释为已证明静音。

导入器校验元数据格式、音频绑定与区间范围，不核验声明的权重文件本身。一个 VAD 区间可以横跨多个字幕，一句台词也可以有多个 VAD 区间；这些情况保留来源和歧义。VAD 端点同样不自动作为真实句尾。

## 输出及融合

输出包含 `utterance_candidates.json`、`review.csv`、`profile.resolved.json` 和 `boundary_manifest.json`，分别保存候选及支撑区间、复核清单、有效参数，以及来源/实现/产物哈希。原字幕空标记及其理由保留，机器候选重建时不会覆盖已保存在来源候选文档中的人工确认。

候选数据与参数的契约分别见 [候选 Schema](../schemas/utterance-candidates.schema.json) 和 [参数 Schema](../schemas/boundary-profile.schema.json)。每个 cue 都有记录，包括没有候选的情况；原本无正文的空标记仍按字幕规则处理。

已有规划时，可将候选接入融合复核：

```powershell
python tools/run.py fuse PROJECT --planning PLAN --analysis ANALYSIS --boundaries BOUNDARIES --profile config/fusion-example.yaml --output NEW_FUSION
```

若同时提供 analysis，必须与提取候选时使用的分析清单完全一致。融合会核对来源与全部产物哈希；旧修订、其他分析或被改动的候选文件均被拒绝。单独提供候选时，也必须匹配当前项目、字幕和分析 PCM。

音频/VAD 候选无论分数多高，都不会自动触发 `after_utterance`。相关候选不足/多解原因会以 `utterance_end:` 前缀进入融合计划及 review.csv；区域记录保留候选证据 ID，便于回查。

试听确认后，继续使用融合 profile 的 `manual_boundaries_ms` 填写对应 cue ID 与整数毫秒。来源中已有 `human_boundary` 时优先保留；新 profile 若给出不同值会报冲突，不静默替换。字幕空标记的原有显式启用策略仍按其配置执行。

## 当前验证范围

2026-11-10：合成测试覆盖弱音/低于下限、短停顿、句内长停顿、多频段分组、持续活动、异常字幕、VAD 缺失/失败/空区间/重叠、人工确认保留、来源与时间网格错误、融合候选不自动采用及复核原因传递。

新增 13 项句尾专项与 2 项融合测试，全量 **130 项通过（68.385 秒）**。

实际四秒合成 CLI 示例保留了 1007 ms 和 2304 ms 两个能量下降候选，以及原字幕 2500 ms 空标记，并标明多解/可能句内停顿；没有自动采用任何一个。结果在被 Git 忽略的 `workspace/utterance-validation/`。真实素材误差、漏检率、错误采用率和精修成本仍需目标环境评估。
