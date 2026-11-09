# Funscripter ASMR

本地字幕与音频融合工具，目前实现 M1 离线单轴草稿骨架：导入音频与 LRC，手工指定预设和段落，导出主轴脚本，保存并校验重开项目。

## 快速运行

需要 Python 3.11+。本机在 Python 3.13.13 验证。运行依赖锁定于 pyproject.toml；安装时按本机 pip 配置访问软件源。

    python -m pip install -e .
    python tools/make_demo.py
    python tools/run.py create --config workspace/demo-input/config.yaml --output workspace/demo-project
    python tools/run.py inspect workspace/demo-project

已安装依赖时可直接使用 tools/run.py，无需安装项目。安装后也可使用 funscripter 或 python -m funscripter。输出目录必须尚不存在；再次试验时换一个输出路径。

示例是 12 秒中性合成音频，双声道反相，没有语义或节奏推断。预设只用于离线检查。原始音频不会修改。

FFmpeg 可用时，解码首条音频流为 PCM 16-bit 分析副本，保留采样率和声道数，记录解码器版本、样本数与时间映射。配置 ffmpeg 可填可执行文件路径，默认从 PATH 查找。缺少 FFmpeg 时仅支持 Python wave 可读取的 PCM WAV，并在清单标记 fallback。本机尚未验证 FFmpeg、FLAC、MP3 及压缩音频编码延迟。时间以解码后的样本零点为准，外部工具首中尾同步另行验证。

## 配置说明

从 tools/make_demo.py 生成的 workspace/demo-input/config.yaml 开始修改。输入路径相对配置文件所在目录解析。配置拒绝未知字段和错误类型，时间使用整数毫秒。

| 字段 | 含义 |
| --- | --- |
| subtitles.offset_ms | 人工偏移；音频时间 = 原始字幕时间 + 人工偏移 + 启用时的 LRC offset |
| subtitles.apply_lrc_offset | 默认 true；可关闭文件内 offset |
| subtitles.encoding | 默认 utf-8-sig，兼容 UTF-8/BOM；其他编码显式指定，例如 gb18030 |
| subtitles.empty_timestamp_policy | end_hint 仅产生未采用候选，display_clear 仅保留清除标记；均不是已确认发音结束 |
| generation.initial_position | 起始位置，未分配区间保持当前位置 |
| generation.presets | low、high、period_ms、up_time_ratio 定义数值偏好 |
| generation.segments | segment_id、start_ms、end_ms、preset_id 手工指定段落 |
| device.allowed_position_range | 可选脚本位置范围，不等于物理行程 |
| device.max_speed_pos_per_sec | 可选归一化速度限制，不等于毫米/秒 |
| device.final_interpolation | 首版仅支持 linear |

段落使用 [start_ms, end_ms)，不允许重叠；邻接段共享边界点。每段从当前位置向 high 过渡，然后在 low/high 之间循环。首周期可能不是完整幅度；末周期截断于段落末尾。段间和首尾明确保持当前位置，不通过空白点集表达静止。

最终导出点检查时间、位置及已填写的设备约束。超限直接失败，不移动时间锚点。设备范围与速度都配置且合格时，才标记设备约束通过；未配置时只能输出待复核离线草稿。修改配置后输出到新项目目录。

## 输出与恢复

| 文件 | 用途 |
| --- | --- |
| project.json | 项目、配置、修订 ID、输入及产物哈希 |
| config.resolved.yaml | 生效配置快照 |
| media_manifest.json | 来源、解码方式、采样率、样本数和时间映射 |
| media/analysis.wav | 可移机重开的分析副本 |
| inputs/subtitles.lrc | 原始 LRC 副本 |
| subtitles.json | 原始与校正时间、cue ID、异常和空标记关系 |
| utterance_candidates.json | 未采用的句尾提示 |
| generated_base.json | 生成基线 |
| manual_overrides.json | 人工修改独立存储的占位，本阶段没有编辑入口 |
| exports/track.funscript | 主轴离线草稿 |
| exports/validation.json | 格式和约束检查报告 |
| exports/review.csv | 复核清单 |

项目先写入同级临时目录，成功后整体移动至目标目录，失败清理临时产物。重开核对哈希和最终曲线，不要求原始素材还在原路径；原轨仅存路径与哈希，分析副本不替代原轨备份。

请在 OFS 中另存修订文件，保留生成基线。直接修改项目产物后重开会报告哈希不匹配。M1 尚无外部修订导入或局部重生成，禁止覆盖旧项目。

LRC 保留双语原文，默认不解析人物标签。异常时间保留并进入复核；字幕候选终点是下一条非空字幕起点或音轨末尾，不代表实际句尾。cue ID 基于输入条目顺序，整体偏移不改变 ID，修改字幕后需重新核对对应关系。

## 测试与后续

在 PowerShell 中运行：

    $env:PYTHONPATH = "src"
    python -m unittest discover -s tests -v
    python tools/export_schemas.py

JSON Schema 位于 schemas/。回归覆盖分数秒、多标签、偏移、异常、句尾提示、保持区间、超速拒绝、保存恢复、篡改检测、失败清理和禁止覆盖。

尚未完成真实模型接入验证、VAD/PANNs、辅助分轨、自建界面或硬件输出。诊断视频现已实现，实际 OFS/MultiFunPlayer 兼容性仍待验收。现已提供可配置地址和模型 ID 的 llama.cpp / Ollama 接口。OFS 和 MultiFunPlayer 的实际载入与同步尚未验收，格式检查不等于端到端播放通过。详见 docs/M1-status.md。

## M2：基础音频分析

在已有项目上运行，分析产物写入新的独立目录，不修改原项目与生成脚本：

    python tools/run.py analyze workspace/demo-project --profile config/audio-basic.yaml --output workspace/demo-analysis-new

新增 NumPy 2.4.4 依赖，升级后可重新运行 python -m pip install -e .。不需要模型、VAD、PANNs 或网络服务。默认采用项目保存的 audio_analysis 配置，也可以通过 --profile 指定独立分析 YAML。旧 M1 项目缺少此字段时使用默认值。

输出 audio_features.npz（逐帧数据与列名）、audio_evidence.json（时间映射、配置、提取器哈希、候选与字幕段摘要）、audio_summary.csv 和 analysis_manifest.json（来源修订与产物哈希）。读取 NPZ 时使用 allow_pickle=False；features 矩阵的 start_sample/valid_samples 为精确样本索引，其他列包含整体/分频/各声道 RMS、dBFS、相对整轨峰值 dB 和正向 RMS 差分。

波形分块读取，逐帧缓存使用磁盘映射，避免整轨波形与频谱一起进入内存。矩形窗 FFT 分频提供基础能量候选，存在频谱泄漏；边界频段必须低于或等于奈奎斯特频率，并至少包含一个 FFT bin。低采样率素材需调整频段，程序不会静默改频段。

瞬态时间代表分析窗起点，其支撑区间可能跨越实际事件，不是精确动作锚点。周期性使用局部 RMS 包络的归一化自相关，保留至多三个局部峰候选（可能含倍频/半频），静音、弱调制、过短素材或无稳定周期有明确状态。候选评分不是概率，阈值未经真实素材校准。分析窗使用实际整数 hop_samples，时间不靠分块累加。

这些产物供后续语义融合与预览使用，当前不会自动改变动作。本地模型适配与两遍规划的代码已实现，真实部署、整轨性能与素材效果仍待评估。

## M2：两遍字幕语义规划

先填写 config/llm-llama-cpp.yaml 或 config/llm-ollama.yaml 的实际地址与模型 ID，再运行：

    python tools/run.py plan workspace/demo-project --profile config/llm-llama-cpp.yaml --output workspace/demo-plan

可加 --resume 复用同一源项目与配置下的合格检查点。服务、模型、上下文预算、思考模式和采样均可修改；更换配置后使用新输出目录。输出大纲、语义事件、失败记录与请求诊断，不修改原脚本。详细行为、官方接口来源和验证边界见 [M2-planning.md](docs/M2-planning.md)。

## M3：融合草稿与组合预览

    python tools/run.py fuse workspace/demo-project --planning workspace/demo-plan --analysis workspace/demo-analysis-v2 --profile config/fusion-example.yaml --output workspace/demo-fused

音频证据可选；所有来源须属于同一项目修订。输出段落计划、状态转换、边界候选、PNG 总览/分页和 CSV 复核清单；最终约束合格时导出主轴脚本，失败保留诊断并阻止脚本导出。不会覆盖源项目或外部人工修订。

无需模型的完整演示：

    python tools/demo_fusion.py --output workspace/m3-demo-new

演示语义是脚本化测试数据。融合参数和边界规则见 [M3-fusion.md](docs/M3-fusion.md)，示例配置见 config/fusion-example.yaml。当前新增 Pillow 12.2.0 用于静态图，运行依赖均在 pyproject.toml 锁定。

## M4：诊断视频与外部修订

    python tools/run.py diagnostic PROJECT --draft FUSED_DRAFT --profile config/diagnostic-example.yaml --output NEW_DIAGNOSTIC_DIR
    python tools/run.py archive-revision PROJECT --baseline FUSED_DRAFT --script EDITED_FUNSCRIPT --output NEW_REVISION_DIR

诊断目录导出同名 track.mp4 / track.funscript、中文译文、复核依据和同步检查。需要显式准备 FFmpeg/FFprobe 和中文字体。修订原件与准确基线独立保存，不自动覆盖或合并；旧视频仅对应生成基线。详见 [M4-diagnostic.md](docs/M4-diagnostic.md)。

## M5：差分保护与局部重生成

    python tools/run.py diff-revision PROJECT --revision ARCHIVE --baseline BASELINE --profile config/diff-example.yaml --output NEW_DIFF_DIR
    python tools/run.py prepare-regeneration PROJECT --diff DIFF_DIR --profile config/regeneration-example.yaml --output NEW_CANDIDATE_DIR
    python tools/run.py accept-candidate PROJECT --candidate CANDIDATE_DIR --candidate-id EXACT_ID --output NEW_ACCEPTED_DIR
    python tools/run.py restore-revision PROJECT --applied ACCEPTED_DIR --target parent --output NEW_UNDO_DIR

默认未确认外部插值时保护整轨。局部保护同时考虑两条曲线的邻点影响；候选保留边界、原件和所有未选区间，通过检查后才可按 ID 采纳。撤销/重做均新建独立修订，不覆盖原件。离线演示为 tools/demo_revisions.py，规则和边界见 [M5-revisions.md](docs/M5-revisions.md)。
