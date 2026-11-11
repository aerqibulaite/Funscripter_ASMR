# 本地 Whisper 与字幕导入

GUI 的“输入与项目”可选择“导入字幕”或“本地 Whisper 转写”。导入字幕继续支持 SRT、LRC、VTT；转写模式只需要音频/视频和 Whisper YAML，不要求提供字幕。两者创建独立项目，后续共用音频分析、Qwen 规划和融合流程。

## 配置与启动

目前接入 faster-whisper，模型需为本地 CTranslate2 目录，包含 `model.bin`、`config.json` 和 `tokenizer.json`。不下载模型，不调用远端识别服务。原版 Whisper `.pt`、Transformers 权重和 LoRA 适配器不能直接当作该目录使用；特调模型需要先准备相应的完整转换产物。

在项目使用的 Python 环境安装可选依赖：

```powershell
python -m pip install -e ".[whisper]"
```

编辑 `config/whisper-local.yaml`：`model_path` 填模型目录，`language` 默认 `ja`（设为 null 可自动识别），默认使用 CPU/int8。GPU 可按实际环境设置 `device: cuda` 和支持的 `compute_type`；CUDA 依赖要求参阅 [faster-whisper 官方说明](https://github.com/SYSTRAN/faster-whisper#gpu)。不会自动安装 CUDA 或选择显存配置。

GUI 中先在“输入与项目”选择音频，再进入“语音模型 · Whisper”页。可直接填写模型目录、设备、计算精度、语言和搜索宽度，也可载入/保存 YAML；高级设置载入后保留。“测试模型加载”实际加载后释放模型，不运行识别，不维持后台服务。点击“转写输入页所选音频”后，结果目录自动填回输入页，再点击“创建新项目”。也可直接选择已有转写目录创建项目，不会重新转写。

字幕路径和编码不参与转写，偏移须为 0；视频沿用输入页的容器时间轴和音轨选择。转写按原语言执行，不自动翻译为中文。“语义模型 · Qwen / LLM”页单独配置文本理解服务，后续在“分步测试”执行模型规划。转写结果与创建项目时的音频或音轨不一致会被拒绝。

CLI 可先生成可复用的转写目录：

```powershell
python tools/run.py transcribe D:/audio/track.mp3 --profile config/whisper-local.yaml --output workspace/transcription-01 --ffmpeg workspace/tools/ffmpeg-binaries/ffmpeg.exe
```

然后用普通 create 配置创建项目，字幕部分填写：

```yaml
subtitles:
  path: D:/OSR/Funscripter_ASMR/workspace/transcription-01/subtitles.srt
  source_kind: whisper
  transcription_manifest: D:/OSR/Funscripter_ASMR/workspace/transcription-01/transcription_manifest.json
```

音频必须与转写时相同，音轨序号也须相同。输入产物被改动时拒绝创建；修订文本应另存并作为人工字幕导入，保留原转写。

## 提示词与来源

- `src/funscripter/prompts/subtitles.txt`：采用桌面文件中的中文版任务提示，针对中文字幕。
- `src/funscripter/prompts/whisper.txt`：独立的原语言转写版，增加识别错误和时间估计说明。
- `config/semantic-knowledge.md`：采用桌面文件中的中文版领域参考，只有模型 YAML 配置该路径时才注入；目前两种输入可共用这一参考。

提示词按保存的项目输入来源自动选择；所选内容参与实现签名、上下文预算和实际请求。编辑提示词后须选择新的规划输出目录。现有项目默认为字幕来源，原先导入的 ASR 字幕不会被程序自动识别成转写来源。

转写目录保存原始文本、分段、可用的词级时间、识别统计、模型文件哈希、运行版本、配置和音频哈希。项目还归档转写 JSON 与来源清单。词级时间只作为估计证据保存，本版不自动用于节拍同步或确认句尾。

转写命令目前不接入任务恢复和批处理；失败时清理未发布目录，成功的结果可反复导入，避免每次规划重新识别。GUI 关闭时沿用当前等待任务完成的行为。

## 验证范围

新增测试使用中性合成音频与模拟识别输出，验证完整导入、来源绑定、原文/词时间保存、异常时间拒绝以及提示词自动选择。没有下载或运行真实特调模型，识别质量与整轨动作效果尚待在目标电脑上评估。
