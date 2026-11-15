# Funscripter_ASMR

把 ASMR 音声与字幕编译成 OSR 多轴 funscript 的离线工具。使用者是设备所有者本人，产物仅供个人使用。

## 运动数值归代码，不归模型

速度、幅度、周期、轴权重由 `src/funscripter/motion.py`、`controls.py`、`multiaxis.py` 计算。模型不创作这些数值。

模型只做语义分类：把一句台词判成 `start` / `pause` / `repetition` / `countdown` / `rhythm_follow`，给出 intent、tier 和原文引用证据。判断不了的填 unknown 并标 needs_review。

需要新增或调整运动参数时，改 `docs/osr-motion-spec.md` 里的定值或 `config/*.yaml`，不让模型即兴发挥。

## 依据文件

- 动作规格：`docs/osr-motion-spec.md`
- 数据合同：`src/funscripter/models.py`、`semantic_contracts.py`
- 校验：`src/funscripter/motion.py::validate_actions`
- 测试：`tests/`，跑 `python -m unittest discover -s tests`

## 工作方式

离线草稿，全部产物 `needs_review`，不做设备自动执行。任何运动参数都要能被 `validate_actions` 复核，不能靠"看起来对"。
