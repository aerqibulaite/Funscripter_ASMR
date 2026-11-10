# 视频与配套字幕

界面“输入与项目”可选择 MP4、MKV、WebM、MOV 等视频，并搭配 VTT / SRT / LRC。需要可用的 FFmpeg 和 FFprobe。音轨序号按音频流从 0 开始计数，不是文件中的全局流编号；例如 0 为第一条音轨，1 为第二条。当前一次只选一条音轨。

CLI 仍沿用 `audio` 字段，兼容旧配置：

```yaml
audio: movie.mp4
ffmpeg: ffmpeg
ffprobe: ffprobe
audio_stream: 0
subtitles:
  path: movie.vtt
  format: auto
  encoding: utf-8-sig
  offset_ms: 0
```

程序会探测音视频流，保存时间、编码、音轨选择等元数据，并排除封面图造成的视频误判。`ffprobe` 未改动默认值时优先使用 FFmpeg 同目录下的 FFprobe。没有音轨、序号越界、视频探测缺失或无法确认时间起点时明确报错；原文件不改写。

视频使用容器播放时间轴：容器起点归零，选中音轨的延迟及时间戳间隙通过分析 WAV 中的静音保留，末尾补齐或裁剪至媒体时长。非零容器起点会额外扫描音视频包时间，避免把绝对结束时间当成归零后的时长；扫描采用逐行读取，限时 120 秒，失败时需先规范化源文件。元数据记录 `container_origin_seconds`、`timeline_policy` 与时长依据。

这与独立音频原有的“解码后首样本归零”策略不同。VTT/SRT 时间应对应本地视频的播放零点；如果字幕来自剪辑前素材或分段直播，还需转换或明确配置偏移。容器时间戳对齐不证明台词与字幕的语义同步，项目复核清单会保留 `video_container_timeline_requires_sync_review`，`compressed_source_sync_verified` 仍为 false。

后续分析、模型规划、句尾候选、融合和诊断使用提取的分析音轨。模型读取字幕，不分析视频画面；诊断视频仍绘制波形和曲线，不拼入原始画面。测试覆盖合成 MKV 多音轨、晚开始音轨、尾部静音、非零容器起点、MP4/AAC，以及无音轨和非法序号；真实剪辑、可变帧率及播放器仍需验收。
