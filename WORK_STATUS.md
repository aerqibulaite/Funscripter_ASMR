# 双机协作进度

最后更新：2026-11-12（Asia/Taipei）。接手工作前先读本文件，再检查本机 Git 状态。

## 当前结论

代码执行链路已能完成整轨分析和导出；语义到动作的还原尚未验收通过。下一步优先修正融合配置入口、准备阶段与正式执行的区分、动作作用对象误判，再进行较小模型的同素材对照。

## 代码同步状态

- 本次记录所在分支：`master`。
- 本次交接的父提交：`e81cf88`，`feat: add local transcription and interactive script previews`；交接提交号以 `git log -1` 为准。
- 提交前已执行 `git fetch origin`，未发现新增远端提交。
- 本次交接将下面的代码修复、模型配置、启动脚本和本文件一起提交；另一台拉取后应检查 `git log -1` 与 `git status` 确认版本及本机改动。推送是否完成以实际 Git 输出为准。
- 最近一次拉取前保留过 stash：`codex-before-pull-e81cf88-20261111`。它是历史备份，不包含此后所有更改；不要直接应用到另一台电脑。

## 本次交接包含的已完成工作

| 工作 | 当前状态 | 入口 |
| --- | --- | --- |
| 上下文规则、阶段、进度与持续关系 | 已实现；语义质量仍需对照 | `src/funscripter/planner.py`、`semantic_contracts.py` |
| 大纲固定范围合并、校验反馈、失败批次拆分 | 已实现，真实旧任务恢复完成 | `src/funscripter/planning_repair.py`、`tools/recover_outline.py`、`docs/outline-recovery.md` |
| GUI 简化失败原因展示 | 已实现 | `src/funscripter/gui.py`、`gui_backend.py` |
| 本地 Whisper 转写与独立提示词 | 远端基准已包含；本机另补 Windows DLL 查找 | `src/funscripter/transcription.py`、`docs/local-whisper.md` |
| Gemma 独立配置与启动脚本 | 已准备并校验参数，尚未实际推理测试 | `config/llm-gemma-local.yaml`、`launch_gemma_server.cmd` |

最近验证：2026-11-12 提交前运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests`，218项全部通过；`git diff --check` 通过。另有真实 Whisper 模型加载、短静音GPU推理及权重SHA256验证通过。测试通过不等于整轨语义还原验收通过。

## 最新真实素材核查

这是同一音轨、同一400条字幕的重测，仍使用 Qwen 27B 和导入字幕；不是 Gemma、9B 或 Whisper 的效果测试。

- 模型规划：`workspace/desktop/20261111-215252-plan-52c5f359`。
- 项目：`workspace/desktop/20261111-215226-create-ace077c1`。
- 音频分析：`workspace/desktop/20261111-215235-analyze-d18eea69`。
- 原融合：`workspace/desktop/20261111-234005-fuse-1b810bd0`。
- 核查报告：`workspace/review/20261111-52c5f359-result-review.md`。

结果：400条全覆盖，最终失败0条，297个事件（251个 maintain），模型请求累计约105.3分钟。结构通过不代表语义理解正确。

原融合使用 `config/fusion-example.yaml`，没有动作默认参数，所有事件幅度又为 unknown，导致全程静止，脚本仅两个位置均为50的点。当前 GUI 默认路径仍指向此演示配置，尚未修正。

已保留原结果，另用相同模型输出与音频分析、`config/fusion-context.yaml` 生成独立对照：

- 融合：`workspace/review/20261111-52c5f359-context-fuse`。
- 交互预览：`workspace/review/20261111-52c5f359-context-preview/index.html`。
- 运动占比87.32%，3,674个脚本点；6条明确的暂停/结束指令执行，最后32:06.650结束后保持至音轨末尾。
- 仅使用 medium_small / slow_small / fast_small，幅度均来自配置默认值。

**未解决的语义问题：**

1. cue_000005 的准备要求被标成 start，导致00:13.900启动周期运动；正式开始宣告在cue_000077，04:22.950。cue_000025也把“进入准备”当成 start。
2. 规则说明中的cue_000065仍被标为即时 instruction/maintain。
3. cue_000621把结果过程中的“缓缓地”误映射为运动减速，作用对象仍混淆。
4. 152个描述/问句事件被融合排除，其中许多只是冗余维持状态，不能全部当成漏动作直接放行。
5. 倒计时只有进度记录，`progress_curve=[]`；3次回升未标 reset。唯一的 rhythm_follow 是规则定义，没有实际节奏锚点。

核查未试听整条音轨或运行设备，输出均为待复核草稿。

## 本机环境（另一台需独立配置）

- GPU：RTX 5060 Ti，16GB 显存。
- Python 环境：项目 `.venv`；它不受 Git 跟踪，另一台需自行安装依赖。
- llama.cpp：`D:/AI/llama.cpp`；GGUF 模型存放在其 `models` 目录。
- Qwen 27B：`config/llm-qwen-local.yaml`，端口8080，上下文32768，thinking=false。
- Gemma 4 12B：`gemma-4-12b-it-UD-Q5_K_XL.gguf`；独立配置使用端口8081、上下文32768、thinking=false。16GB本机切换前需释放旧模型占用。
- Whisper：`D:/AI/whisper-ct2`，`config/whisper-local.yaml` 使用 cuda/bfloat16。权重官方SHA256核对通过；真实模型加载和1秒静音GPU推理通过，尚未评估真实音频识别准确率。
- 已在 `.venv` 安装 faster-whisper、CTranslate2 及 NVIDIA CUDA 12/cuDNN 运行库。项目代码会查找当前Python环境中NVIDIA包的DLL目录；不依赖修改系统PATH。
- Qwen3.5-9B 无审查版已查到下载地址；本文件不宣称已经下载、配置或测试。检查另一台及本机实际文件后再更新状态。

## 下一步与分工

当前没有登记跨机任务占用。开始并行工作时填写下面三项，并通知另一台；进度文件是交接记录，不是实时锁。

| 电脑/分支 | 正在处理 | 涉及文件 |
| --- | --- | --- |
| 待登记 | 待登记 | 待登记 |

建议顺序：

1. 将 GUI 默认融合入口与配套配置对齐，避免再次选用演示配置。
2. 用准备/正式执行、规则说明/触发、动作/结果过程等样本修正语义分类并验证。
3. 用同一组代表性台词对比 Gemma 12B 与 Qwen候选的耗时、关键指令命中和误执行；尚未证明较小模型在此项目中达到可接受质量。

## 两台电脑的同步方式

1. 接手前读本文件并检查 `git status`。有本机改动时先妥善保存，拉取时不要覆盖未完成工作。
2. 两台同时改代码时使用各自分支，登记任务和涉及文件，尽量避免同时修改同一模块。
3. 每完成一个可交接的阶段，将本文件中的结论、已完成项、剩余问题和实际验证结果一起更新；代码与进度一起提交并推送。另一台拉取后才能看到。
4. `.gitignore` 已排除 `workspace/`、`.venv/`、构建产物。音视频、GGUF、Whisper权重、测试输出和本机安装依赖不会随代码同步，需要另行复制或下载。
5. 转移测试产物时保留完整目录和清单/哈希，不要只复制最终脚本；另一台的模型目录、可执行文件路径和服务地址应按实际环境调整。
6. 交接时必须区分“代码已写”“检查通过”“真实素材验证”“已提交推送”，不要把其中任一步当作全部完成。
