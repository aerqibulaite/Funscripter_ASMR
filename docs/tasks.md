# CLI 任务记录与恢复

所有生成类 CLI 命令现在自动保存任务 JSON；`inspect` 和任务查询不创建任务。现有命令用法保持有效，任务仍在当前终端同步执行，没有后台服务或自动消费队列。

## 使用

准备演示输入后，可指定易记的任务路径：

```powershell
python tools/make_demo.py
python tools/run.py create --config workspace/demo-input/config.yaml --output workspace/task-demo-project --task-file workspace/tasks/create-demo.json
python tools/run.py task-status workspace/tasks/create-demo.json
python tools/run.py task-resume workspace/tasks/create-demo.json
```

输出目录和任务文件都必须是新路径。省略 `--task-file` 时，默认记录位置为 `输出目录的父目录/.funscripter-tasks/<UUID>.json`。运行开始时 stderr 打印 `task: 路径`，正常返回的 JSON 也包含 `task_file` 和 `task_id`。任务文件必须位于输入及输出路径之外，不会写进已有项目的哈希产物。

例如分析阶段可以独立记录和重试：

```powershell
python tools/run.py analyze workspace/task-demo-project --output workspace/task-demo-analysis --task-file workspace/tasks/analyze-demo.json
python tools/run.py task-status workspace/tasks/analyze-demo.json
python tools/run.py task-resume workspace/tasks/analyze-demo.json
```

直接调用 Python 阶段函数的演示/脚本仍保持原行为；只有经过 CLI 的生成命令自动纳入任务管理。

## 状态与记录

| 字段 | 含义 |
| --- | --- |
| `execution_status` | queued、running、completed、failed、cancelled；queued 代表记录已持久化、执行尚未开始 |
| `review_status` | not_available 或 needs_review；completed 只表示执行完成，不表示人工或设备验收通过 |
| `arguments` | 规范化后的绝对路径、命令与非路径参数；恢复不依赖启动时的工作目录 |
| `input_hashes` | 输入文件与源目录内容的哈希；create 还包含配置引用的音频与 LRC |
| `implementation_hashes` | 本包 Python 实现文件的哈希；实现变更后要求新任务 |
| `output_hashes` | 正常收尾时记录的输出目录文件哈希，包括失败/取消时已保留的产物 |
| `attempts` | 每次尝试的开始、结束、状态与错误；重试保留前次记录 |
| `result` / `error` | 原阶段结果或异常说明；原阶段的业务 `status` 继续保留 |

任务 JSON 原子写入。操作系统文件锁阻止同一任务同时执行，也阻止任务管理入口同时处理相同输出目录；锁在进程退出后自动释放，磁盘上的 `.lock` 文件可以保留。直接调用阶段 API 的程序不受此锁协调。

任务查询只读取最后一次持久化状态，不校验全部产物，也不自动判断后台进程是否存活。强制终止进程后，记录可能仍为 running；恢复会取得锁，再核对输入与产物。执行中的细粒度百分比、远程取消和任务调度尚未提供。

## 恢复规则

1. 校验命令、绝对路径、实现、输入和已记录输出哈希；内容变化或缺失则拒绝恢复，不改写原产物。哈希检查按文件流读取，但会增加大项目的 I/O 成本。
2. completed 任务校验通过后直接返回已保存结果，不重复生成。
3. failed/cancelled 或中断任务如果没有输出目录，从该阶段重新执行。已成功的上游阶段保持原样，不自动回溯重跑整条流水线。
4. plan 若保留了与任务快照一致的检查点，使用原 `plan --resume` 机制继续，复用经过 Schema 和业务校验的请求结果。其他阶段暂不支持阶段内部断点续算。
5. 非 plan 阶段若已经发布输出（例如约束失败的融合诊断），保留整个目录并拒绝覆盖。复核后用新输出目录开启新任务；修改配置同样必须新建任务。
6. 强制终止进程后若存在未完成任务快照核对的输出，保守拒绝自动重试，保留它并使用新路径。普通 Ctrl+C 会记录 cancelled；plan 会尽量保存其已合格检查点，事务式阶段会清理尚未发布的临时产物。

锁定、路径和输入预检查失败时，任务可能尚未创建或停在 queued；没有后台工作器自动重试它。对未通过输入检查的任务，应先修正输入，再使用新的任务文件。

任务记录是本地恢复元数据，不是签名审计日志；不要手工改写哈希以绕过校验。外部工具/模型进程、环境变量的实际取值、权重和全部运行时依赖不包含在本次任务指纹中，仍须按项目配置与验收流程记录。任务快照目前不支持符号链接输入。

## 返回码与验证

执行/恢复：成功为 0，失败为 2，取消为 130。`task-status` 只要成功读取记录就返回 0，任务自身失败会显示在 JSON 内。

`tests/test_tasks.py` 覆盖真实 CLI 项目创建/查询、已完成不重跑、失败与取消后的重试、绝对路径、输入/配置/代码变化、产物篡改、同任务锁、未知中断产物保护、运行中输入变化，以及真实规划函数中“第一遍已接受、第二遍取消”后的检查点复用。模型回复仍是测试数据，不代表真实语义质量验收。
