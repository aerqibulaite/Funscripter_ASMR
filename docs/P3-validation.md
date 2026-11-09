# P3：长轨与安装基线

本记录验证合成 PCM 音轨的创建、重开、基础音频分析与缓存复用，以及隔离环境中的 wheel 安装。它不包含真实模型、FFmpeg 压缩源解码、融合/视频长轨性能、外部播放器或设备验收。

## 复现性能测量

在项目根目录运行：

```powershell
python tools/benchmark.py --output workspace/performance-new --durations 60 600 3600
```

输出必须是新目录。可通过 `--rate` 选择 22050、44100 或 48000 Hz；`--python` 选择实际执行阶段的解释器。每个阶段在独立进程执行，保留日志、任务记录、输入/产物和 `report.json`。无需模型、网络或 FFmpeg。

默认素材为 22050 Hz、16-bit、双声道反相 PCM WAV；固定种子生成八种一秒噪声/调制组合，每 29 秒加入静音，字幕每 30 秒一条。逐块写入，不把整轨生成到内存；音频哈希记录在报告。重复模板有利于压缩，不能据此预测真实素材的 NPZ 大小或所有音频内容的分析耗时。

报告的 `wall_seconds` 包含 worker 进程启动、包导入、真实 CLI、任务哈希与退出；`cli_seconds` 从 worker 导入 CLI 前计时。峰值内存来自 Windows `GetProcessMemoryInfo.PeakWorkingSetSize`，Linux/macOS 使用 `getrusage(RUSAGE_SELF).ru_maxrss`，包括 Python/NumPy 和 CLI 的整个 worker 进程，不是系统总内存、GPU 显存或所有子进程之和。本基线明确选择 PCM fallback，没有额外解码子进程。

每个规模仅运行一次；操作系统文件缓存未清空，分析先于缓存复用执行。数字属于本机观察，不作为跨机器速度保证或经过多次重复的稳定统计量。

## 干净环境与离线安装

先在联网构建机准备 wheelhouse。项目仅安装到新虚拟环境，不修改全局 Python 包：

```powershell
python -m pip wheel --no-deps --no-build-isolation --wheel-dir workspace/install-new/wheelhouse .
python -m pip download --only-binary=:all: --dest workspace/install-new/wheelhouse pydantic==2.13.4 PyYAML==6.0.3 numpy==2.4.4 Pillow==12.2.0
python -m venv workspace/install-new/venv
workspace/install-new/venv/Scripts/python.exe -m pip install --no-index --find-links workspace/install-new/wheelhouse funscripter-asmr==0.1.0
workspace/install-new/venv/Scripts/python.exe -m pip check
workspace/install-new/venv/Scripts/python.exe -m pip freeze --all
```

构建使用已安装的 setuptools（要求至少 68）；`--no-build-isolation` 不自动准备构建依赖。离线安装所需 wheel 必须先全部准备好。本次下载遵循本机 pip 软件源配置。不同操作系统、CPU 或 Python ABI 需在对应环境重新准备 wheelhouse；本机下载的 Windows CPython 3.13 x64 wheel 不能作为其他平台的通用安装包。

本次 Windows x64 / CPython 3.13 的八个运行依赖（含传递依赖）已记录到 [版本与哈希锁](P3-runtime-windows-py313.lock)。若要严格重建同一环境，可先按锁准备依赖，再单独安装项目 wheel；已有 wheelhouse 的离线重建示例：

```powershell
workspace/install-new/venv/Scripts/python.exe -m pip install --no-index --find-links workspace/install-new/wheelhouse --require-hashes -r docs/P3-runtime-windows-py313.lock
workspace/install-new/venv/Scripts/python.exe -m pip install --no-index --no-deps --find-links workspace/install-new/wheelhouse funscripter-asmr==0.1.0
```

本次已经用 `pip download --no-index --require-hashes` 对本地八个依赖 wheel 执行哈希校验。项目 wheel 的 SHA-256 单独保存在验收清单中；版本号相同的不同构建不能仅靠文件名视为同一产物。

随后从已安装包运行烟雾验证：

```powershell
python tools/benchmark.py --output workspace/install-new/smoke --durations 12 --python workspace/install-new/venv/Scripts/python.exe --installed
workspace/install-new/venv/Scripts/funscripter.exe --help
```

`--installed` 会清除子进程中的 PYTHONPATH/PYTHONHOME，从用例目录启动，核对解释器确实在虚拟环境、包来源不在仓库 `src`。烟雾验证覆盖 create、inspect、analyze 和缓存 hit；安装后的控制台入口另行验证。源代码演示脚本中有的会自行添加 `src`，因此不能仅靠运行那些脚本证明安装包有效。

也可在新环境中验证现有全量测试，`-I` 避免从环境变量加载仓库源码：

```powershell
workspace/install-new/venv/Scripts/python.exe -I -m unittest discover -s tests -v
```

项目目前分发为 Python wheel，需要兼容 Python 和本地依赖；尚无独立桌面安装器。FFmpeg/FFprobe、字体、模型权重与外部编辑器不打包进 wheel。

## 本机结果

日期：2026-11-09；业务代码提交：`f786709`。Windows 11 build 26200、Intel Core i5-1035G1、8 逻辑处理器、系统可见物理内存约 7.75 GiB；CPython 3.13.13，构建使用 setuptools 83.0.0。

| 合成时长 | 首次分析（秒） | 缓存命中（秒） | 分析峰值（MiB） | 复用峰值（MiB） | 分析目录（MiB） |
| --- | --- | --- | --- | --- | --- |
| 1 分钟 | 2.09 | 1.03 | 56.5 | 54.6 | 0.34 |
| 10 分钟 | 10.31 | 1.05 | 71.7 | 60.0 | 3.43 |
| 60 分钟 | 57.11 | 2.95 | 125.2 | 89.7 | 20.66 |

所有缓存运行均为 `hit`。60 分钟用例创建约 4.21 秒、重开约 1.13 秒。内存随时长上升，不能把分块读取解释成整套分析恒定内存；复用也需要解压特征矩阵。此数据可作为下一轮长轨/批量优化的比较基线。

隔离环境使用 `include-system-site-packages = false`，从本地 wheelhouse 以 `--no-index` 安装成功。12 秒合成烟雾验证通过；包来源为新环境 `Lib/site-packages/funscripter`，控制台入口正常，`pip check` 无损坏依赖。使用该已安装环境执行全量回归：**89 项通过，32.423 秒**。测试模型响应仍为模拟数据，不代表真实模型验收。

完整机器信息、逐阶段时间、峰值内存、产物字节数、输入哈希、环境版本和九个 wheel 的哈希见 [机器可读验收清单](P3-validation-manifest.json)。本次隔离环境的传递依赖版本为 annotated-types 0.8.0、pydantic-core 2.46.4、typing-extensions 4.16.0、typing-inspection 0.4.5；源环境中的后两项版本不同，完整版本分别记录在清单中。

本机原始日志、长轨产物、虚拟环境与 wheelhouse 保存在被 Git 忽略的 `workspace/p3-validation/`；不将生成音频、虚拟环境和依赖二进制纳入源码提交。
