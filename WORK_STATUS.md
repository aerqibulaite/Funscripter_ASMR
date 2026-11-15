# 双机协作进度

最后更新：2026-11-13（Asia/Taipei）。接手工作前先读本文件，再检查本机 Git 状态。

## 当前结论

用户于2026-11-13明确要求停止模型分析：已停止Gemma的8081服务（原PID21292），确认进程退出。本次不自动重启或重跑分析。原任务 `20261112-235501-plan-8ebb0632` 保留，服务中断使任务记录为failed / tokenizer_connection_or_invalid_response；这是主动停止引起。停止说明另存 `workspace/review/20261113-user-stopped-gemma-plan.json`。

代码执行链路已能完成整轨分析和导出；语义到动作的还原尚未验收通过。下一步优先修正融合配置入口、准备阶段与正式执行的区分、动作作用对象误判，再进行较小模型的同素材对照。

2026-11-13新增：已完善原语言/日文转写提示词及中日知识参考，取消强制每句输出动作；程序自动补齐复核元数据，孤立processed标为ambiguous，不补造动作。证据反馈细化到cue，支持保留连续合格前缀；审阅元数据显示补齐记录。详情见 `docs/semantic-language-adaptation.md`。原始证据、来源和覆盖校验保留。新提示词尚未整轨实测，正在运行的旧任务不受这些文件修改影响；重启后从已有转写项目新建规划。

本轮最终验证：231项完整测试通过（32.878秒），另含7项原语言适配、原始响应保留、复核补齐审计和连续前缀恢复测试。30份旧失败响应离线复验中23份经机械补齐后通过，另4份可保留连续合格前缀（累计13条，包含尝试之间重复）；来源错误继续拒绝。该数据不代表新模型准确率或新整轨耗时。当前工作区修改尚未提交推送。

## 代码同步状态

- 本次记录所在分支：`master`。
- 本次交接的父提交：`e81cf88`，`feat: add local transcription and interactive script previews`；交接提交号以 `git log -1` 为准。
- 上述交接已提交并推送为 `c1c66db`。之后新增的9B配置、启动脚本及本文件更新尚未提交推送，不包含在该提交内。
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
| Gemma 独立配置与启动脚本 | 已加载真实模型，计数接口及短JSON推理通过；尚未完成整轨测试 | `config/llm-gemma-local.yaml`、`launch_gemma_server.cmd` |
| Qwen3.5-9B 独立配置与启动脚本 | 已按本机实际文件名准备；尚未实际推理测试 | `config/llm-qwen35-9b-local.yaml`、`launch_qwen35_9b_server.cmd` |
| 首次音频转写的目录提示 | 已区分“已有结果”与“输出位置”，创建项目前拒绝空/不完整目录，并提示先执行转写；尚未提交 | `src/funscripter/gui.py`、`gui_backend.py`、`docs/local-whisper.md` |
| PyAV文件解码兼容 | 已将Whisper依赖限制为 `av>=11,<19`，本机安装18.1.0；实际WAV解码加GPU推理通过，尚未提交 | `pyproject.toml`、`tests/test_transcription.py`、`docs/local-whisper.md` |
| 转写分段进度与词级时间偏差 | 已显示阶段、音频进度、分段数、耗时和最近文本；音轨内的词/分段边界偏差保存原值并标记复核，尚未提交 | `src/funscripter/transcription.py`、`gui_backend.py`、`gui.py` |

最近验证：2026-11-12 提交前运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests`，218项全部通过；`git diff --check` 通过。另有真实 Whisper 模型加载、短静音GPU推理及权重SHA256验证通过。测试通过不等于整轨语义还原验收通过。

后续目录提示修改的定向验证：`test_transcription.py` 6项通过，包含空目录/不完整目录的提前拒绝，以及已有成功转写的复用与项目导入。用户正在尝试 Whisper转写加Gemma流程，截图中的失败发生在转写结果缺少字幕文件的创建项目阶段，尚无该组合的真实效果结论。

之后用户实际执行转写，发现 faster-whisper 1.2.1 与 PyAV 19 的 `metadata_errors` 参数不兼容。此前NumPy数组GPU测试绕过了文件解码，未发现此问题。本机已安装经PyPI官方SHA256验证的PyAV18.1.0，并新增真实WAV文件解码回归；更新后的 `test_transcription.py` 7项通过，`pip check`通过。实际本地模型以WAV文件为输入、cuda/bfloat16推理通过，记录为 `workspace/review/20261112-whisper-file-runtime-check.json`。该样本为1秒静音，只验证文件解码和GPU执行，不评价转写准确率。用户需完全关闭并重开工作台，旧进程仍可能持有PyAV19模块。

后续真实转写在第60个分段因 `ASR word timestamp outside segment` 中止。修正为：对音轨范围内的词时间偏差记录问题，不修改原始时间，不终止整轨；非有限、倒置或超出音轨的时间仍拒绝。GUI增加分段进度回调，进度事件不将后台任务误标为完成。定向转写测试11项通过，含实际文件解码、时间偏差保留、非法时间拒绝、阶段回调和GUI忙碌状态；此前失败的整轨尚未用此次修正重跑完成。

最新整体回归：上述修改后运行完整测试，224项全部通过（33.305秒），`git diff --check`通过。当前修改仍在本机工作区，尚未提交推送。

随后任务 `workspace/desktop/20261112-235037-plan-110a233b` 在模型请求前失败：`tokenizer_connection_or_invalid_response`，`request_log.json`为空。配置为Gemma端口8081；沙箱外核对时没有llama-server进程，8080/8081/8082均拒绝连接。已于本次排查中后台启动Gemma服务（检查时PID21292），日志在 `workspace/servers/gemma/20261112-235351.*.log`。实际模型名称匹配，`/apply-template`和`/tokenize`计数通过，短JSON推理通过；不代表失败的整轨已经重跑或完成。此服务只在本机进程存活期间可用，另一台电脑需自行启动。

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
- Qwen3.5-9B 无审查版已下载到 `D:/AI/llama.cpp/models/Huihui-Qwen3.5-9B-abliterated.Q5_K_M_2.gguf`，大小6,522,004,864字节。已读取GGUF元信息确认 qwen35 / 9B / abliterated / Q5_K_M；末尾 `_2` 仅是本机文件名的一部分。独立配置端口8082、上下文32768、thinking=false。尚未运行本模型推理或评估语义质量；另一台需要自行放置模型并核对路径。

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
