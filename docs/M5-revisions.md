# M5：外部差分保护与局部重生成

日期：2026-11-09

已实现 CLI 数据闭环：准确基线与外部原件比较 → 保护影响范围 → 生成局部候选和差异图 → 按候选 ID 采纳为独立子修订 → 保存父版本与恢复结果。原项目、外部原件和生成基线均不覆盖。当前没有自建编辑页面，不代替 OFS 的逐点编辑。

## 差分报告

先使用 M4 的 archive-revision 保存外部原件及准确基线，再运行：

    python tools/run.py diff-revision PROJECT --revision ARCHIVE --baseline BASELINE --profile config/diff-example.yaml --output NEW_DIFF_DIR

报告比较的是归档中保存的 baseline.funscript 与 external_original.funscript。传入基线的修订 ID、清单哈希和脚本哈希必须与归档一致；曲线相同也不能自行认定是同一次导出。基线来源不明、非法或空脚本、重复时间、非标准 JSON 常量或重复键均保留完整保护。

先识别整体时间偏移、时间缩放、位置仿射变换、相关格式/未知元数据变化、等价曲线重采样及大范围无法解释的修改。这些情况保护整轨，不自动逆变换或按大量局部编辑合并。重采样后曲线相同也不证明没有编辑意图。

局部比较记录 raw_changes 的精确点数据，包括增删、时间和位置变化。affected_ranges 依据两条曲线各自的前后相邻点扩展；原始数据变化与推断影响分开保存。保护范围同时保留边界 guard_times_ms，避免只看段内点而漏掉段外点对插值的影响。

config/diff-example.yaml 默认不确认外部插值，因此保护整轨。只有实际确认外部工具使用线性插值和端点保持，并提供 comparison_reference 后，才把局部保护外的区间标记为 confirmed_unaffected_ranges。设置示例：

    interpolation: linear
    external_interpolation_confirmed: true
    comparison_reference: 填写真实编辑器版本、插值设置与往返验证记录
    time_tolerance_ms: 0

默认零时间容差，不忽略 ±1 ms。非零容差需要 timing_calibration_reference；当前容差仅用于诊断标记，不会把精确变化静默判为未修改。播放设置无法从点集本身推断，真实 OFS 往返和播放器行为仍需验证。

## 局部候选

按 external_original.funscript 的实际点选择端点，修改 config/regeneration-example.yaml：

    python tools/run.py prepare-regeneration PROJECT --diff DIFF_DIR --profile REGEN_PROFILE --output NEW_CANDIDATE_DIR

每个 window 指定整数毫秒端点、preset_id 和 end_transition_ms。两个端点必须已存在于外部原件中，程序不静默吸附或取整；这样可以精确保留边界位置和原点附加字段。窗口不得重叠，不得进入差分保护或锁定区间。接触保护边界可以，但该边界点保持原值。

end_transition_ms 是选区末尾留给返回原结束锚点的时间，必须由配置给出且小于选区长度。周期部分使用已实现的确定性生成器，末尾线性连接到原结束位置。所有动作留在原时间轴内；当前不保存完整周期相位，也不重跑局部 LLM 语义规划。

候选继承外部脚本的顶层格式字段和未选区间的原始点对象。独立检查：

- 未选区间的原点对象完全不变。
- 未选区间的分段线性曲线完全不变。
- 保护区间和锁定区间的曲线完全不变。

曲线比较在双方所有转折点和边界处使用有理数精确计算；在线性和端点保持假设下，这也覆盖两点之间。不是用稀疏采样近似证明。

同时检查整条最终曲线的格式、范围和已配置速度，包括跨选区边界。任何冲突都使整个候选禁止采纳，不会悄悄跳过某个选区。约束失败仍保留候选 JSON、报告和预览，但不导出可直接播放的候选 funscript。

profile.locked_ranges、项目已保存锁定和继承的人工保护都会检查。无法映射的 project manual_overrides 保守地保护整轨。新子修订继续保存原保护和锁定；再归档并与新基线比较时不会因为点集相同就清除旧人工保护。

## 采纳与恢复

先查看 candidate_preview.png、conflicts.json、validation.json 和 preservation_proof.json，取 candidate_manifest.json 中的 candidate_id：

    python tools/run.py accept-candidate PROJECT --candidate CANDIDATE_DIR --candidate-id EXACT_ID --output NEW_ACCEPTED_DIR

该命令验证候选 ID、全部产物哈希、当前项目锁定、原点/格式保持和曲线约束，然后发布新的 track.funscript 与 export_manifest.json。没有修改项目当前指针，也不会覆盖原文件；需要在外部编辑器/播放器中明确选择新的结果。采纳不等于已经通过硬件或人工试听验收。

撤销到父外部原件：

    python tools/run.py restore-revision PROJECT --applied ACCEPTED_DIR --target parent --output NEW_UNDO_DIR

重新导出已采纳结果：

    python tools/run.py restore-revision PROJECT --applied ACCEPTED_DIR --target result --output NEW_REDO_DIR

两者都从原 ACCEPTED_DIR 恢复到独立目录，重启后仍可使用；父原件的 BOM、空白和额外字段按字节保留。如果当前约束不允许恢复文件作为有效脚本导出，仍保存 restored_original.funscript 与失败报告，不丢失原件。

新输出可以作为 M4 archive-revision 的准确新基线继续归档。旧诊断视频始终标记为源生成上下文，不自动变成新曲线的实时说明。新 preview.png 是当前实际点序列的对比图；没有伪造完整语义计划，所以 M4 诊断生成器不会把它当作完整融合计划使用。

## 产物

| 目录 | 关键产物 |
| --- | --- |
| DIFF_DIR | baseline/external 原件快照、diff_report.json、diff_preview.png、diff_manifest.json |
| CANDIDATE_DIR | parent 原件、candidate_curve.json（有候选时）、差异图、冲突、约束与保持证明、候选清单 |
| ACCEPTED_DIR | track.funscript、parent 原件、当前对比图、生成配置、导出清单 |
| UNDO/REDO_DIR | 精确恢复文件、约束报告与独立导出清单 |

所有目录禁止覆盖，逐文件写入暂存目录后整体发布。清单包含项目/源修订、输入哈希、配置和实现哈希，重开与采纳会校验。支持重新选择已有目录中的候选、采纳结果或父版本，不依赖内存中的编辑历史。

## 离线演示

在已有 M3 合成演示上运行：

    python tools/demo_revisions.py --output workspace/m5-demo-new

演示脚本模拟一个点的外部修改、锁定区间、另一选区的数值重生成、采纳、撤销和重做。其 comparison_reference 明确标为脚本化线性样本，不是 OFS 实测校准。

当前实现仅支持已明确确认的线性插值及端点保持。全局变化、重采样、未知元数据或未确认播放设置保持整轨保护；没有提供自动解除保护或推断人工意图的功能。真实 OFS 往返、真实样本精修成本和长轨性能仍待目标环境评估。
