"""Tk desktop for the existing immutable, review-oriented CLI workflow."""
import argparse
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .gui_backend import DesktopController, absolute, load_model, probe_model, save_model
from .models import LLMConfig
from .review_panel import ReviewPanel


class DesktopApp:
    def __init__(self, root, workspace):
        self.root, self.controller = root, DesktopController(workspace)
        self.events, self.busy = queue.Queue(), False
        self.paths, self.inputs, self.model = {}, {}, {}
        self.buttons = []
        self.advanced = {}
        root.title("Funscripter ASMR · 本地测试工作台")
        root.geometry("1160x890")
        root.minsize(920, 740)
        style = ttk.Style(root)
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(10, 5))
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="本地测试工作台", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(outer, text="导入 → 音频分析 → 模型规划 → 融合 → 诊断预览　｜　每次运行保存独立结果，生成结果待复核").pack(anchor="w", pady=(5, 12))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        source, model, workflow = (ttk.Frame(tabs, padding=14) for _ in range(3))
        tabs.add(source, text="  1  输入与项目  ")
        tabs.add(model, text="  2  本地模型  ")
        tabs.add(workflow, text="  3  分步测试  ")
        for frame in (source, model, workflow):
            frame.columnconfigure(1, weight=1)
        self.build_source(source)
        self.build_model(model)
        self.build_workflow(workflow)
        review = ttk.Frame(tabs, padding=10)
        tabs.add(review, text="  4  模型输出与纠正  ")
        self.review = ReviewPanel(review, self.controller.workspace, lambda: self.busy)
        self.tabs, self.review_tab = tabs, review
        self.status = tk.StringVar(value="就绪 · 无需本地模型即可导入和分析音频")
        ttk.Label(outer, textvariable=self.status).pack(anchor="w", pady=(12, 3))
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x")
        self.log = ScrolledText(outer, height=9, wrap="word", font=("Consolas", 10), state="disabled")
        self.log.pack(fill="both", pady=(8, 0))
        self.write("工作目录：" + str(self.controller.workspace))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(150, self.poll)

    def button(self, parent, text, action):
        button = ttk.Button(parent, text=text, command=action)
        self.buttons.append(button)
        return button

    def field(self, parent, row, label, mapping, key, default="", browse=None, choices=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        variable = tk.StringVar(value=default)
        mapping[key] = variable
        widget = ttk.Combobox(parent, textvariable=variable, values=choices, state="readonly") if choices else ttk.Entry(parent, textvariable=variable)
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        if browse:
            self.button(parent, "选择…", lambda: self.choose(variable, browse)).grid(row=row, column=2, padx=(8, 0))
        return variable

    def choose(self, variable, kind):
        if kind == "directory":
            value = filedialog.askdirectory(parent=self.root)
        else:
            types = {"audio": [("音频与视频", "*.wav *.mp3 *.flac *.ogg *.m4a *.mp4 *.mkv *.webm *.mov *.avi *.m4v *.ts *.mts *.m2ts *.mpeg *.mpg *.ogv *.flv"), ("所有文件", "*.*")],
                     "subtitles": [("字幕", "*.srt *.lrc *.vtt"), ("所有文件", "*.*")],
                     "yaml": [("YAML", "*.yaml *.yml"), ("所有文件", "*.*")]}
            value = filedialog.askopenfilename(parent=self.root, filetypes=types.get(kind, [("所有文件", "*.*")]))
        if value:
            variable.set(value)

    def build_source(self, frame):
        self.field(frame, 0, "音频 / 视频文件", self.inputs, "audio", browse="audio")
        self.field(frame, 1, "字幕（SRT / LRC / VTT）", self.inputs, "subtitles", browse="subtitles")
        executable = Path("workspace/tools/ffmpeg-binaries/ffmpeg.exe").resolve()
        self.field(frame, 2, "FFmpeg", self.inputs, "ffmpeg", str(executable) if executable.is_file() else "ffmpeg", browse="file")
        self.field(frame, 3, "字幕编码", self.inputs, "encoding", "utf-8-sig")
        self.field(frame, 4, "字幕偏移（毫秒）", self.inputs, "offset", "0")
        probe = executable.with_name("ffprobe.exe")
        self.field(frame, 5, "FFprobe（视频需使用）", self.inputs, "ffprobe", str(probe) if probe.is_file() else "ffprobe", browse="file")
        self.field(frame, 6, "音轨序号（从 0 开始）", self.inputs, "audio_stream", "0")
        self.button(frame, "创建新项目", self.create).grid(row=7, column=1, sticky="w", pady=8)
        ttk.Separator(frame).grid(row=8, column=0, columnspan=3, sticky="ew", pady=5)
        self.field(frame, 9, "已有项目目录", self.paths, "project", browse="directory")
        self.button(frame, "打开并校验项目", self.inspect).grid(row=10, column=1, sticky="w", pady=5)
        ttk.Label(frame, text="视频按容器起点对齐音轨，字幕需使用同一时间轴。仅提取音轨，画面不参与模型分析。", wraplength=800).grid(row=11, column=0, columnspan=3, sticky="w", pady=5)

    def build_model(self, frame):
        self.field(frame, 0, "服务类型", self.model, "backend", "ollama", choices=("ollama", "llama_cpp"))
        self.field(frame, 1, "服务地址", self.model, "base_url", "http://127.0.0.1:11434")
        self.field(frame, 2, "模型 ID", self.model, "model_id")
        self.field(frame, 3, "上下文窗口（tokens）", self.model, "context_budget_tokens", "32768")
        self.field(frame, 4, "最大输出（tokens）", self.model, "max_output_tokens", "4096")
        self.field(frame, 5, "每批字幕数", self.model, "max_chunk_cues", "32")
        self.field(frame, 6, "请求超时（秒）", self.model, "timeout_seconds", "120")
        self.field(frame, 7, "思考模式", self.model, "thinking", "backend_default", choices=("backend_default", "false", "true"))
        self.field(frame, 8, "API Key 环境变量名（可空）", self.model, "api_key_env")
        actions = ttk.Frame(frame)
        actions.grid(row=9, column=0, columnspan=3, sticky="w", pady=12)
        self.button(actions, "载入模型 YAML", self.load_profile).pack(side="left", padx=(0, 8))
        self.button(actions, "保存模型 YAML", self.save_profile).pack(side="left", padx=(0, 8))
        self.button(actions, "测试连接与 JSON 推理", self.probe).pack(side="left")
        ttk.Label(frame, text="Ollama 常用 http://127.0.0.1:11434；llama.cpp 常用 http://127.0.0.1:8080。\n先自行启动服务并加载模型，再填入实际模型 ID。本界面不下载模型。\nAPI 密钥只从环境变量读取。YAML 中其他高级参数载入后会保留。", wraplength=820).grid(row=10, column=0, columnspan=3, sticky="w", pady=5)

    def build_workflow(self, frame):
        examples = Path(__file__).resolve().parents[2] / "config"
        self.field(frame, 0, "音频分析结果", self.paths, "analysis", browse="directory")
        self.field(frame, 1, "模型规划结果", self.paths, "planning", browse="directory")
        self.field(frame, 2, "句尾候选（可空）", self.paths, "boundaries", browse="directory")
        self.field(frame, 3, "融合配置 YAML", self.paths, "fusion_profile", str(examples / "fusion-example.yaml") if examples.exists() else "", browse="yaml")
        self.field(frame, 4, "融合草稿目录", self.paths, "draft", browse="directory")
        self.field(frame, 5, "诊断配置 YAML", self.paths, "diagnostic_profile", str(examples / "diagnostic-example.yaml") if examples.exists() else "", browse="yaml")
        actions = ttk.Frame(frame)
        actions.grid(row=6, column=0, columnspan=3, sticky="w", pady=12)
        for label, command in (("① 分析音频", "analyze"), ("② 模型规划", "plan"),
                               ("③ 句尾候选（可选）", "utterance-candidates"), ("④ 融合草稿", "fuse"), ("⑤ 诊断视频", "diagnostic")):
            self.button(actions, label, lambda stage=command: self.stage(stage)).pack(side="left", padx=(0, 7))
        ttk.Label(frame, text="先在“输入与项目”创建或打开项目。每一步成功后自动填入结果目录；也可手动选择此前结果。\n模型规划使用“本地模型”页当前设置；融合和诊断使用各自 YAML。示例预设仅用于离线测试。\n结果、复核清单及任务记录保存在工作目录中；日志会显示具体路径。", wraplength=820).grid(row=7, column=0, columnspan=3, sticky="w", pady=10)
        self.button(frame, "打开工作目录", self.open_workspace).grid(row=8, column=1, sticky="w", pady=5)

    def guard(self, operation):
        try:
            operation()
        except Exception as exc:
            self.write(f"错误：{exc}")
            messagebox.showerror("操作失败", str(exc), parent=self.root)

    def model_values(self):
        values = dict(self.advanced)
        values.update({key: variable.get().strip() for key, variable in self.model.items()})
        for key in ("context_budget_tokens", "max_output_tokens", "max_chunk_cues"):
            values[key] = int(values[key])
        values["timeout_seconds"] = float(values["timeout_seconds"])
        values["thinking"] = {"true": True, "false": False}.get(values["thinking"], values["thinking"])
        values["api_key_env"] = values["api_key_env"] or None
        return LLMConfig.model_validate_json(json.dumps(values)).model_dump(mode="json")

    def load_profile(self):
        def load():
            path = filedialog.askopenfilename(parent=self.root, filetypes=[("YAML", "*.yaml *.yml")])
            if not path:
                return
            values = load_model(path).model_dump(mode="json")
            self.advanced = values
            for key, variable in self.model.items():
                value = values[key]
                variable.set("" if value is None else str(value).lower() if isinstance(value, bool) else str(value))
            self.write("已载入模型配置：" + path)
        self.guard(load)

    def save_profile(self):
        def save():
            values = self.model_values()
            path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".yaml", filetypes=[("YAML", "*.yaml")])
            if path:
                save_model(path, values)
                self.write("已保存模型配置：" + path)
        self.guard(save)

    def probe(self):
        def start():
            values = self.model_values()
            self.start("模型连接测试", lambda: probe_model(values))
        self.guard(start)

    def create(self):
        def start():
            values = {key: variable.get().strip() for key, variable in self.inputs.items()}
            self.start("创建项目", lambda: self.controller.create(**values), "project")
        self.guard(start)

    def inspect(self):
        def start():
            project = self.path("project")
            self.start("校验项目", lambda: {**self.controller.inspect(project), "output": project}, "project")
        self.guard(start)

    def path(self, key, optional=False):
        value = self.paths[key].get().strip()
        if not value:
            if optional:
                return None
            raise ValueError(f"请先填写或选择：{key}")
        return absolute(value)

    def stage(self, command):
        def start():
            args = {"project": self.path("project")}
            target = {"analyze": "analysis", "plan": "planning", "utterance-candidates": "boundaries", "fuse": "draft"}.get(command)
            if command == "plan":
                profile = self.controller.fresh("model").with_suffix(".yaml")
                save_model(profile, self.model_values())
                args["profile"] = str(profile)
            elif command == "utterance-candidates":
                args["analysis"] = self.path("analysis")
            elif command == "fuse":
                args.update(planning=self.path("planning"), analysis=self.path("analysis", True),
                            boundaries=self.path("boundaries", True), profile=self.path("fusion_profile"))
            elif command == "diagnostic":
                args.update(draft=self.path("draft"), profile=self.path("diagnostic_profile"))
            callback = (lambda path: self.events.put(("planning_started", None, {"output": path}, None))) if command == "plan" else None
            self.start(command, lambda: self.controller.stage(command, on_output=callback, **args), target)
        self.guard(start)

    def start(self, label, work, target=None):
        if self.busy:
            raise ValueError("已有任务运行，请等待完成")
        self.busy = True
        for button in self.buttons:
            button.state(["disabled"])
        self.progress.start(12)
        self.status.set("运行中：" + label)
        self.write("开始：" + label)

        def worker():
            try:
                result = work()
                if result.get("execution_status") in ("failed", "cancelled"):
                    raise ValueError(json.dumps(result, ensure_ascii=False))
                self.events.put((label, target, result, None))
            except Exception as exc:
                self.events.put((label, target, None, f"{type(exc).__name__}: {exc}"))
        threading.Thread(target=worker, daemon=False).start()

    def poll(self):
        try:
            while True:
                label, target, result, error = self.events.get_nowait()
                if label == "planning_started":
                    self.review.follow(result["output"])
                    self.tabs.select(self.review_tab)
                    self.paths["planning"].set(result["output"])
                    continue
                self.busy = False
                self.progress.stop()
                for button in self.buttons:
                    button.state(["!disabled"])
                self.status.set(("失败：" if error else "完成：") + label)
                if error:
                    self.write(error)
                    messagebox.showerror("任务失败", error, parent=self.root)
                else:
                    if target:
                        if target == "project":
                            for key in ("analysis", "planning", "boundaries", "draft"):
                                self.paths[key].set("")
                        self.paths[target].set(result["output"])
                    self.write(json.dumps(result, ensure_ascii=False, indent=2))
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_workspace(self):
        import os
        import subprocess
        import sys
        path = str(self.controller.workspace)
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    def close(self):
        if self.busy or self.review.running:
            messagebox.showinfo("任务运行中", "请等待当前任务结束后关闭。任务记录会保存在工作目录。", parent=self.root)
        else:
            self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local desktop testing workbench")
    parser.add_argument("--workspace", default="workspace/desktop", help="directory for new projects and task results")
    args = parser.parse_args(argv)
    root = tk.Tk()
    DesktopApp(root, args.workspace)
    root.mainloop()


if __name__ == "__main__":
    main()
