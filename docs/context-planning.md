# 情境规划与可编辑知识

本版在原有语义 1.4 数据上增加可选 `context_notes`，旧规划仍可读取；新增动作意图 `start` 和 `continue`。新输出需使用本版程序读取。

## 在哪里修改知识

直接编辑 `config/semantic-knowledge.md`。这是简短的 UTF-8 Markdown 运动意图参考，提供常见控制词、歧义与情境关系，不要求固定阶段或显式规则。模型目标是提取可映射到手部往返运动的控制意图；描述、情绪和结果约束用于理解上下文，不直接等于机械参数。

具体字幕编号、逐句案例和文件路径已从注入模型的知识正文移除，只保留在人工参考中。逐段解释与 JSON 参考见 [来源台本的语义动作范例](script-intent-example.md)，它们不自动注入提示词。

`config/llm-qwen-local.yaml` 已启用：

```yaml
semantic_knowledge_file: semantic-knowledge.md
context_cues: 2
```

文件路径相对这份模型 YAML 解析，也支持绝对路径。使用其他模型配置时，在相应 YAML 加上此字段即可；省略或设为 null 则不加载领域文件。GUI 载入模型配置时会解析为绝对路径，随后保存临时模型配置也能继续找到文件。

知识内容进入模型请求的 system 消息，供全局大纲和局部语义分析参考；它和 Schema、字幕、输出预留一起计入上下文预算。每次规划固定读取一份快照，保存在新规划目录的 `semantic_knowledge.json`，请求中也保留实际内容。文件内容变更会改变运行签名，旧请求不会被误当成相同知识的结果恢复；应另选输出目录重跑。

## 如何追加词条

推荐使用这种形式：

```text
表达：接着、再来、继续
可能含义：延续正在做的事，或恢复此前暂停的事。
适用上下文：检查前文处于活动还是暂停状态。
例外：回忆、引用、未来安排不立即执行。
示例：“休息结束，继续刚才的动作。” → 恢复此前活动。
证据要求：引述当前要求和必要的前文；无法判断则标明推断。
```

可以自由增加你熟悉的表达和情境关系。优先说明歧义、作用对象、阶段和连续性，不要写成“出现一个词就固定用某速度或幅度”。通用词典只给可能解释，实际文本具有更高优先级。没有规则说明时，模型可输出 `scene_inference`，并用 `basis=inferred` 区分推断与原文明示。

## 输出如何参与后续处理

`semantic_events.json` 中的 `context_notes` 支持：

- `rule_definition`：登记规则，不立即执行。
- `rule_trigger`：某规则已经触发；需要改变动作时另有对应动作事件。
- `phase_transition`：从说明进入活动、结束或其他阶段变化。
- `state_continuation`：持续关系、时长变化等不必改变频率的信息。
- `scene_inference`：没有明示规则时，根据前后文提出的候选情境解释。

每条笔记含摘要、明示/推断依据、来源字幕 ID 和原文引用，程序校验引用确实存在。引用正确只证明可追溯，不自动证明解释正确。所有笔记和动作都是模型候选，仍需复核。

没有新增控制指令时，`no_new_information` 仍可附带不含 control 的 scene_inference/state_continuation 备忘，用于保存持续背景。规则、阶段变化和可执行控制仍要求 processed/ambiguous。结构化响应连续失败时，规划器会缩小目标批次；网络或服务错误不会触发无意义的拆分重试。

后续分块会收到所有此前规则定义、最新阶段笔记、最近六条其他笔记，以及这些笔记对应的原始字幕证据。最近八个动作事件也会保留为参考。这不是无限上下文，也不是真实运动状态；预算不足时明确失败，不静默丢弃规则。旧的或被修改的规则仍保留为候选历史，由模型结合后续原文解释，不自动建立不可撤销的规则。

模型检查界面的“分析”文本会显示笔记；融合 `segment_plan.json` 也保留笔记供追查。普通笔记不驱动运动；附带结构化 control 的笔记由通用控制接口解析，见 [进度、定次重复与节奏锚点](generic-controls.md)。

## 动作配置与语义分开

`start` 开始活动，`continue` 在活动时延续、暂停时恢复；`maintain` 仅维持现状，不解除暂停。暂停保留此前活动样式。相对加速已到相同绝对目标档位时保留状态，真正反向的目标仍报告冲突。

同一句、同一生效时刻的一个主要动作与互补 maintain 事件可合并，例如“恢复，同时保持速度”；两个独立主动作、相反参数或不同字幕的同时事件仍报告冲突，不编造句内先后时间。

如需让明确的开始/恢复意图在原文没有给全参数时生成草稿，融合时选择 `config/fusion-context.yaml`。它初始暂停，只在 `start/resume/continue` 时为未知字段使用 `action_defaults`；模型明示参数和已继承参数优先。这些默认值是编辑偏好，不是模型从台词发现的事实，使用过的字段会写进事件处置和复核理由。

默认值可在 `config/fusion-context.yaml` 中修改：

```yaml
action_defaults:
  pace_tier: medium
  amplitude_tier: small
  motion_style: rhythmic
  paused: false
```

其中 paused 不用于覆盖当前暂停状态，是否启动由动作意图决定。该示例的 unknown_policy 为 maintain_state：未解析或冲突事件保留已有状态并报告复核原因；明确暂停仍执行。若希望遇到不确定就保持位置，可改为 hold_position。

执行器仍只实现 rhythmic 预设；smooth/irregular 会如实保留并报告未映射，不自动伪装成已支持的样式。现在支持有限重复与人工确认的节奏锚点，尚未实现自动音频节拍识别。

## 如何重新测试

若旧任务在全局合并时失败，先参考 [大纲恢复](outline-recovery.md) 复用已通过分块，并载入生成的 `llm-recovered.yaml`，避免重复建立大纲。

1. 重启 GUI，重新载入 `config/llm-qwen-local.yaml`。
2. 对原项目重新运行“模型规划”，使用新输出目录。旧补跑只修失败字幕，不能替代本次完整语义重算。
3. 将融合配置切换为 `config/fusion-context.yaml`，生成新的融合草稿。
4. 检查规则讲解是否被提前执行、无规则情境是否得到有据解释、暂停后继续是否恢复，以及默认参数来源是否合理。

也可用 CLI：

```powershell
.\.venv\Scripts\python.exe tools\run.py plan workspace\desktop\20261110-212253-create-c98b15fe --profile config\llm-qwen-local.yaml --output workspace\desktop\context-plan-01
.\.venv\Scripts\python.exe tools\run.py fuse workspace\desktop\20261110-212253-create-c98b15fe --planning workspace\desktop\context-plan-01 --profile config\fusion-context.yaml --output workspace\desktop\context-fused-01
```

自动化测试验证数据传递、证据校验和状态转换；不能替代目标素材的语义质量评估。

## 2026-11-11 验证记录

全量 190 项自动化测试通过。本地 Qwen 使用新知识文件对两组中性短样本实测（每组 4 条，每批 2 条），原始请求、响应、语义事件及融合草稿保存在 `workspace/model-test/context-v5-smoke-20261111`。

- 无显式规则组：识别 start、increase_rate、pause、resume；相对加快未被擅自指定绝对档位。
- 显式规则组：规则讲解只形成笔记，不提前执行；识别后续开始、暂停、恢复；持续时间要求未被判为加速。
- 两组实际模型输出均经过融合验证：暂停保持、恢复后有预设运动，无事件冲突；规则说明期间保持静止。

短样本表明这条语义到动作的链路可运行。原先 400 条字幕尚未按本版重新生成，不能据此宣称其人物意图已经全部正确还原。
