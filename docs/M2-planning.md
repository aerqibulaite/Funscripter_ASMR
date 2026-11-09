# M2：模型服务与两遍规划

日期：2026-11-09

已实现 llama.cpp 与 Ollama 原生结构化 HTTP 适配，服务 URL、模型 ID、版本备注、量化备注、超时、重试、上下文预算、输出长度、采样和思考模式均可配置。没有模型下载或云服务后备路径。使用 Python 标准库，不要求安装模型服务 SDK。

接口依据：

- [llama.cpp server 官方说明](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)：/v1/chat/completions、response_format schema、chat_template_kwargs。
- [Ollama chat 官方说明](https://docs.ollama.com/api/chat)与[结构化输出说明](https://docs.ollama.com/capabilities/structured-outputs)：/api/chat、stream=false、format Schema、options 与 think。

具体部署版本和模型模板的支持情况仍需实际接入测试。模板不支持时记录失败，不静默取消 Schema 或更换服务。

## 运行

复制并修改 config/llm-llama-cpp.yaml 或 config/llm-ollama.yaml 中的 base_url 和 model_id。另一台电脑的服务填其实际可访问地址，不要求服务运行在本机。base_url 可为根地址；llama.cpp 可带 /v1，Ollama 可带 /api。模型名不绑定 Qwen，可切换任何通过本项目测试的服务模型。

    python tools/run.py plan workspace/demo-project --profile config/llm-llama-cpp.yaml --output workspace/demo-plan

恢复同一次运行：

    python tools/run.py plan workspace/demo-project --profile config/llm-llama-cpp.yaml --output workspace/demo-plan --resume

也可以在创建项目的配置中填写 llm 对象，之后不传 --profile。没有服务配置时直接报错，不访问默认地址。需要鉴权时设置 api_key_env 为环境变量名称；密钥内容不会进入配置快照和请求文件。请求绕过系统代理并拒绝 HTTP 重定向，始终使用指定端点。

## 规划与校验

第一遍优先全文输入；超出保守预算时先分块生成有 ID 范围的章节摘要，再分层合并。不能容纳单条完整字幕或摘要无法缩小时显式失败，不截断正文。

预算以序列化消息和 Schema 的 UTF-8 字节数估计 token 上界，再预留输出与安全空间；不是精确 tokenizer 计数。应按服务实际加载窗口设置 context_budget_tokens，并留足模板开销。此方法偏保守，会比真实 tokenizer 更早分块，尚未接入服务端 tokenizer。

第二遍按章节边界、条数上限和预算分块，携带全局大纲、可选前后文及最近两条已接受语义事件。预算不足时缩小目标块，再减少可选邻接上下文。失败后清空衔接上下文，避免跨过缺失块继续沿用旧状态。这里保存的是语义上下文；动作档位状态机属于 M3。

大纲必须有序、完整且不重叠地覆盖目标 cue，证据 ID 必须位于对应章节。局部事件必须覆盖每个目标 cue 且无重复，不得返回属于邻接上下文的结果。明确意图/档位/样式必须引用当前 cue 原文中的连续文本；unknown、uncertain 和 after_utterance 必须带复核原因。字段严格校验，额外设备坐标字段和未知枚举会失败。

程序为章节与事件建立稳定 ID。语义事件不会直接生成设备坐标。证据原文存在只是可追溯性条件，不能证明语义判断正确；所有模型结果仍带未获人工确认的候选状态。

## 输出与恢复

- outline.json：全局章节大纲，仅全局规划合格后写入。
- semantic_events.json：合格事件及每条失败 cue 的明确原因。
- planning_run.json：源项目/修订、字幕哈希、配置、提示词/实现版本、状态和产物哈希。
- request_log.json：各次请求的接受、失败、耗时和缓存复用情况。
- requests/：请求正文、原始返回内容与业务校验通过的检查点。字幕会保存在这些本地诊断文件中。

所有模型调用串行、有限重试。空响应、拒答、截断、非法 JSON、ID 缺失或证据不合格均不能当作成功。每次合格响应落盘；取消后保留缓存，--resume 重新校验并复用。字幕、源修订、配置或规划代码改变时拒绝续跑旧目录，改用新目录。下游请求包含全局大纲与前块事件，因此依赖变化会得到新缓存键。

输出目录独立于项目，不修改原有脚本或人工成果。运行成功仍是 needs_review；有失败项返回退出码 2；Ctrl+C 标记 cancelled 并返回 130。磁盘/进程故障可能留下 running 状态，可使用同源配置恢复。当前不支持多个进程同时写一个规划目录。

## 验证状态

已用本机 HTTP 模拟服务器验证两种协议，用中性确定性模拟响应验证全文/分层两遍规划、上下文所有权、缓存续跑、失败隔离、取消恢复、超预算保护、模型切换拒绝旧缓存和非法响应校验。

本机没有真实模型，因此尚未证明 Qwen 或其他模型的语义准确性、部署兼容性及推理性能。模型版本与量化信息是用户配置备注，并非已自动从后端确认。后续需用否定、引用、未来描述、弱上下文等真实样本评估。

下一步 M3：融合语义与音频证据、定性档位状态机、可解释段落计划和组合预览。外部播放器兼容性仍需实际环境验证。
