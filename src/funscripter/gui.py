"""Tk desktop for the existing immutable, review-oriented CLI workflow."""
import argparse
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .gui_backend import DesktopController, absolute, load_model, probe_model, save_model, format_stage_failure
from .models import LLMConfig
from .review_panel import ReviewPanel


class DesktopApp:
    def __init__(self, root, workspace):
        self.root, self.controller = root, DesktopController(workspace)
        self.events, self.busy = queue.Queue(), False
        self.paths, self.inputs, self.model = {}, {}, {}
        self.speech, self.speech_advanced = {}, {}
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
        source, speech, model, workflow = (ttk.Frame(tabs, padding=14) for _ in range(4))
        tabs.add(source, text="  1  输入与项目  ")
        tabs.add(speech, text="  2  语音模型  ")
        tabs.add(model, text="  3  语义模型  ")
        tabs.add(workflow, text="  4  分步测试  ")
        for frame in (source, speech, model, workflow):
            frame.columnconfigure(1, weight=1)
        self.build_source(source)
        self.build_speech(speech)
        self.build_model(model)
        self.build_workflow(workflow)
        review = ttk.Frame(tabs, padding=10)
        tabs.add(review, text="  5  语义输出与纠正  ")
        self.review = ReviewPanel(review, self.controller.workspace, lambda: self.busy)
        self.tabs, self.review_tab = tabs, review
        self.status = tk.StringVar(value="就绪 · 语音模型负责转写，语义模型负责文本理解")
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
        self.field(frame, 0, "文本输入方式", self.inputs, "source_mode", "导入字幕",
                   choices=("导入字幕", "本地音频转写"))
        self.field(frame, 1, "音频 / 视频文件", self.inputs, "audio", browse="audio")
        self.field(frame, 2, "已有字幕（导入字幕模式）", self.inputs, "subtitles", browse="subtitles")
        self.field(frame, 3, "转写结果目录（转写模式）", self.paths, "transcription", browse="directory")
        executable = Path("workspace/tools/ffmpeg-binaries/ffmpeg.exe").resolve()
        self.field(frame, 4, "FFmpeg", self.inputs, "ffmpeg", str(executable) if executable.is_file() else "ffmpeg", browse="file")
        self.field(frame, 5, "字幕编码", self.inputs, "encoding", "utf-8-sig")
        self.field(frame, 6, "字幕偏移（毫秒）", self.inputs, "offset", "0")
        probe = executable.with_name("ffprobe.exe")
        self.field(frame, 7, "FFprobe（视频需使用）", self.inputs, "ffprobe", str(probe) if probe.is_file() else "ffprobe", browse="file")
        self.field(frame, 8, "音轨序号（从 0 开始）", self.inputs, "audio_stream", "0")
        self.button(frame, "创建新项目", self.create).grid(row=9, column=1, sticky="w", pady=8)
        ttk.Separator(frame).grid(row=10, column=0, columnspan=3, sticky="ew", pady=5)
        self.field(frame, 11, "已有项目目录", self.paths, "project", browse="directory")
        self.button(frame, "打开并校验项目", self.inspect).grid(row=12, column=1, sticky="w", pady=5)
        ttk.Label(frame, text="导入字幕：选择音频与字幕后创建项目。\n音频转写：先选音频，再到“语音模型”执行转写，返回此页创建项目。已有转写结果可直接选择，无需重跑。", wraplength=800).grid(row=13, column=0, columnspan=3, sticky="w", pady=5)

    def build_speech(self, frame):
        from .transcription import TranscriptionProfile
        defaults = TranscriptionProfile(model_path="").model_dump(mode="json")
        self.speech_advanced = defaults
        self.field(frame, 0, "本地模型目录（CTranslate2）", self.speech, "model_path", browse="directory")
        self.field(frame, 1, "运行设备", self.speech, "device", "cpu", choices=("cpu", "cuda", "auto"))
        self.field(frame, 2, "计算精度", self.speech, "compute_type", "int8",
                   choices=("int8", "float16", "float32", "int8_float16", "default", "auto"))
        self.field(frame, 3, "识别语言（留空自动识别）", self.speech, "language", "ja")
        self.field(frame, 4, "搜索宽度（beam size）", self.speech, "beam_size", "5")
        actions = ttk.Frame(frame)
        actions.grid(row=5, column=0, columnspan=3, sticky="w", pady=12)
        self.button(actions, "载入语音模型 YAML", self.load_speech_profile).pack(side="left", padx=(0, 8))
        self.button(actions, "保存语音模型 YAML", self.save_speech_profile).pack(side="left", padx=(0, 8))
        self.button(actions, "测试模型加载", self.probe_speech).pack(side="left", padx=(0, 8))
        self.button(actions, "转写输入页所选音频", self.transcribe).pack(side="left")
        self.speech_status = tk.StringVar(value="尚未测试或转写")
        ttk.Label(frame, textvariable=self.speech_status, wraplength=800).grid(row=6, column=0, columnspan=3, sticky="w", pady=5)
        ttk.Label(frame, text="语音模型在本机加载模型并识别音频，不需要预先启动服务。YAML 用于保存设置，也可直接填写本页。\n加载测试会实际加载模型后释放；执行转写时重新加载。加载通过不代表识别质量通过。\n音频、FFmpeg 和音轨选择沿用输入页。完成后自动填入转写结果目录，再由输入页创建项目。\n只识别原语言；转写原文和时间供“语义模型”后续分析。", wraplength=800).grid(row=7, column=0, columnspan=3, sticky="w", pady=5)

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
        self.button(actions, "载入语义模型 YAML", self.load_profile).pack(side="left", padx=(0, 8))
        self.button(actions, "保存语义模型 YAML", self.save_profile).pack(side="left", padx=(0, 8))
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
        self.field(frame, 6, "交互预览目录", self.paths, "preview", browse="directory")
        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, columnspan=3, sticky="w", pady=12)
        for label, command in (("① 分析音频", "analyze"), ("② 模型规划", "plan"),
                               ("③ 句尾候选（可选）", "utterance-candidates"), ("④ 融合草稿", "fuse"), ("⑤ 诊断视频", "diagnostic")):
            self.button(actions, label, lambda stage=command: self.stage(stage)).pack(side="left", padx=(0, 7))
        ttk.Label(frame, text="先在“输入与项目”创建或打开项目。每一步成功后自动填入结果目录；也可手动选择此前结果。\n模型规划使用“语义模型”页当前设置；融合和诊断使用各自 YAML。\n交互预览读取融合草稿，无需合成视频；填写匹配的规划目录可联动查看原始解释。", wraplength=820).grid(row=8, column=0, columnspan=3, sticky="w", pady=10)
        previews = ttk.Frame(frame)
        previews.grid(row=9, column=0, columnspan=3, sticky="w", pady=5)
        self.button(previews, "生成交互预览", self.make_preview).pack(side="left", padx=(0, 8))
        self.button(previews, "打开交互预览", self.open_preview).pack(side="left", padx=(0, 8))
        self.button(previews, "打开工作目录", self.open_workspace).pack(side="left")

    def make_preview(self):
        def start():
            project, draft, planning = self.path("project"), self.path("draft"), self.path("planning", True)
            self.start("生成交互预览", lambda: self.controller.preview(project, draft, planning), "preview")
        self.guard(start)

    def open_preview(self):
        def open_page():
            import webbrowser
            page = Path(self.path("preview")) / "index.html"
            if not page.is_file():
                raise ValueError("请先生成或选择有效的交互预览目录")
            webbrowser.open(page.resolve().as_uri())
        self.guard(open_page)

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
            if values["source_mode"] == "本地音频转写":
                values["transcription_directory"] = self.path("transcription")
            self.start("创建项目", lambda: self.controller.create(**values), "project")
        self.guard(start)

    def speech_values(self):
        from .transcription import TranscriptionProfile
        values = dict(self.speech_advanced)
        values.update({key: variable.get().strip() for key, variable in self.speech.items()})
        if not values["model_path"]:
            raise ValueError("请选择本地语音模型目录")
        values["model_path"] = absolute(values["model_path"])
        values["language"] = values["language"] or None
        values["beam_size"] = int(values["beam_size"])
        return TranscriptionProfile.model_validate_json(json.dumps(values))

    def load_speech_profile(self):
        def load():
            from .transcription import load_profile
            path = filedialog.askopenfilename(parent=self.root, filetypes=[("YAML", "*.yaml *.yml")])
            if not path:
                return
            values = load_profile(path).model_dump(mode="json")
            self.speech_advanced = values
            for key, variable in self.speech.items():
                variable.set("" if values[key] is None else str(values[key]))
            self.speech_status.set("已载入配置，尚未测试模型加载")
            self.write("已载入语音模型配置：" + path)
        self.guard(load)

    def save_speech_profile(self):
        def save():
            import yaml
            values = self.speech_values().model_dump(mode="json")
            path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".yaml", filetypes=[("YAML", "*.yaml")])
            if path:
                Path(path).write_text(yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")
                self.write("已保存语音模型配置：" + path)
        self.guard(save)

    def probe_speech(self):
        def start():
            from .transcription import probe_whisper
            profile = self.speech_values()
            self.speech_status.set("正在测试模型加载…")
            self.start("语音模型加载测试", lambda: probe_whisper(profile))
        self.guard(start)

    def transcribe(self):
        def start():
            import yaml
            profile = self.speech_values()
            audio = self.inputs["audio"].get().strip()
            if not audio:
                raise ValueError("请先在输入页选择音频")
            settings = {key: self.inputs[key].get().strip() for key in ("ffmpeg", "ffprobe", "audio_stream")}
            snapshot = self.controller.fresh("whisper-profile").with_suffix(".yaml")
            snapshot.write_text(yaml.safe_dump(profile.model_dump(mode="json"), allow_unicode=True), encoding="utf-8")
            self.speech_status.set("正在转写…")
            self.start("语音模型音频转写", lambda: self.controller.transcribe(audio, str(snapshot), **settings), "transcription")
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
                    raise ValueError(format_stage_failure(result))
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
                    if label.startswith("语音模型"):
                        self.speech_status.set("上次操作失败：" + error)
                    self.write(error)
                    messagebox.showerror("任务失败", error, parent=self.root)
                else:
                    if label.startswith("语音模型"):
                        self.speech_status.set("上次加载测试通过（模型已释放）" if target is None else "转写完成；可返回输入页创建项目")
                    if target == "transcription":
                        self.inputs["source_mode"].set("本地音频转写")
                    if target:
                        if target == "project":
                            for key in ("analysis", "planning", "boundaries", "draft", "preview"):
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
