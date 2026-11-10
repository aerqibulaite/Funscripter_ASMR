# 本机 Qwen 测试

本机模型：`D:\AI\llama.cpp\models\Huihui-Qwen3.8-27B-abliterated-UD-DW-IQ3_S.gguf`。

1. 双击根目录 `launch_qwen_server.cmd`，等待模型加载及监听提示。若已有该服务在 8080 端口运行，无需重复启动。
2. 双击 `launch_gui.cmd`。启动器优先使用项目 `.venv`。
3. 在“本地模型”载入 `config/llm-qwen-local.yaml`，测试连接。地址为 `http://127.0.0.1:8080`。
4. 选择音视频和字幕，创建项目，再运行“分析音频”和“模型规划”。规划发送字幕；视频画面不参与分析。
5. 选择适合用途的融合配置，运行融合草稿；查看复核表和预览。诊断视频还需 FFmpeg、FFprobe 和字体配置。

启动脚本和 YAML 均使用 32768 上下文，单请求槽，关闭思考模式；修改上下文时应同步修改两处。模型服务使用本机回环地址。服务启动窗口按 Ctrl+C 停止。

命令行规划示例（先创建项目，输出目录必须不存在）：

```powershell
.\.venv\Scripts\python.exe tools\run.py plan workspace\你的项目 --profile config\llm-qwen-local.yaml --output workspace\qwen-plan-01
```

`.venv` 基于本机 ComfyUI 所附 Python 创建，仅向项目环境安装依赖，不修改 ComfyUI 的依赖。该 Python 安装目录仍需保留。

连接和合成字幕规划成功只证明接口及业务流程可运行；真实字幕语义效果需用目标素材人工复核。

2026-11-10 本机实测：模型加载和短 JSON 推理通过。原提示词的首次演示规划被句尾事件复核校验拒绝；明确补充现有复核规则后，`workspace/model-test/demo-plan-v2` 完成全局大纲与局部语义规划，5 条字幕、2 个事件、0 项失败，整体仍为 `needs_review`。该轮使用 ComfyUI Python 的已有依赖运行，独立环境验证另行执行。

随后项目 `.venv` 已安装 pyproject 锁定的依赖；在独立环境中，规划、语义 v2、GUI 后端共 28 项回归通过，Tkinter 导入和实际模型短 JSON 推理通过。

## 32K 与规划预算更新

本机配置启用 `token_count_method: server`：通过 llama.cpp 的 `/apply-template` 渲染实际聊天模板，再用 `/tokenize` 计数，预留输出 4096 token 和安全余量 1024 token。不再把 UTF-8 字节数当作实际 token 数。相同请求的计数在本次进程内复用；计数接口不可用时明确失败，不静默改成不可靠的估算。其他配置仍默认保守字节预算，Ollama 暂不支持此服务端计数方式。

`outline_max_phases: 8` 和 `outline_summary_chars: 160` 控制全局/分块大纲的摘要大小；每章保留最多 3 条代表性证据。每轮合并还要求章节数不超过输入章节数的一半。JSON Schema 和应用层均检查这些上限，并继续检查字幕范围完整有序覆盖。它会降低全局大纲的细节密度；原始字幕、请求和响应不截断，第二遍仍读取目标字幕和邻近字幕原文。若素材有更多重要章节，可提高章节上限并重新验证质量。

每次请求的 `request.json` 及 `request_log.json` 新增 `budget`，记录计数方法、输入用量、输出预留、安全预留和上下文上限。修改配置、提示词或实现后须使用新输出目录；旧任务缓存签名不再匹配，不可直接恢复旧失败目录。

2026-11-10 更新验证：32K 服务加载、实际短 JSON 推理通过；全量 173 项自动化测试通过。本次真实素材验证输出为 `workspace/desktop/qwen32k-validation-20261110`，源项目及旧失败记录保持独立。

长输入验证：`workspace/model-test/long-context-32k.json` 的实际输入为 18029 token，与计数接口结果一致，返回有效 JSON。旧任务的 50 份合格分块大纲经新合并逻辑生成 8 个章节，覆盖全部 400 条字幕；输入 12632 token，合并约 67 秒，记录位于 `workspace/model-test/merge32k-recovery`。这是合并专项验证，不是可直接交给融合的完整规划目录。

真实素材整轨重跑在某分块遇到模型漏覆盖字幕，故 `qwen32k-validation-20261110` 最终仍是失败记录，不代表整轨通过。为此新增校验反馈重试及实际重试请求快照 `request-N.json`，指出预期字幕 ID；同一问题分块随后两次调用恢复成功，记录位于 `workspace/model-test/coverage32k-recovery`。反馈也计入上下文预算。新增反馈专项测试通过；完整 400 条字幕的第二遍语义结果尚待重新运行及人工复核。

更新代码后需重启 GUI，再载入本机模型 YAML，使新计数方法和压缩配置生效。当前32K服务无需重复启动。

## 仅补跑失败字幕

`tools/repair_planning.py PROJECT --planning FAILED_PLAN --output NEW_DIR` 在独立目录修复语义 1.4 的失败批次。它先检查源项目、规划产物哈希和完整覆盖，再按原请求保留上下文，将失败批次拆成最多 4 条字幕补跑，并明确 processed 与事件的对应规则。已通过的事件和字幕评估保持原值；源目录不修改。

新目录包含 `repair_report.json`、源清单/日志快照及实际补跑请求。合并后重新检查全部事件身份、字幕评估和失败列表，更新产物哈希，可作为融合的规划输入。后续已通过批次沿用原来的请求上下文，不传播新事件重新推理，因此仍需人工复核跨批次连续性；这不是整轨重新推理。若仍有失败，新结果继续标记 failed，不能冒充完整成功。
