# M3：单轴融合草稿与组合预览

日期：2026-11-09

当前已跑通“已保存项目 + 模型规划 + 可选音频证据 + 用户预设 → 段落计划 → 单轴离线草稿、组合 PNG、复核表”。本阶段使用合成音频和脚本化语义验证实现；真实模型语义、真实素材效果、OFS 与 MultiFunPlayer 兼容性尚未完成实测。

## 使用

先完成 create、analyze、plan，再运行：

    python tools/run.py fuse workspace/demo-project --planning workspace/demo-plan --analysis workspace/demo-analysis-v2 --profile config/fusion-example.yaml --output workspace/demo-fused

--analysis 可省略，计划会明确标记 audio_evidence_missing。所有来源必须匹配同一项目、修订、字幕和分析音频；逐一校验已保存产物哈希。输出目录必须是新的独立目录，不能嵌入源项目或分析目录。

完整离线演示：

    python tools/demo_fusion.py --output workspace/m3-demo-new

演示创建 16 秒反相立体声合成音频，并使用固定的中性命令事件运行真实规划保存接口和融合流程。它不请求任何模型服务，不代表模型理解能力。结果位于指定目录的 fusion/ 下。

## 预设与状态机

config/fusion-example.yaml 是离线示例，数值不代表设备限值。presets 定义数值曲线，tier_map 使用 pace/amplitude/style 映射到预设 ID，preset_version 与整个配置快照进入计划。当前只实现 rhythmic 的分段线性周期生成；smooth/irregular 无生成器时保留复核并保持当前位置，不伪装成已实现。

initial_state 默认所有档位 unknown。未知状态不会被当作最低档。unknown_policy 必须显式选择 hold_position 或 maintain_state。示例选择 hold_position：无法解析的语义、时间锚点或状态冲突会让后续保持当前位置，直到明确的绝对档位再次建立可用状态。maintain_state 则保留旧设置并标记原因；这是用户选择的兜底方式，不是对未知内容的推断。

相对加快/减慢沿 slow → medium → fast 变化一档并限制边界。已知绝对档位直接设置目标，不额外再执行一次相对变化；相对方向与绝对档位矛盾进入复核。没有前态时不能仅凭相对提示猜档位，有明确绝对目标时可生成带方向无法验证标记的候选。

pause 只停止后续周期并保持当前位置，不回中、不增加微动；resume 从已保留的完整状态恢复。处于 hold 样式时需要明确的可生成样式才能恢复周期。缺失恢复目标则进入复核。恢复曲线从当前位置启动新的周期；位于上端点时先下行，避免额外停留。当前不保存暂停前完整的周期相位。

current_section 作用于所属章节，章节开始重置到配置初态；next_section 定位到下一章节起点，没有下一章或锚点矛盾时不执行。uncertain 不作为已解析范围。相同事件 ID 不会重复应用；同一时刻多个明确事件保守地标记冲突。明确事件旁的纯 unknown 上下文不当作第二个动作命令，但仍显示复核原因。

每次状态变化保存输入状态、接受事件 ID、输出状态与原因。分段保留完整来源链，不只记最后一条“保持”提示。原项目手工指定的预设段落优先于自动预设；这里只保留段落设置，不是对外部编辑器逐点修改的合并。发现已保存的人工曲线覆盖或锁定范围时直接拒绝融合，等待后续修订保护能力。

## 句尾、音频与分段

cue_start 使用校正后字幕起点。after_utterance 仅在存在可采用边界时定位：

- manual_boundaries_ms：以 cue ID 提供明确边界，检查其落在合法候选跨度内。
- use_empty_timestamp_hints：默认关闭；同时确认 empty_timestamp_convention_confirmed 后，才采用唯一时间的空标记候选。多个不同候选保持未解决。

自动采用的空标记始终是 inferred_draft_boundary，不能升级成人工确认。下一句起点不是实际句尾，不会自动作为 after_utterance 的落点。采用边界后计划分别显示 utterance_candidate 和 inter_cue_gap；这只是候选切分，间隙不等于静音或某类动作。边界不改写原始项目产物。

音频周期候选附加到覆盖区间，供复核比较；与预设周期差异超阈值时记录提示。没有语义而存在周期、保持期间出现周期、证据缺失等情况均有明确原因。音量、周期评分不直接决定动作速度、相位或位置。当前未接入 VAD/PANNs 句尾定位，也未实现自动音频驱动运动或瞬态吸附。

计划完整覆盖 [0, audio_duration_ms)，解释无字幕区间与未知区域。证据分段若使用同一相邻预设，曲线生成前合并，避免“保持”事件重置相位。不同设置衔接保留位置连续性；暂停/未知保持区间显式保留两端位置。所有时间锚点保持原值，不通过延长时间解决超速。

## 导出与复核

| 产物 | 内容 |
| --- | --- |
| segment_plan.json | 段落、状态、事件处置、预设、字幕/音频来源与复核原因 |
| state_transitions.json | 输入/输出状态及接受事件链 |
| utterance_candidates.json | 本次采用或保留未解决的边界候选 |
| generated_base.json | 最终候选点序列，供诊断和后续修订比较 |
| track.funscript | 格式和已配置约束合格时才导出 |
| validation.json | 最终点、线性插值速度、设备配置状态及导出阻断原因 |
| preview.png | 同时间轴的全声道波形包络、段落与候选曲线总览 |
| preview_pages/ | 超过 preview_page_ms 时的分段细图 |
| review.csv | 与段落对应的来源和问题清单 |
| export_manifest.json | 当前修订、输入来源、配置、实现及所有产物哈希 |

PNG 使用 Pillow 直接绘制，避免 GUI 依赖。波形包络分块读取所有声道的极值，不混音；坐标以毫秒数据计算，以秒标注。预览使用英文标签，中文字幕和详细理由保留在 JSON/CSV 中。PNG 和脚本绑定同一个导出修订，图上始终显示需要人工复核。后续外部修订不会自动改变这些静态预览。

无设备限值可以导出明确标识的离线草稿，但不会宣称设备约束通过。已配置范围/速度检查失败时保留计划、候选 JSON、PNG 和诊断报告，阻止写出 track.funscript，CLI 返回 2，预览显示 EXPORT BLOCKED。不会静默修改锁定内容、移动锚点或声称复核完成。

## 验证与剩余工作

测试覆盖绝对/相对提示、重复事件、暂停恢复、未知初态、章节范围、人工边界、空标记歧义、同预设相位连续、人工段落优先、锁定拒绝、音频周期冲突提示、跨修订拒绝、PNG 分页、约束失败禁止脚本导出。

剩余：真实样本校准、VAD/分频句尾候选评估、自动候选接受 profile、真实语义质量和人工精修成本评估。复杂过渡、平滑/不规则生成器、瞬态吸附与局部重生成暂未实现。实际 OFS 载入及音频—脚本—MultiFunPlayer 同步仍待验证。

下一阶段 M4：诊断视频、媒体/脚本同名绑定、外部试听精修与修订版本保存。
