# 批量串行处理

通过 YAML 将现有生成命令组成有依赖的批次，每项保留独立任务记录。当前使用一个进程串行执行，没有后台服务或并行 worker。

## 快速运行

先准备演示输入，再使用 [示例配置](../config/batch-example.yaml)：

```powershell
python tools/make_demo.py
python tools/run.py batch --config config/batch-example.yaml --output workspace/batch-demo
python tools/run.py batch-status workspace/batch-demo
python tools/run.py batch-resume workspace/batch-demo
```

示例依次创建项目、提取音频特征、验证缓存命中；无需真实音频和模型。配置中的输出路径写在 `workspace/batch-demo` 下，因此该示例的 `--output` 应使用同一路径。重跑新批次时需同时修改 YAML 中的任务输出路径和命令中的批次目录；已有批次使用 `batch-resume`。

批次目录必须是新路径。元数据保存在 `批次目录/.batch/state.json`，各子任务保存在 `.batch/tasks/<id>.json`；业务产物可以放在批次目录内的其他子目录，也可以放在外部独立目录。`.batch` 不能成为任何任务的输入或输出，也不能放在任务源目录内部。

## 配置规则

```yaml
schema_version: "1"
failure_policy: continue
jobs:
  - id: first
    command: create
    arguments:
      config: input-a/config.yaml
      output: results/project-a
  - id: analyze_first
    command: analyze
    depends_on: [first]
    arguments:
      project: results/project-a
      output: results/analysis-a
  - id: independent
    command: create
    arguments:
      config: input-b/config.yaml
      output: results/project-b
```

- 所有路径相对 YAML 所在目录解析，保存时转换为绝对路径；不相对启动命令时的工作目录解析。
- `id` 唯一，使用字母、数字、下划线或短横线，长度 1–64；首字符必须是字母或数字。
- `command` 使用现有生成类命令名称，`arguments` 使用字段名：如 `reuse_analysis`、`candidate_id`、`elapsed_seconds`。可选字段默认值与单任务入口一致。
- 依赖必须指向列表中更早的任务；使用前项输出时，必须声明该生产任务为直接或间接依赖。引用后项、循环、重复 ID、未知字段或输出目录重叠会在执行前被拒绝。
- 可以串联 create、analyze、plan、fuse 等阶段；批处理不会自动填入模型部署信息、猜测候选 ID 或生成隐藏参数。
- 各任务的输出目录须独立且不嵌套，不能写入其他任务的源目录；目录不覆盖规则继续生效。

## 失败、取消与恢复

| 情况 | 行为 |
| --- | --- |
| `failure_policy: stop`（默认） | 第一个失败项之后停止；失败依赖链标为 blocked，尚未开始的独立项保持 queued |
| `failure_policy: continue` | 继续其他独立任务；依赖失败项的任务标为 blocked，不执行 |
| Ctrl+C / 子任务 cancelled | 停止整个批次；已完成结果与可用检查点保留，后续任务不再启动 |
| completed | 只表示执行成功，业务草稿仍需人工复核；依赖按执行结果判断，不等待人工复核状态 |

`batch-status` 显示最后持久化状态、各项结果/错误、子任务路径及 queued/running/completed/failed/cancelled/blocked 计数。它不校验全部产物或自动判断进程是否仍存活。

`batch-resume` 先核对原 YAML 的内容和解析结果、实现版本及批次锁，再按原顺序处理。每个已完成任务也会重新核对输入与输出哈希，验证通过后直接复用结果，不重复执行；配置或产物变化不能通过“之前成功”绕过检查。批次重新核对期间，尚未核对的项显示 queued。

失败/取消项继续使用单任务的恢复规则：规划可复用合格请求检查点，其他未发布阶段可重跑；已发布的失败诊断保留且拒绝覆盖。输入预检查失败且未创建子任务记录的项目，可以补齐缺失文件后重试；已有快照的输入内容变更仍要求新任务。原 YAML（包括失败策略）或代码改变时需要新批次和新的输出路径。

进程被强制结束后，批次可能仍显示 running；恢复取得操作系统锁后会重新核对子任务。若子任务已完成而批次进度尚未写回，可以认领该已验证结果。已知子任务记录丢失、参数被替换或输出无法核对时，保留现有成果并报告失败。

成功返回 0，含失败/阻塞项返回 2，取消返回 130；`batch-status` 成功读取记录即返回 0。每次执行/恢复保留批次尝试历史，实际业务尝试保留在独立子任务内。

## 验证与范围

自动化覆盖依赖链和缓存命中、停止/继续、取消恢复、已完成不重跑、篡改与丢失记录、配置/代码变化、锁、元数据路径及失效依赖。当前使用短合成音频验证批处理；P3 的单阶段长轨测量不代表多轨批次的吞吐和峰值内存已经验收。

P3 归档 wheel 对应 `f786709`。批量入口是后续代码，部署到其他电脑前需从当前源码重新构建 wheel；保留旧 wheel 以便按其哈希复现历史基线。

2026-11-09 本次验证：新增 13 项批量测试，全量 102 项通过（43.162 秒）；源码示例完成三个阶段。新 wheel 在独立虚拟环境离线安装后，也完成三个阶段并确认缓存命中，随后恢复批次仍为三个 completed，子任务尝试次数各为 1。`pip check` 通过，实际导入路径位于该环境的 `Lib/site-packages`。

新构建保存在 `workspace/batch-validation/wheelhouse/funscripter_asmr-0.1.0-py3-none-any.whl`，SHA-256 为 `761cfde4dd32da1fd8fe8786dbd7cbf861fec63181830635e5ebdd2f9d1a92ac`；安装和运行记录在 `workspace/batch-validation/`。该构建与 P3 的历史 wheel 版本号相同但内容不同，以各自哈希区分。
