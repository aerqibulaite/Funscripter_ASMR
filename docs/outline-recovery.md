# 大纲合并失败与恢复

分块大纲通过不代表全局合并通过。曾出现模型在合并多份摘要时只覆盖前半轨，或将证据放入错误章节。旧界面把整个失败清单作为异常弹出，掩盖了具体校验原因。

现在由程序将已验证的相邻章节划入固定连续范围，每批最多8个来源章节。模型只为指定范围生成一个摘要，Schema 将起止字幕限定为该范围的首尾，证据限定在范围内。多轮归并直到满足章节上限，不让模型在压缩时丢弃尾段。范围完整性仍由程序独立校验。

这种分组保证结构完整，不代表章节边界已经获得人工确认。程序归并的章节标为待复核；摘要仍是模型候选解释。新流程也不能保证后续每个语义批次都成功。

## 恢复已通过的分块

```powershell
.\.venv\Scripts\python.exe tools\recover_outline.py PROJECT --planning FAILED_PLAN --output NEW_RECOVERY_DIR
```

只适用于 `hierarchical_merge_failed` 且尚无语义事件的规划。工具验证源清单、字幕与分块输入，重新校验已接受大纲的覆盖和证据，复用这些分块，只调用本地模型完成摘要合并。输出须为新的独立目录，原失败目录保留。

成功后目录包含：

- `outline.json`：恢复后的完整大纲。
- `outline_recovery.json`：来源、覆盖数量、哈希和 `outline_recovered_semantics_pending` 状态。
- `source_partials.json`、`request_log.json`、`requests/`：复用内容与本次合并请求证据。
- `llm-recovered.yaml`：可在 GUI 直接载入的模型配置，设置了 `outline_seed_dir`。

## 接着运行局部语义

重启 GUI，打开原项目，在本地模型页载入恢复目录的 `llm-recovered.yaml`，重新运行模型规划。新任务会验证恢复产物的哈希、项目/修订、字幕和知识文件，直接使用大纲，跳过分块与合并，只运行局部语义。

也可执行：

```powershell
.\.venv\Scripts\python.exe tools\run.py plan PROJECT --profile NEW_RECOVERY_DIR\llm-recovered.yaml --output NEW_PLAN
```

`outline_seed_dir` 是模型 YAML 的可选字段，路径相对 YAML 解析。换项目、修改字幕或知识文件后，应清空该字段或使用匹配的新恢复结果；不匹配时程序拒绝使用，避免采用过期大纲。普通 `config/llm-qwen-local.yaml` 未设置此字段，仍从头建立大纲。

恢复目录没有完整语义事件，不能直接用于融合。需要等待新的局部语义规划完成。

## 错误显示

规划失败时保存具体阶段及最后校验原因。GUI 弹窗显示简短说明、输出目录和日志位置，完整配置、哈希与任务清单继续保存在 JSON 文件中。

## 本次真实任务验证

2026-11-11，原失败目录 `workspace/desktop/20261111-010255-plan-66b3cbb5` 的50份合格分块已复用到 `workspace/desktop/20261111-outline-recovery-v8-fixed`。固定范围版本的13次本地模型合并均一次通过，累计推理约153秒；最终5章连续覆盖全部400条目标字幕，从首条到末条均通过独立覆盖、证据与哈希校验。

原失败目录保留。恢复配置为该恢复目录下的 `llm-recovered.yaml`；恢复目录本身仍不能作为融合输入。后续局部语义已在 `workspace/desktop/20261111-plan-continued-v8-retry` 完成，400条全部评估、最终失败0条，结果仍待复核；更新的整轨重测结论见仓库根目录 `WORK_STATUS.md`。早期 `20261111-outline-recovery-v8` 是失败验证记录，不应作为继续规划的来源。

回归还覆盖了长字幕下的历史事件预算问题：最小目标仍可容纳时，可缩减可选的近期事件列表并在请求中注明省略数量；持续规则和原始目标证据不静默丢弃。
