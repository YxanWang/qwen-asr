# -*- coding: utf-8 -*-
"""Qwen 本地音视频转文字界面。仅使用 Python 标准库。"""
import ctypes
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:  # 高分屏（4K/150%/175%）下必须声明 DPI 感知，否则整窗被系统拉伸发虚
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

BASE = Path(__file__).resolve().parent
PYTHON = BASE / ".venv" / "Scripts" / "python.exe"
SCRIPT = BASE / "transcribe.py"
MEDIA = "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.ts *.m4v *.mp3 *.wav *.m4a *.flac *.aac *.ogg *.opus *.wma"
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 浅色配色：灰底白卡片，蓝色为唯一强调色。
BG = "#f3f4f6"        # 窗口底
CARD = "#ffffff"      # 卡片 / 输入控件
LINE = "#e5e7eb"      # 边框
ACCENT = "#2563eb"    # 主按钮
ACCENT_DARK = "#1d4ed8"
OK = "#16a34a"        # 状态：完成
BAD = "#dc2626"       # 状态：失败
ACTIVE = "#2563eb"    # 状态：进行中
MUTED = "#6b7280"     # 次要文字
TEXT = "#111827"      # 正文


def output_path(path, out_fmt="txt"):
    """主输出文件：跳过、完成、打开都以它为准。"""
    return Path(path).with_suffix(".srt" if out_fmt == "srt" else ".txt")


def run_queue(jobs, skip_existing, speakers, out_fmt, events, cancel, process_lock, process_holder):
    """后台线程只发事件；所有界面操作留在主线程。"""
    try:
        for item_id, src in jobs:
            if cancel.is_set():
                break
            if skip_existing and output_path(src, out_fmt).exists():
                events.put(("state", item_id, "已跳过（已有结果）"))
                continue
            if not Path(src).is_file():
                events.put(("state", item_id, "失败：文件不存在"))
                continue
            events.put(("state", item_id, "准备中"))
            events.put(("detail", f"正在处理：{Path(src).name}"))
            env = os.environ.copy()
            env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
            tail = []
            try:
                with process_lock:
                    if cancel.is_set():
                        break
                    cmd = [str(PYTHON), "-u", str(SCRIPT),
                           "--speakers", str(speakers), "--format", out_fmt, str(src)]
                    proc = subprocess.Popen(
                        cmd, cwd=BASE,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", env=env,
                        creationflags=HIDDEN,
                    )
                    process_holder[0] = proc
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    tail.append(line)
                    tail = tail[-12:]
                    if "加载模型" in line:
                        events.put(("state", item_id, "加载模型"))
                    elif "说话人分离" in line:
                        events.put(("state", item_id, "分离说话人"))
                    elif "分离完成" in line:
                        events.put(("state", item_id, "分离完成"))
                    elif "转写中" in line:
                        events.put(("state", item_id, "正在转写"))
                    elif re.fullmatch(r"\d+/\d+", line):
                        events.put(("state", item_id, "转写进度 " + line))
                    elif line.startswith(("显存不足", "提示：", "错误：")):
                        events.put(("detail", line))
                code = proc.wait()
                proc.stdout.close()
                if cancel.is_set():
                    events.put(("state", item_id, "已停止"))
                    break
                if code == 0 and output_path(src, out_fmt).is_file():
                    events.put(("state", item_id, "已完成"))
                else:
                    events.put(("state", item_id, "失败"))
                    events.put(("detail", f"{Path(src).name} 转写失败：\n" + "\n".join(tail)))
            except Exception as exc:
                events.put(("state", item_id, "失败"))
                events.put(("detail", f"{Path(src).name}：{exc}"))
            finally:
                with process_lock:
                    process_holder[0] = None
    finally:
        events.put(("done", cancel.is_set()))


class SlimScrollbar(tk.Canvas):
    """Canvas 自绘的细滚动条：浅灰轨道 + 胶囊形圆角滑块，
    支持拖动、点轨道翻页、滚轮。ttk 主题画不了圆角，只能自己画。"""

    TRACK = "#eef0f3"  # 轨道浅灰：和白色列表区分开，不再融为一体
    THUMB, HOVER, PRESS = "#a8aeb5", "#868d97", "#6b7280"

    def __init__(self, master, tree, scale=1.0):
        self.w = round(12 * scale)
        super().__init__(master, bg=self.TRACK, highlightthickness=0, bd=0,
                         width=self.w, cursor="arrow")
        self.tree = tree
        self.gap = round(4 * scale)         # 滑块距轨道两端的留白
        self.inset = round(2 * scale)       # 滑块距画布左右的内缩
        self.min_thumb = round(28 * scale)  # 滑块最短长度
        self.first, self.last = 0.0, 1.0
        self._pressing = False
        self._hover = False
        self._drag_off = None
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", lambda _e: self._enter(hover=True))
        self.bind("<Leave>", lambda _e: self._enter(hover=False))
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._draw()

    # ── 几何 ──
    def _thumb(self):
        track = max(self.winfo_height() - 2 * self.gap, 1)
        span = self.last - self.first
        if span <= 0.0 or span >= 1.0:
            return None  # 内容没铺满
        length = max(track * span, min(self.min_thumb, track))
        # first 的真实取值范围是 [0, 1-span]：除以 (1-span) 才能把两端贴满轨道。
        # （直接 first*(track-length) 是二次映射，滑块永远滑不到底。）
        top = self.gap + self.first / (1.0 - span) * (track - length)
        return self.inset, top, self.w - self.inset, top + length

    def _color(self):
        if self._pressing:
            return self.PRESS
        return self.HOVER if self._hover else self.THUMB

    def _draw(self):
        self.delete("all")
        if self.winfo_height() < 12:
            return
        box = self._thumb()
        if box is None:
            return
        x0, y0, x1, y1 = box
        c = self._color()
        r = (x1 - x0) / 2
        if y1 - y0 < 2 * r:  # 滑块比宽度还短就画成圆
            self.create_oval(x0, y0, x1, y1, fill=c, outline=c)
            return
        # 中段 + 上下两个半圆端盖 = 胶囊
        self.create_rectangle(x0, y0 + r, x1, y1 - r, fill=c, outline=c)
        self.create_oval(x0, y0, x1, y0 + 2 * r, fill=c, outline=c)
        self.create_oval(x0, y1 - 2 * r, x1, y1, fill=c, outline=c)

    # ── 交互 ──
    def _enter(self, hover):
        if self._hover != hover:
            self._hover = hover
            self._draw()

    def _press(self, e):
        box = self._thumb()
        if box is None:
            return
        if box[1] - 6 <= e.y <= box[3] + 6:  # 按在滑块上：拖动
            self._drag_off = e.y - box[1]
        else:  # 按在轨道上：向该方向翻一页，然后接着拖
            self.tree.yview_scroll(-1 if e.y < (box[1] + box[3]) / 2 else 1, "pages")
            box = self._thumb()
            self._drag_off = min(max(e.y - box[1], 0), box[3] - box[1])
        self._pressing = True
        self._draw()

    def _drag(self, e):
        if not self._pressing or self._drag_off is None:
            return
        track = max(self.winfo_height() - 2 * self.gap, 1)
        span = self.last - self.first
        length = max(track * span, min(self.min_thumb, track))
        frac = (e.y - self._drag_off - self.gap) / max(track - length, 1) * (1.0 - span)
        self.tree.yview_moveto(min(max(frac, 0.0), 1.0))

    def _release(self, _e):
        self._pressing = False
        self._drag_off = None
        self._draw()

    def _wheel(self, e):
        if e.delta:
            self.tree.yview_scroll(-int(e.delta / 120), "units")


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.process_lock = threading.Lock()
        self.process_holder = [None]
        self.busy = False
        self.closing = False
        self.paths = {}
        root.title("ASR 转写")
        root.iconbitmap(str(BASE / "assets" / "asr.ico"))
        root.configure(bg=BG)
        self.scale = root.winfo_fpixels("1i") / 96.0  # 系统缩放（100% 时为 1）
        root.geometry(f"{round(920 * self.scale)}x{round(640 * self.scale)}")
        self.build_style(root)
        self.build_layout(root)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.tree.bind("<<TreeviewSelect>>", self.show_selected)
        # Treeview 默认点空白处不清除选中；补上：点行外空白或按 Esc 取消选中。
        # 双击也走同一处理：表头分隔线上的按下/双击都拦截，列宽就调不动了。
        self.tree.bind("<Button-1>", self.clear_selection_if_blank)
        self.tree.bind("<Double-1>", self.clear_selection_if_blank)
        self.tree.bind("<Escape>", lambda _e: self.tree.selection_set())
        # 聚焦时树会画 L 形虚线焦点指示器（主题层压不住）；不让它持有焦点即可根除。
        self.tree.bind("<FocusIn>", lambda _e: self.root.focus_set())
        self.tree.takefocus = 0
        root.after(100, self.poll)

    def build_style(self, root):
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=("Microsoft YaHei UI", 10))
        # 按钮：白底灰边的次按钮；蓝色实心主按钮。
        style.configure("TButton", background=CARD, foreground=TEXT, bordercolor=LINE,
                        lightcolor=CARD, darkcolor=CARD, relief="flat",
                        padding=(12, 7), focusthickness=1, focuscolor=LINE)
        style.map("TButton",
                  background=[("disabled", BG), ("active", "#e5e7eb")],
                  foreground=[("disabled", "#9ca3af")],
                  bordercolor=[("disabled", LINE)])
        style.configure("Primary.TButton", background=ACCENT, foreground=CARD,
                        bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                        padding=(16, 7), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton",
                  background=[("disabled", "#93c5fd"), ("active", ACCENT_DARK)],
                  bordercolor=[("disabled", "#93c5fd"), ("active", ACCENT_DARK)])
        style.configure("Stop.TButton", foreground=BAD)
        style.map("Stop.TButton", foreground=[("disabled", "#9ca3af"), ("active", BAD)])
        # 输入框
        style.configure("TEntry", fieldbackground=CARD, bordercolor=LINE,
                        lightcolor=CARD, darkcolor=CARD, padding=3)
        # 表格：白底、加粗表头、蓝底黑字选中行。
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        bordercolor=CARD, rowheight=round(32 * self.scale),
                        font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#eef0f3", foreground=MUTED,
                        font=("Microsoft YaHei UI", 10, "bold"), relief="flat", padding=(8, 6))
        style.map("Treeview", background=[("selected", "#dbeafe")],
                  foreground=[("selected", TEXT)])
        # 布局里剔除边框元素（辅助）；焦点虚线框的根治见 __init__ 中的 takefocus 处理。
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.map("Treeview.Heading", background=[("active", "#e2e5ea")])
        # 文字层级
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Status.TLabel", background=BG, foreground=TEXT,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TCheckbutton", background=BG, foreground=TEXT, focuscolor=BG)

    def build_layout(self, root):
        panel = ttk.Frame(root, padding=(20, 14, 20, 12))
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)

        # ── 标题区 ─────────────────────────────────────────────
        header = ttk.Frame(panel)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="ASR 转写", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="全程本地运行 · TXT / SRT 字幕 · 区分说话人",
                  style="Sub.TLabel", padding=(10, 6, 0, 0)).pack(side="left")

        # ── 工具栏：左操作右设置 ───────────────────────────────
        toolbar = ttk.Frame(panel)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        left = ttk.Frame(toolbar)
        left.pack(side="left")
        self.add_btn = ttk.Button(left, text="添加音视频", command=self.add_files)
        self.add_btn.pack(side="left")
        self.remove_btn = ttk.Button(left, text="移除所选", command=self.remove_selected)
        self.remove_btn.pack(side="left", padx=6)
        self.clear_btn = ttk.Button(left, text="全部移除", command=self.remove_all)
        self.clear_btn.pack(side="left")

        right = ttk.Frame(toolbar)
        right.pack(side="right")

        # 说话人数：标签 + 连体步进器（一个白底灰边外壳包住 － 数字 ＋）
        ttk.Label(right, text="说话人数").pack(side="left", padx=(0, 6))
        self.speakers = tk.IntVar(value=1)
        # highlightcolor 不设会默认黑色：焦点进入框内时描边会闪成深灰，钉成恒定 LINE 色
        stepper = tk.Frame(right, bg=CARD, highlightbackground=LINE,
                           highlightcolor=LINE, highlightthickness=1)
        stepper.pack(side="left")
        self.spk_down = tk.Button(stepper, text="－", width=2, bd=0, bg=CARD, relief="flat",
                                  activebackground="#e5e7eb", cursor="hand2",
                                  font=("Microsoft YaHei UI", 10, "bold"),
                                  command=lambda: self.step_speakers(-1))
        self.spk_down.pack(side="left", padx=(2, 0), pady=2)
        self.spk_entry = tk.Entry(stepper, width=3, justify="center", bd=0, bg=CARD,
                                  highlightthickness=0, textvariable=self.speakers,
                                  font=("Microsoft YaHei UI", 10))
        self.spk_entry.pack(side="left", padx=2)
        self.spk_entry.bind("<FocusOut>", lambda _e: self.clamp_speakers())
        self.spk_up = tk.Button(stepper, text="＋", width=2, bd=0, bg=CARD, relief="flat",
                                activebackground="#e5e7eb", cursor="hand2",
                                font=("Microsoft YaHei UI", 10, "bold"),
                                command=lambda: self.step_speakers(1))
        self.spk_up.pack(side="left", padx=(0, 2), pady=2)

        tk.Frame(right, width=1, bg=LINE).pack(side="left", fill="y", padx=10)

        # 输出格式：菜单式下拉，和步进器同款白底灰边外壳
        ttk.Label(right, text="输出").pack(side="left", padx=(0, 6))
        self.format = tk.StringVar(value="TXT")
        self.format_btn = tk.Menubutton(
            right, text="TXT ▾", bd=0, relief="flat", bg=CARD, fg=TEXT,
            highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE,
            activebackground="#e5e7eb", activeforeground=TEXT, cursor="hand2",
            font=("Microsoft YaHei UI", 10), padx=12, pady=7, direction="below")
        menu = tk.Menu(self.format_btn, tearoff=0, bd=0, relief="flat", bg=CARD, fg=TEXT,
                       activebackground="#dbeafe", activeforeground=TEXT,
                       selectcolor=ACCENT, font=("Microsoft YaHei UI", 10))
        for label in ("TXT", "SRT", "TXT+SRT"):
            menu.add_radiobutton(label=label, value=label, variable=self.format)
        self.format_btn.configure(menu=menu)
        self.format_btn.pack(side="left")
        self.format.trace_add("write", lambda *_: self.format_btn.configure(
            text=f"{self.format.get()} ▾"))

        tk.Frame(right, width=1, bg=LINE).pack(side="left", fill="y", padx=10)

        # 跳过已有结果：经典复选框（原生打勾样式），默认勾选
        self.skip = tk.BooleanVar(value=True)
        self.skip_btn = tk.Checkbutton(right, text="跳过已有结果", variable=self.skip,
                                       bg=BG, fg=TEXT, selectcolor=CARD, bd=0,
                                       activebackground=BG, activeforeground=TEXT,
                                       highlightthickness=0, cursor="hand2",
                                       font=("Microsoft YaHei UI", 10))
        self.skip_btn.pack(side="left")

        # ── 文件列表卡片 ───────────────────────────────────────
        table = tk.Frame(panel, bg=CARD, highlightbackground=LINE,
                         highlightcolor=LINE, highlightthickness=1)
        table.grid(row=2, column=0, sticky="nsew")
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("file", "state"), show="headings",
                                 selectmode="extended", height=5)
        self.tree.heading("file", text="音视频文件")
        self.tree.heading("state", text="状态")
        self.tree.column("file", width=round(480 * self.scale), minwidth=round(200 * self.scale))
        # 状态列收窄 + 居中：列宽刚好容下最长状态文本，居中留下的空隙才不会太大
        self.tree.column("state", width=round(130 * self.scale), minwidth=round(110 * self.scale),
                         stretch=False, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(2, 0), pady=2)
        # 滚动条用 grid：内容没铺满时整体隐藏（grid_remove 可原样恢复）。
        self._scrollbar = SlimScrollbar(table, self.tree, self.scale)
        self._scrollbar.grid(row=0, column=1, sticky="ns", pady=2,
                             padx=round(4 * self.scale))
        self.tree.configure(yscrollcommand=self._on_yscroll)
        self.tree.tag_configure("ok", foreground=OK)
        self.tree.tag_configure("bad", foreground=BAD)
        self.tree.tag_configure("active", foreground=ACTIVE)
        self.tree.tag_configure("muted", foreground=MUTED)

        # ── 底部：说明 + 操作 ─────────────────────────────────
        footer = ttk.Frame(panel)
        footer.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(footer, text="结果保存到原文件旁边：原名.txt / 原名.srt",
                  style="Muted.TLabel").pack(side="left")
        actions = ttk.Frame(footer)
        actions.pack(side="right")
        self.start_btn = ttk.Button(actions, text="开始转写", style="Primary.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="停止转写", style="Stop.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(actions, text="打开结果文件", command=self.open_text).pack(side="left")
        ttk.Button(actions, text="打开文件目录", command=self.open_folder).pack(side="left", padx=(6, 0))

        # ── 状态栏 ────────────────────────────────────────────
        ttk.Separator(panel, orient="horizontal").grid(row=4, column=0, sticky="ew", pady=(12, 6))
        self.status = tk.StringVar(value="等待添加文件")
        ttk.Label(panel, textvariable=self.status, style="Status.TLabel").grid(row=5, column=0, sticky="w")
        # 详情行用 Label：不可选中，避免 Text 拖选反白
        self.detail_var = tk.StringVar(value="")
        self.detail = ttk.Label(panel, textvariable=self.detail_var, style="Muted.TLabel",
                                wraplength=800, justify="left", anchor="w")
        self.detail.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        self.detail.bind("<Configure>",
                         lambda e: self.detail.configure(wraplength=max(50, e.width - 4)))

        # 固定工具栏和底部操作区；窗口缩小时只压缩文件列表。
        root.update_idletasks()
        fixed_height = panel.winfo_reqheight() - table.winfo_reqheight() + round(24 * self.scale)
        fixed_width = max(toolbar.winfo_reqwidth(), footer.winfo_reqwidth()) + round(48 * self.scale)
        root.minsize(max(round(720 * self.scale), fixed_width),
                     max(round(480 * self.scale), fixed_height))

    # ── 状态列着色 ────────────────────────────────────────────
    def set_state(self, item_id, text):
        self.tree.set(item_id, "state", text)
        if text == "已完成":
            tag = "ok"
        elif text.startswith(("失败",)):
            tag = "bad"
        elif text in ("等待中", "未处理", "已停止") or text.startswith("已跳过"):
            tag = "muted"
        else:
            tag = "active"
        self.tree.item(item_id, tags=(tag,))

    def _on_yscroll(self, first, last):
        """内容没铺满时把滚动条整个藏起来，铺满才显示。"""
        self._scrollbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            if self._scrollbar.winfo_ismapped():
                self._scrollbar.grid_remove()
        elif not self._scrollbar.winfo_ismapped():
            self._scrollbar.grid()

    def show_selected(self, event=None):
        selected = self.tree.selection()
        path = self.paths.get(selected[0]) if selected else None
        if path and not self.busy:
            self.log(str(path))

    def clear_selection_if_blank(self, event):
        """点在表头分隔线上时拦截（禁拖禁双击调列宽）；
        点击落在任何一行之外（比如列表下方的空白）时清空选中。"""
        if self.tree.identify_region(event.x, event.y) == "separator":
            return "break"
        if not self.tree.identify_row(event.y):
            self.tree.selection_set()

    def log(self, text):
        self.detail_var.set(text)

    def add_files(self):
        files = filedialog.askopenfilenames(title="选择音频或视频（可多选）", filetypes=[("音视频文件", MEDIA), ("所有文件", "*.*")])
        known = {os.path.normcase(str(p)) for p in self.paths.values()}
        for name in files:
            path = Path(name).resolve()
            key = os.path.normcase(str(path))
            if key in known:
                continue
            if path.suffix.lower() == ".txt":
                continue
            item = self.tree.insert("", "end", values=(path.name, "待转写"))
            self.set_state(item, "待转写")
            self.paths[item] = path
            known.add(key)
        self.status.set(f"已添加 {len(self.paths)} 个文件")

    def remove_selected(self):
        for item in self.tree.selection():
            self.tree.delete(item)
            self.paths.pop(item, None)
        self.status.set(f"已添加 {len(self.paths)} 个文件")

    def remove_all(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.paths.clear()
        self.status.set("已清空列表")

    def step_speakers(self, delta):
        try:
            value = int(self.speakers.get())
        except tk.TclError:
            value = 1
        self.speakers.set(max(1, min(8, value + delta)))

    def clamp_speakers(self):
        """输入框失焦时把手输的值收回 1–8。"""
        try:
            value = int(self.speakers.get())
        except tk.TclError:
            value = 1
        self.speakers.set(max(1, min(8, value)))

    def start(self):
        if self.busy:
            return
        if not self.paths:
            self.add_files()
            if not self.paths:
                return
        if not PYTHON.is_file() or not SCRIPT.is_file():
            messagebox.showerror("缺少运行文件", "请把界面放在 Qwen 工具目录中，确保 transcribe.py 和 .venv 文件夹存在。")
            return
        try:
            speakers = max(1, min(8, self.speakers.get()))
        except tk.TclError:
            speakers = 1
        jobs = [(item, self.paths[item]) for item in self.tree.get_children()]
        out_fmt = {"TXT": "txt", "SRT": "srt", "TXT+SRT": "both"}.get(self.format.get(), "txt")
        if not self.skip.get() and any(output_path(src, out_fmt).exists() for _, src in jobs):
            if not messagebox.askyesno("替换已有结果", "部分文件已有结果。转写成功后将替换，是否继续？"):
                return
        for item, _ in jobs:
            self.set_state(item, "等待中")
        self.busy = True
        self.cancel.clear()
        self.set_controls(True)
        self.status.set("正在依次转写，请保持界面打开")
        threading.Thread(target=run_queue, args=(jobs, self.skip.get(), speakers, out_fmt, self.events, self.cancel, self.process_lock, self.process_holder), daemon=True).start()

    def set_controls(self, busy):
        for widget in (self.start_btn, self.add_btn, self.remove_btn, self.clear_btn,
                       self.skip_btn, self.format_btn, self.spk_up, self.spk_down, self.spk_entry):
            widget.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def stop(self):
        if not self.busy:
            return
        self.cancel.set()
        self.stop_btn.configure(state="disabled")
        self.status.set("正在停止当前任务…")
        def kill_owned_process():
            with self.process_lock:
                proc = self.process_holder[0]
                if proc is not None and proc.poll() is None:
                    result = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=HIDDEN)
                    if result.returncode and proc.poll() is None:
                        proc.terminate()
        threading.Thread(target=kill_owned_process, daemon=True).start()

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "state":
                    self.set_state(event[1], event[2])
                elif event[0] == "detail":
                    self.log(event[1])
                elif event[0] == "done":
                    self.busy = False
                    self.set_controls(False)
                    for item in self.tree.get_children():
                        if self.tree.set(item, "state") in ("等待中", "准备中"):
                            self.set_state(item, "未处理")
                    states = [self.tree.set(item, "state") for item in self.tree.get_children()]
                    done = states.count("已完成")
                    failed = sum(s.startswith("失败") for s in states)
                    skipped = sum(s.startswith("已跳过") for s in states)
                    self.status.set(f"{'已停止' if event[1] else '处理结束'} · 完成 {done} · 跳过 {skipped} · 失败 {failed}")
                    if self.closing:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def selected_path(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("选择文件", "请先在列表中选中一个文件。")
            return None
        return self.paths[selected[0]]

    def open_text(self):
        src = self.selected_path()
        if src:
            out_fmt = {"TXT": "txt", "SRT": "srt", "TXT+SRT": "both"}.get(self.format.get(), "txt")
            out = output_path(src, out_fmt)
            if out.is_file():
                os.startfile(out)
            else:
                messagebox.showinfo("尚无结果", "这个文件还没有生成结果文件。")

    def open_folder(self):
        src = self.selected_path()
        if src:
            os.startfile(src.parent)

    def close(self):
        if self.busy:
            if messagebox.askyesno("停止并退出", "仍有文件正在转写。退出会停止当前任务，已完成的结果会保留。是否退出？"):
                self.closing = True
                self.stop()
        else:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
