"""Batch-level live model output viewer for the Tk workbench."""
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from .model_review import entries, inspect_entry, save_feedback, reanalyze, read


class ReviewPanel:
    def __init__(self, parent, workspace, task_busy):
        self.parent, self.workspace, self.task_busy = parent, workspace, task_busy
        self.rows, self.shown, self.signature = {}, None, None
        self.running, self.last_refresh = False, 0
        self.events = queue.Queue()
        top = ttk.Frame(parent)
        top.pack(fill="x")
        self.directory = tk.StringVar()
        ttk.Label(top, text="规划目录").pack(side="left")
        ttk.Entry(top, textvariable=self.directory).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="选择…", command=self.choose).pack(side="left")
        ttk.Button(top, text="刷新", command=lambda: self.refresh(True)).pack(side="left", padx=5)
        self.auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="自动刷新", variable=self.auto).pack(side="left")
        self.filter = tk.StringVar()
        search = ttk.Frame(parent)
        search.pack(fill="x", pady=4)
        ttk.Label(search, text="查找字幕 ID / 阶段 / 状态").pack(side="left")
        ttk.Entry(search, textvariable=self.filter).pack(side="left", fill="x", expand=True, padx=6)
        self.filter.trace_add("write", lambda *_: self.refresh(True))
        self.status = tk.StringVar(value="选择已有规划，或运行模型规划后自动跟踪。批次完成后显示回答，非逐字流式。")
        ttk.Label(parent, textvariable=self.status).pack(anchor="w")
        split = ttk.Panedwindow(parent, orient="horizontal")
        split.pack(fill="both", expand=True, pady=5)
        left, right = ttk.Frame(split), ttk.Notebook(split)
        split.add(left, weight=1)
        split.add(right, weight=3)
        self.tree = ttk.Treeview(left, columns=("task", "status", "cue"), show="headings", height=9)
        for key, title, width in (("task", "阶段 / 次数", 105), ("status", "状态", 65), ("cue", "字幕范围", 150)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width)
        bar = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.texts = {}
        for key, title in (("analysis", "台词与分析对照"), ("answer", "原始回答 JSON"), ("request", "输入台词 / 提示词"),
                           ("reasoning", "服务返回的思考文本"), ("metadata", "校验 / 耗时 / 用量")):
            box = ScrolledText(right, wrap="word", font=("Microsoft YaHei UI", 10), height=9, state="disabled")
            right.add(box, text=title)
            self.texts[key] = box
        ttk.Label(parent, text="纠正意见：指出误判、上下文和期望解释。重分析结果单独保存，需复核后再接入规划。").pack(anchor="w")
        self.note = ScrolledText(parent, height=3, wrap="word")
        self.note.pack(fill="x")
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=4)
        ttk.Button(actions, text="保存纠正意见", command=self.save).pack(side="left")
        ttk.Button(actions, text="按意见重分析所选请求", command=self.retry).pack(side="left", padx=6)
        ttk.Label(actions, text="重分析思考模式").pack(side="left", padx=5)
        self.thinking = tk.StringVar(value="沿用原设置")
        ttk.Combobox(actions, textvariable=self.thinking, values=("沿用原设置", "开启", "关闭"), state="readonly", width=12).pack(side="left")
        self.parent.after(1000, self.poll)
        runs = list(workspace.glob("*/planning_run.json"))
        if runs:
            self.follow(str(max(runs, key=lambda path: path.stat().st_mtime).parent))

    def choose(self):
        path = filedialog.askdirectory(parent=self.parent)
        if path:
            self.follow(path)

    def follow(self, path):
        self.directory.set(path)
        self.shown, self.signature = None, None
        self.refresh(True)

    def refresh(self, force=False):
        if not self.directory.get().strip():
            return
        try:
            rows = entries(self.directory.get().strip())
            signature = [(r["key"], r["attempt"], r["updated"], r["status"], r["error"]) for r in rows], self.filter.get()
            if not force and signature == self.signature:
                return
            self.signature = signature
            selected = self.tree.selection()
            selected = selected[0] if selected else None
            self.tree.delete(*self.tree.get_children())
            self.rows = {}
            for row in rows:
                cue = " ～ ".join(row["cue_ids"][::max(1, len(row["cue_ids"])-1)]) if row["cue_ids"] else "全局合并"
                if self.filter.get().strip().casefold() not in (row["task"] + row["status"] + " ".join(row["cue_ids"])).casefold():
                    continue
                key = row["key"] + ":" + str(row["attempt"])
                self.rows[key] = row
                self.tree.insert("", "end", iid=key, values=(row["task"] + " / " + str(row["attempt"]), row["status"], cue))
            if self.rows:
                key = selected if selected in self.rows else next(reversed(self.rows))
                self.tree.selection_set(key)
                self.tree.see(key)
                self.select()
            self.status.set(f"已发现 {len(rows)} 次请求；显示 {len(self.rows)} 条。批次完成后更新；思考文本仅在服务返回时可见。")
        except (OSError, ValueError, KeyError) as exc:
            self.status.set("读取中／目录不可用：" + str(exc))

    def select(self, *_):
        selection = self.tree.selection()
        if not selection or selection[0] not in self.rows:
            return
        entry = self.rows[selection[0]]
        try:
            detail = inspect_entry(self.directory.get(), entry)
            detail["metadata"] = json.dumps({k: entry[k] for k in ("status", "error", "elapsed_seconds", "key", "attempt")}, ensure_ascii=False, indent=2) + "\n" + detail["metadata"]
            for key, box in self.texts.items():
                box.configure(state="normal")
                box.delete("1.0", "end")
                box.insert("end", detail[key])
                box.configure(state="disabled")
            self.shown = entry
        except (OSError, ValueError, KeyError) as exc:
            self.status.set(str(exc))

    def save(self):
        try:
            if not self.shown:
                raise ValueError("请先选择一个请求")
            folder = save_feedback(self.workspace, self.directory.get(), self.shown, self.note.get("1.0", "end"))
            self.status.set("意见已保存：" + str(folder))
        except (OSError, ValueError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.parent)

    def retry(self):
        if self.running or self.task_busy():
            messagebox.showinfo("请稍候", "可以先保存意见。当前任务完成后再重分析，避免与正在规划的请求混在一起。", parent=self.parent)
            return
        if not self.shown or not self.note.get("1.0", "end").strip():
            messagebox.showinfo("需要纠正意见", "请选择请求并填写纠正意见。", parent=self.parent)
            return
        directory, entry, note = self.directory.get(), dict(self.shown), self.note.get("1.0", "end")
        thinking = {"开启": True, "关闭": False}.get(self.thinking.get())
        self.running = True
        self.status.set("正在按意见重分析所选请求……")
        def work():
            try:
                folder = reanalyze(self.workspace, directory, entry, note, thinking=thinking)
                self.events.put((folder, None))
            except Exception as exc:
                self.events.put((None, str(exc)))
        threading.Thread(target=work, daemon=False).start()

    def poll(self):
        try:
            folder, error = self.events.get_nowait()
            self.running = False
            if error:
                self.status.set(error)
            else:
                detail = inspect_entry(folder, {"request": folder / "request.json", "response": folder / "response-1.json"})
                for key, box in self.texts.items():
                    box.configure(state="normal")
                    box.delete("1.0", "end")
                    box.insert("end", "纠正后的候选，尚未应用到规划\n保存位置：" + str(folder) + "\n\n" + detail[key])
                    box.configure(state="disabled")
                self.status.set("重分析完成，候选未自动应用：" + str(folder))
                self.auto.set(False)
        except queue.Empty:
            pass
        if self.auto.get() and time.monotonic() - self.last_refresh > 2:
            self.last_refresh = time.monotonic()
            self.refresh()
        self.parent.after(1000, self.poll)
