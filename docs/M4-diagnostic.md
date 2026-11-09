# M4：诊断视频与 OFS 修订归档

> 本文描述 M4 模块的职责；下文未在 M4 实现的外部差分与局部重生成已有 M5 独立流程。OFS/MultiFunPlayer 的实际往返仍未验收。完整状态见 [开发计划实现审计](implementation-audit.md)。

日期：2026-11-09

已实现 revision-bound 诊断 MP4、同名脚本、实际编码/探测/音频样本检查，以及外部修订的完整独立归档。当前已经在本机生成合成素材视频并检查中文画面；OFS 和 MultiFunPlayer 的实际载入、试听、跳转和保存仍待目标环境验收。

## 生成视频

需要 FFmpeg、FFprobe 与支持中文的字体。配置示例为 config/diagnostic-example.yaml，路径可指定绝对路径；工具相对路径以 profile 所在目录解析，找不到则按 PATH 查找。本阶段不修改系统 PATH，也不会在业务运行时自动下载程序。

本机已准备的独立工具在 workspace/tools/ffmpeg-binaries/，本机 profile 为 workspace/tools/diagnostic-local.yaml。工具来源及哈希记录在 workspace/tools/tool_manifest.json，许可证在工具目录中。该目录被 Git 忽略，不是项目运行依赖的自动安装项。

    python tools/run.py diagnostic workspace/m3-demo-final/project --draft workspace/m3-demo-final/fusion --profile workspace/tools/diagnostic-local.yaml --output workspace/m4-diagnostic-new

通用用法：

    python tools/run.py diagnostic PROJECT --draft FUSED_DRAFT --profile config/diagnostic-example.yaml --output NEW_DIAGNOSTIC_DIR

输出目录必须尚不存在，且位于源项目和草稿目录外。程序核对项目、音频、字幕、计划、曲线和导出修订，拒绝已损坏或被约束检查阻止导出的草稿。

## 编码与显示

默认 1280×720、10 fps、H.264/yuv420p 与 AAC 48kHz/192k。试听音频来自项目保留的主轨分析 PCM 副本，原始素材不改动；这不是任意原轨的无损封装。实际参数、工具版本、二进制/字体哈希、输入与输出哈希写入清单。

Pillow 绘制当前译文、已有说话人标签、时间、当前段落与预设、定性档位、证据 ID 摘要、复核原因及带游标的草稿曲线。字幕通过内存像素传给 FFmpeg，不拼进过滤器或 shell 表达式。保留完整 subtitles.json；画面特别长的文本会换行并截断显示，抽样帧元数据记录截断状态。

不填补未知人物身份。字幕显示终点仍属于字幕显示/候选时间；display_clear 空标记会清除显示，不把它解释为音频实际发音结束。10 fps 的 100 ms 视觉刷新不改写脚本的毫秒时间戳。

所有视频都显示“生成草稿依据”和源修订 ID；诊断内容仅对应生成基线。外部编辑改变脚本后，旧视频不会自动描述新曲线。

## 同步检查

实际运行 FFprobe 读取流起点、时长、首批音频包及 skip-samples 等编码前置数据。再用 FFmpeg 解码 MP4 音频到原分析采样率，检查样本数及末尾差异，并在首、中、尾窗口比较原 PCM 波形的偏移和相关性。

低能量、周期性多解或相关性不足时标为 needs_review，不能因容器起点为零就宣称全程同步。容器帧网格或 AAC 补齐产生的尾部差异单独记录，不通过更改脚本时间修正。阈值是可配置的诊断设置，不是对真实编辑器准确性的承诺。

当前 16 秒周期性演示：视频/音频流均从 0 开始，容器时长 16 秒，回解码样本数一致；首中尾最优偏移均为 0，但波形存在周期多解，所以自动状态仍为 needs_review。噪声与已知 40 ms 偏移另有测试，验证检测器能区分无偏移、明确偏移和静音。

## OFS 外部精修

诊断目录包含同基础名称的 track.mp4 与 track.funscript，脚本副本和融合草稿哈希一致。此命名只提供可验证的配对关系；是否自动载入按实际 OFS 版本验证，不预设所有版本均兼容。

建议在 OFS 中：

1. 打开诊断媒体及对应脚本，核对译文、计划与曲线。
2. 抽查首、中、尾，测试暂停、跳转与试听。
3. 修改后另存到独立目录，保留生成基线。
4. 重开修订结果；记录使用的 OFS 版本、媒体/脚本绑定方式和人工耗时。
5. 在 MultiFunPlayer 中另行确认媒体源、L0 绑定、暂停、跳转、偏移和倍速；导入成功本身不表示整条播放链已经同步。

生成的 compatibility.json 明确保留 not_tested 状态与待核对项。不要直接修改已纳入哈希的诊断产物以冒充新修订；实际测试记录可独立保存，并通过归档 notes 关联。

## 归档修订

    python tools/run.py archive-revision workspace/m3-demo-final/project --baseline workspace/m3-demo-final/fusion --diagnostic workspace/m4-diagnostic --script workspace/ofs-edits/track.funscript --output workspace/revisions/revision-001 --elapsed-seconds 120 --notes "填写实际修改类型与兼容性观察"

elapsed-seconds 可省略，填写时表示用户报告的人工耗时，不是程序测量的编辑时长。

归档包含 external_original.funscript 的逐字节原件、baseline.funscript 的准确基线、validation.json 与 revision_manifest.json。原项目、草稿、媒体和外部输入文件均不覆盖。保留额外格式字段、BOM、原始序列和输入路径；不按“曲线近似相同”归一化或解除保护。

非法 JSON、空文件、重复时间、越界或超限同样可以保存原件，但明确标记格式/约束未通过，不能自动视为用户删除全部内容。完整外部修订默认全部受保护，automatic_merge_allowed=false，不推断局部编辑意图。

提供诊断目录时验证它对应准确基线。外部文件变化后记录旧诊断对该修订已过期；旧视频仍只提供基线背景。当前不实现外部差分、局部保护区间推断或自动合并，这些属于 M5。

## 输出

| 文件 | 用途 |
| --- | --- |
| track.mp4 / track.funscript | 同名诊断媒体与完全一致的基线脚本 |
| diagnostic_manifest.json | 源修订、计划/音频/脚本哈希、编码配置与产物绑定 |
| sync_report.json | 流、样本、编码前置和首中尾定位检查 |
| render_report.json | 实际命令、帧数、抽样帧和耗时 |
| frame-*.png | 首、中、尾抽样画面 |
| compatibility.json | OFS/播放器待验收记录 |
| subtitles.json | 完整字幕来源 |

## 参考与边界

命令行为参考 [FFmpeg 文档](https://ffmpeg.org/ffmpeg.html)和 [FFprobe 文档](https://ffmpeg.org/ffprobe.html)。实际测试工具为 ffmpeg-binaries 1.1.0 所带 FFmpeg/FFprobe 6.0，已记录版本及哈希；没有假定安装包版本等于 FFmpeg 版本。

本地测试覆盖真实编码、同名脚本一致性、中文与特殊字符、缺工具失败、已知偏移、静音、多解状态、外部原件保存、非法修订保护和旧诊断失效标记。视频生成能力不代表已完成 OFS、MultiFunPlayer 或真实素材的端到端验收。
