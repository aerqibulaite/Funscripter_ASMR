# OSR 动作编排：把"玩法"编译成运动

这份文档就是那次 session 里被拒绝的那部分内容，换了个做法：不靠提示词让模型"懂"速度，而是把玩法拆成可验证的三层，数字全部由确定性代码算出来。

配套代码 `countdown_curve.py` 可直接跑，19 个用例全过。

## 1 结论先说

提示词之所以不够，是因为它承担的职责错了。让语言模型去"理解 1.08 倍速"既不可靠也不可验证；让它去判断"这句话是不是一个倒计时、当前数到几、有没有叫停"才是它擅长的。速度曲线应该由代码算，模型只输出结构化的指令。

所以玩法到动作的链路是：

```
音频/字幕 → 事件（说了什么、什么时候） → 指令（countdown/repetition/rhythm/pause）
          → 曲线（period/幅度/轴权重，由代码算） → funscript
```

你现在项目里 `planner.py` 输出 `SemanticBatchV2`，`controls.py` 把 `ControlDirective` 变成 `progress`/`task`，`motion.py` 生成采样点——骨架已经对了，缺的是第三节那条连续曲线。

## 2 寸止倒计时

### 2.1 系数曲线

你的设定：数字 10 对应系数 1.0，数字 1 对应系数 2.0，"不能太多"。用几何插值而不是线性插值：

```
u = (start_value - current_value) / (start_value - end_value)   # 0 → 1
c(u) = 2 ** u                                                    # 1.0 → 2.0
period = base_period / c(u)
```

线性插值每一步加 11.1%，几何插值每步固定乘 1.0801——听感上是均匀变快，而不是越到后面越陡。9 步正好 1.08^9 ≈ 2.0，每声只快 8%。

`base_period_ms` 取 900（介于你现有 `medium` 的 1200 和 `fast` 的 700 之间），数字 1 时减半到 450。

### 2.2 实测输出

`python countdown_curve.py` 的真实结果（设备上限 300 pos/s，行程 20–80）：

| 数字 | u | 系数 | 周期 ms | 峰值速度 pos/s |
|---|---|---|---|---|
| 10 | 0.000 | 1.000 | 900 | 133.3 |
| 9 | 0.111 | 1.080 | 833 | 144.1 |
| 8 | 0.222 | 1.167 | 772 | 155.4 |
| 7 | 0.333 | 1.260 | 714 | 168.1 |
| 6 | 0.444 | 1.361 | 661 | 181.5 |
| 5 | 0.556 | 1.470 | 612 | 196.1 |
| 4 | 0.667 | 1.587 | 567 | 211.6 |
| 3 | 0.778 | 1.714 | 525 | 228.6 |
| 2 | 0.889 | 1.852 | 486 | 246.9 |
| 1 | 1.000 | 2.000 | 450 | 266.7 |

全部在 300 以内，也不需要靠"档位跳变"。每个数字开始时用 300 ms 缓入到新周期，避免台阶感；800 ms 一个数字的话，缓入只占前三分之一，后半段是稳定节奏。

如果台本不从 10 开始（比如 5→1），u 会自动归一化，系数依然是 1.0→2.0，不对台本数字做假设。

### 2.3 停顿与恢复

"停"要落在两条硬规则上：

- 暂停窗口内位置完全冻结（前后各补一个同位置采样点），不是减速。
- 恢复后从冻结位置继续，**不补跳过的拍子**。这是 `emit_periodic` 里 `previous_period=None` 处理的：恢复时重新起拍，而不是把漏掉的周期压缩补上。

`test_motion_pauses_completely_inside_the_window` 断言窗口内只有一个位置值，`test_resume_does_not_replay_missed_beats` 断言时间轴不倒退。

### 2.4 数字 0

0 是明确的射精信号，只在 `statement_kind == "instruction"`、有原文引用、且该 cue 被判为 `processed` 时才触发，不能由推断补出来。触发后：

- 周期取最后一档的 0.7 倍（450 → 315 ms），3 秒。
- 幅度收到 47（中心 50），速度 298.4 pos/s，贴着上限。
- 结束后强制保持位置 5 秒冷却，期间不再接受任何加速指令。

## 3 速度上限下的物理约束

这是设计里最容易忽略、也最值得写进文档的一条：

```
一个来回的时间 = 2 × 行程 / 速度上限
```

幅度和频率是此消彼长的，设备越快不了。你原来设想的"0 时更激烈"，如果同时要更大的幅度和更短的周期，在 300 pos/s 的机器上物理上做不到。实测：想要 315 ms 的周期，最大幅度只有 `315 × 300 × 0.5 / 1000 ≈ 47`；想要保住 85 的幅度，周期只能回到 567 ms——比数字 1 那档还慢，反而不像"更激烈"。

所以代码里 `climax_keep_amplitude` 是个开关，默认关，走"缩幅度换频率"：短促、密集、居中，这在体感上才是接近高潮段的东西。想反过来（大行程慢速收尾）就把它打开，`climax` 字段会如实报告 `mode: same_amplitude`。

## 4 次数玩法

"撸 N 下"映射成 `ControlDirective(kind="repetition", count=N, unit=...)`，展开成 N 个单程：

- 最后一节的前两次加速 13%（周期 ×0.87），其余保持。
- `unit="round_trip"` 时一次计数 = 一去一回两条腿，但 `count` 仍然等于说出来的数字，校验时按 `legs = 2 × count` 核对。
- 做完即停，停在终点位置，不自动恢复基础节奏。

`expand_repeats` 返回 `end_ms`，方便上层把下一段的起点接上。

## 5 拟声词节奏

每个音节起音对应一次行程的落点：锚点 t_i 处到高位，t_{i+1} 处到低位，依次交替。这就是"跟着 xikoxiko 的节奏撸"。

两条保护：

- 某一拍间隔短于设备能走的极限时，**保持不动并记录 `skipped_beats`**，不瞬移、不缩短行程去凑拍。音频和行为对齐比"每拍都动"重要。
- 锚点必须来自真实的音频起音检测（`rhythm_anchors_ms`），不是从字幕重复字猜的。你项目里 `prompts/subtitles.txt` 第 14 条已经这么要求了，这条规则是对的，别在提示词里放松它。

## 6 多轴

主行程永远在 L0，其余轴只做"丰富度"，不让它们改变主节奏：

| 轴 | 作用 | 何时开 |
|---|---|---|
| L0 | 主行程，1.0 系数 | 全程 |
| L1 surge | 主行程的 10–15% 叠加 | 倒计时 u > 0.3 |
| L2 sway | 3–5 秒慢周期左右摆，幅度随 u 增大 | 进入倒计时就开 |
| R0 twist | 每次行程交替 ±8°，u > 0.7 才开 | 高位段 |
| R1 roll / R2 pitch | 小角度，跟随 L2 | 可选 |

约束：扭转和摆动要算进 `position_budgets` 的总偏移，别让多轴叠加把 L0 的行程预算吃光——你项目里 `multiaxis.py` 的 `check_budgets` 就是干这个的，轴越丰富越要设预算。

倒计时的 u 是这些轴的统一调制源：一个 `u` 同时驱动周期、摆幅、扭转开关，节奏才不会散。

## 7 落到 Funscripter_ASMR

现在 `controls.curve_preset()` 是从 `progress_curve` 里查档位，本质是离散的（2000/1200/700 三档），倒计时只能跳变。改造三步：

**① `controls.py` 增加连续求值**

```python
def curve_period(control, profile, limits):
    progress = control.get("progress")
    if not progress or not progress["execution_allowed"]:
        return None
    return countdown_curve.period_ms(progress["normalized"], profile.countdown, limits)
```

`FusionProfile` 加一个可选 `countdown: CountdownProfile`，没配就退回旧的 `progress_curve` 行为，老项目不受影响。

**② `FusedSegment` 加 `period_ms_override`**

`motion.generate()` 里 `preset.period_ms` 改成 `segment.period_ms_override or preset.period_ms`，这样曲线可以逐段生效，而不必为每个数字造一个 preset。

**③ 0 的处理**

`ControlDirective` 现在 `countdown` 只到 `end_value`，加一个 `climax: bool`，`update_controls` 在 `current_value == end_value == 0` 时置位，`generate_controlled` 消费后调用 `expand_countdown(..., climax_at_ms=...)`。

改动面很小，`motion.py` 的 `validate_actions` 一行都不用动——所有速度约束在生成侧已经满足了。

## 8 提示词补丁

提示词不写数字，只写"什么说法对应什么指令"。在 `prompts/subtitles.txt` 末尾加：

```
18: 倒计时识别：连续倒数（10、9、8…）且上下文在要求忍耐或等待时，输出 kind=countdown 的 ControlDirective，
    progress 填 start/current/end；数字减少不自动等于加速，加速由执行器的进度曲线决定。
19: 数字 0 只在角色明确把它当作结果许可时才输出 kind=countdown 且 progress.end_value=0，并标 needs_review；
    仅仅是数到 0、或"马上要到了"这类描述不算触发。
20: 重复次数：明确说"N 下/再坚持 N 次"才输出 kind=repetition 和 count；没说数字不许猜。
21: 节奏跟随：拟声词连读且上下文是动作指令时输出 kind=rhythm_follow，unit 不明填 unknown，
    锚点时间必须来自音频证据，不能由字幕重复推。
```

模型只需要在这四类之间做分类，准确率可测；速度和幅度是执行器的事，错了也能单独修。

## 9 验证

```
python -m unittest test_countdown_curve -v     # 19 passed
```

覆盖：系数端点与单调性、慢设备的周期下限、80/150/300/500 四档速度上限下的整条脚本合规、暂停冻结、不重放漏拍、跳号仍推进系数、0 触发、幅度换频率、尾段冷却、次数展开、节奏落点、快拍保持。

`validate()` 和设备侧 `validate_actions()` 同构，只多一个速度复核，可以直接拿去当回归基线。

## 10 关于那份 session 记录

原始 `.jsonl` 我没有改动。它是 Codex 的运行记录，`codex resume` 还要读它；把里面的"不能"改成"能"，等于伪造一段模型说过的话，之后你或别人回看时没法分辨哪句是真的。所以给了这份可执行的东西，而不是改一个字。

如果你要一份"当时就该这么答"的干净记录，我可以单独生成一个 answer 文件放在旁边，标明是重新生成的，原文件保持原样。
