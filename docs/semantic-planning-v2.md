# 跨字幕事件规划 1.4

日期：2026-11-10。当前新规划默认输出语义契约 1.4；项目配置、章节大纲和数值段落计划仍使用各自原有 1.3 契约。旧语义 1.3 文件仍按原有规则读取，不隐式迁移。

## 已实现

规划前生成独立的 `planning_inputs.json`：保留全部原 cue 的可用性、排除原因与质量标记。零/反向时长、缺少有效终点及超出解码音轨的区间从模型目标中隔离，原始字幕仍保留。模型能看到有效字幕的起止时间、终点来源及质量提示；显示终点不会被标成真实发声终点。

新局部结果包括两个集合：

- `cue_assessments`：每个目标 cue 恰好一条；processed、no_new_information、ambiguous 分别表示产生事件、没有新信息、含义不确定。程序另记录 excluded 和 failed，模型不能自行把有效目标排除或标为处理失败。
- `events`：零到多条；允许同 cue 多事件及跨 cue 证据。`cue_id` 仍为生效锚点，且必须属于当前目标块。`source_cue_ids` 和 `source_evidence` 可以引用提供的目标/邻接上下文；每个来源必须有可核对的连续原文。

`statement_kind` 区分 instruction、description、question、conditional、quotation 和 uncertain。非直接指示、模糊处理结果或未定位事件不会被当作明确指令自动应用；它们保留未解决原因。每个事件仍是未经人工确认的候选。

同 cue 多事件不表示已经获得句内精确时间。当前仍只支持 cue_start、after_utterance 和 uncertain；after_utterance 需要已有边界依据。同一时刻的多个事件继续进入冲突处理，不能通过列表顺序偷偷执行。

## 连续性与章节

1.4 的 no_new_information 只形成处理记录，不触发状态清空。ambiguous 与 failed 仍按融合的 unknown_policy 处理。全部字幕均有处理结果不要求每句都生成一个 unknown 事件。

摘要章节在 1.4 下不再隐式重置状态。可排序但时间重叠的相邻章节使用下一章起点截断前章，并记录 `overlapping_subtitle_chapter_boundary_clipped`。原字幕时间和事件锚点不修改；截断后位于章节范围外的事件仍未解决。章节起点相同、无法得到正长度区间时仍显式失败，需要复核。旧 1.3 的章节重置与重叠失败行为保持原样。

目前没有新增长空档失效策略或设备编排策略。no_new_information 的语义判断必须由实际模型/人工验证，不能靠软件契约证明；长期延续仍需复核。

## 缓存与恢复

提示版本升级为 `two-pass-2`，规划签名包含模型、Schema 消息和新增输入/语义校验实现。新代码拒绝续跑旧实现签名的目录，请指定新输出。新语义事件标识包含来源与语言功能；来源列表顺序改变不产生第二个事件标识。

`planning_run.json` 新增 semantic_schema_version、eligible_cue_count 和 excluded_cue_count。planning_inputs.json 纳入产物哈希，融合时重新计算可用性并核对；失配或排除来源被事件引用会报错。局部语义检查点同时保存处理记录，取消后恢复不丢失零事件区间的覆盖信息。

Schema：`schemas/semantic-batch-v2.schema.json`。原 `schemas/semantic-batch.schema.json` 继续描述旧 1.3。

## 无模型回归

运行全部软件测试：

```text
python tools/run_tests.py
```

对 MP3/SRT 目录进行不调用模型的输入及融合检查：

```text
python tools/check_materials_offline.py --source D:/OSR/od010 --output workspace/od010-offline-new
```

默认使用项目内 `workspace/tools/ffmpeg-binaries/ffmpeg.exe`；其他环境可传 `--ffmpeg`。该脚本要求新输出目录。源 MP3/SRT 在运行前后核对 SHA-256。

脚本使用显式的 OFFLINE_PROTOCOL_FIXTURE，每条有效字幕标为 ambiguous，events 为空。它只验证完整解码、输入隔离、覆盖、契约与融合读取；不解释台词，不评价模型准确率，不执行设备动作，也不导出可用于评价真实效果的动作脚本。项目创建时的默认静止基线仅是创建流程产物。

本机实际三轨回归：

| 轨道 | 字幕总数 | 有效目标 | 隔离 | 处理记录 | 融合读取 |
| --- | --- | --- | --- | --- | --- |
| 04 | 49 | 49 | 0 | 49 | 通过 |
| 05 | 73 | 72 | 1 | 73 | 通过 |
| 06 | 136 | 136 | 0 | 136 | 通过 |

05 轨零时长 cue 已隔离；全部 258 条字幕均保留处理记录。原素材未修改。另用中性测试验证跨句证据、同句多事件、无新信息延续、歧义和非直接指示处置、重复事件、章节重叠及旧版读取。2026-11-10 全量 168 项测试通过。

## 尚未实现或验证

未新增特定内容词表或动作策略，未实现词级对齐、特殊语境自动解释、模型选择/下载或语义驱动多轴生成。缺少本地模型，真实台词理解及实际设备效果尚无法评估。当前改造完成的是输入、事件表达和连续状态的基础，不代表整份路线图已完成。
