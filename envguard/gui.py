"""Native window: the whole report drawn by tkinter, no browser involved.

An earlier revision served this report as HTML from a loopback server and opened
a browser. Drawing in-process removes the socket, the browser-profile dependency
and the extraction step; what it gives up is the browser-only half (WebRTC
candidates, the page's own timezone), which stays in ClaudeEnvGuard.html and is
named in the footer so a missing row never reads as a pass.

Colour: light theme. Panels sit on a warm off-white rather than pure white, with a
hairline border instead of a shadow — tkinter cannot draw shadows, and a shadowless
white-on-white card would have no edge at all. Coral #D97757 stays the single
accent; status colours stay red/amber/green because they carry state, not brand.

The mark is loaded with tkinter's own PhotoImage (Tk 8.6 reads PNG directly).
PIL is deliberately NOT used: the build excludes it to keep the exe small, and an
earlier version imported PIL inside a try/except, so the icon vanished silently in
the frozen build while still working from source. A feature that can disappear
without an error needs a loader that cannot be absent.
"""

import json
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

from . import collector, report as report_mod

# Light surface stack: page, panels, hover/alt, hairlines.
BG = "#ffffff"
SURFACE = "#faf9f6"
ALT = "#f0eee8"
BORDER = "#e2dfd6"
TEXT = "#1f1e1d"
DIM = "#6f6b63"
ACCENT = "#d97757"
ACCENT_TEXT = "#8a3f28"
WEIGHT_DIM = "#a6a29a"

STATUS_COLOR = {"good": "#2f7d4f", "warn": "#9a6b00", "bad": "#b4372c", "unknown": "#8b877f"}
STATUS_LABEL = {"good": "通过", "warn": "注意", "bad": "风险", "unknown": "未检测"}
VERDICT_COLOR = {"ok": "#2f7d4f", "caution": "#9a6b00", "risky": "#b4372c"}
VERDICT_LABEL = {"ok": "环境良好", "caution": "有可改进项", "risky": "存在明显风险"}
FACTOR_LABEL = {"network": "网络", "device": "设备", "behavior": "使用"}

# Self-check rows: facts only the user knows. Deliberately OUT of the score — a
# self-report folded into a measurement stops the number being a measurement.
SELF_CHECK = (
    ("注册", (
        ("reg_email", "注册邮箱不是 Gmail", "有更高权重的邮箱服务时优先用它"),
        ("reg_no_kyc", "注册时没有弹 KYC", "注册即 KYC 的号建议直接弃用"),
        ("reg_mobile", "用手机 App 完成过注册", "手机 App 注册会加分"),
    )),
    ("支付", (
        ("pay_delay", "不是刚注册就订阅", "刚注册就订阅会扣分"),
        ("pay_pro_first", "先订 Pro 用了两天，再考虑 Max", "直接上 Max 大幅扣分"),
        ("pay_no_kyc", "支付时没有弹 KYC", "弹了说明账号还在考察期"),
        ("pay_real_card", "用国外实体银行卡或 Google Play 支付", "虚拟卡扣分"),
    )),
    ("使用", (
        ("use_not_maxed", "没有一次把限额用完", "宁可两个号轮换，也不要一个号撸到底"),
        ("use_desktop", "主要用 Desktop 客户端", "比网页端略加分"),
        ("use_mobile", "手机端也有使用", "少量加分"),
        ("use_memory", "记忆功能已开启、记忆文件在积累", "记忆越多分越高"),
        ("use_no_fable", "没有大量使用 Fable", "大量使用会扣分"),
    )),
    ("IP 与设备", (
        ("ip_stable", "出口 IP 长期稳定，没有跳变", "稳定性是关键"),
        ("ip_global_tun", "代理是全局模式且开了 TUN", "跳 IP、泄漏都是大扣分项"),
        ("dev_single", "没有多设备同时登录", "理想状态是一台电脑一个客户端"),
        ("dev_calm", "没有频繁换 IP、频繁登录、高并发", "这些都会大幅扣分"),
    )),
    ("账号年龄", (
        ("age_old", "账号是 2023 年或更早注册的", "老号容错空间大得多"),
    )),
)


def _asset(name: str) -> str | None:
    """Locate a bundled asset, frozen or from source."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "assets", name))
        candidates.append(os.path.join(base, name))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "assets", name))
    candidates.append(os.path.join(os.path.dirname(here), "assets", name))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Claude 环境自检")
        self.geometry("1020x860")
        self.minsize(860, 660)
        self.configure(bg=BG)

        self.report = None
        self._queue: list = []
        self._selfcheck: dict[str, tk.BooleanVar] = {}
        self._selfcheck_total = 0
        self._mark = None

        self._light_titlebar()
        self._build_header()
        self._build_factors()
        self._build_body()
        self._build_footer()
        self.after(120, self.start_scan)

    # ---------- chrome ----------

    def _light_titlebar(self) -> None:
        """Keep the frame light so it matches the page.

        Windows draws the frame from the app's own theme state; without this it
        keeps whatever the last dark window set, and a dark bar over a white page
        reads as a rendering fault. Best effort: unsupported builds keep the
        system default.
        """
        try:
            import ctypes

            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            for attribute in (20, 19):
                value = ctypes.c_int(0)  # 0 = light frame
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                ) == 0:
                    break
        except Exception:
            pass

    def _load_mark(self, parent: tk.Widget) -> None:
        """Draw the official Claude mark next to the title.

        tkinter's own PNG decoder is used on purpose, so the frozen build cannot
        lose the icon the way the PIL-dependent version silently did.
        """
        path = _asset("claude-mark-64.png") or _asset("claude-mark.png")
        if path is None:
            return
        try:
            image = tk.PhotoImage(file=path)
            if image.width() >= 64:
                image = image.subsample(2, 2)  # 64 px file -> 32 px mark
            self._mark = image
            tk.Label(parent, image=self._mark, bg=BG).pack(side="left", padx=(0, 10))
        except Exception:
            self._mark = None

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=BG, padx=26, pady=20)
        header.pack(fill="x")

        left = tk.Frame(header, bg=BG)
        left.pack(side="left")

        brand = tk.Frame(left, bg=BG)
        brand.pack(anchor="w")
        self._load_mark(brand)
        tk.Label(
            brand, text="Claude 环境自检", bg=BG, fg=TEXT,
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(side="left")

        tk.Label(
            left, text="只统计本机能测到的事实；测不到的标「未检测」，不猜也不计入分母。",
            bg=BG, fg=DIM, font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(6, 0))

        right = tk.Frame(header, bg=BG)
        right.pack(side="right")
        self.score_label = tk.Label(right, text="—", bg=BG, fg=WEIGHT_DIM,
                                    font=("Microsoft YaHei UI", 30, "bold"))
        self.score_label.pack(anchor="e")
        self.verdict_label = tk.Label(right, text="", bg=BG, fg=DIM,
                                      font=("Microsoft YaHei UI", 9))
        self.verdict_label.pack(anchor="e")

    def _build_factors(self) -> None:
        panel = tk.Frame(self, bg=SURFACE, padx=18, pady=12,
                         highlightbackground=BORDER, highlightthickness=1)
        panel.pack(fill="x", padx=26)
        self.factor_rows = {}
        for key in ("network", "device", "behavior"):
            row = tk.Frame(panel, bg=SURFACE)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=FACTOR_LABEL[key], bg=SURFACE, fg=DIM, width=6,
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(side="left")
            bar = tk.Canvas(row, height=8, bg=ALT, highlightthickness=0)
            bar.pack(side="left", fill="x", expand=True, padx=(8, 10))
            value = tk.Label(row, text="—", bg=SURFACE, fg=DIM, width=13,
                             font=("Microsoft YaHei UI", 9), anchor="e")
            value.pack(side="right")
            self.factor_rows[key] = (bar, value)

    def _build_body(self) -> None:
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=26, pady=(14, 0))

        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.list = tk.Frame(self.canvas, bg=BG)
        self.list.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self._list_id = self.canvas.create_window((0, 0), window=self.list, anchor="nw")
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._list_id, width=e.width),
        )
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.canvas.bind_all(
            "<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units")
        )

    def _build_footer(self) -> None:
        footer = tk.Frame(self, bg=BG, padx=26, pady=14)
        footer.pack(fill="x")

        self.status = tk.Label(footer, text="准备中…", bg=BG, fg=DIM,
                               font=("Microsoft YaHei UI", 9), anchor="w",
                               justify="left", wraplength=520)
        self.status.pack(side="left")

        self.export_button = tk.Button(
            footer, text="导出报告", command=self.export,
            bg=ALT, fg=TEXT, relief="flat", padx=15, pady=6,
            activebackground=BORDER, activeforeground=TEXT,
            font=("Microsoft YaHei UI", 9), state="disabled", cursor="hand2",
        )
        self.export_button.pack(side="right", padx=(8, 0))

        self.copy_button = tk.Button(
            footer, text="复制文本", command=self.copy,
            bg=ALT, fg=TEXT, relief="flat", padx=15, pady=6,
            activebackground=BORDER, activeforeground=TEXT,
            font=("Microsoft YaHei UI", 9), state="disabled", cursor="hand2",
        )
        self.copy_button.pack(side="right", padx=(8, 0))

        self.scan_button = tk.Button(
            footer, text="重新检测", command=self.start_scan,
            bg=ACCENT, fg="#ffffff", relief="flat", padx=18, pady=6,
            activebackground="#c9663f", activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9, "bold"), cursor="hand2",
        )
        self.scan_button.pack(side="right")

    # ---------- scan ----------

    def start_scan(self) -> None:
        self.scan_button.configure(state="disabled", text="检测中…")
        self.copy_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        for widget in self.list.winfo_children():
            widget.destroy()
        for bar, value in self.factor_rows.values():
            bar.delete("all")
            value.configure(text="—", fg=DIM)
        self.score_label.configure(text="—", fg=WEIGHT_DIM)
        self.verdict_label.configure(text="")

        self._queue.clear()
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(80, self._drain)

    def _worker(self) -> None:
        try:
            result = collector.collect(
                progress=lambda text: self._queue.append(("progress", text))
            )
        except Exception as error:  # a probe crash must not kill the window
            self._queue.append(("error", f"{type(error).__name__}: {error}"))
            return
        self._queue.append(("done", result))

    def _drain(self) -> None:
        while self._queue:
            kind, payload = self._queue.pop(0)
            if kind == "progress":
                self.status.configure(text=payload + "…", fg=DIM)
            elif kind == "error":
                self.status.configure(text=f"检测失败：{payload}", fg=STATUS_COLOR["bad"])
                self.scan_button.configure(state="normal", text="重新检测")
                return
            elif kind == "done":
                self._render(payload)
                return
        self.after(80, self._drain)

    def _render(self, result) -> None:
        self.report = result
        score, earned, total = result.total()
        verdict = result.verdict()
        self.score_label.configure(text=f"{score:.0f}", fg=VERDICT_COLOR[verdict])
        self.verdict_label.configure(
            text=f"{VERDICT_LABEL[verdict]}　{earned}/{total} 分权重",
            fg=VERDICT_COLOR[verdict],
        )
        self.status.configure(
            text=f"完成于 {result.generated_at}　·　WebRTC / 浏览器时区不在本窗口，见 HTML 版",
            fg=DIM,
        )

        for key, (bar, value) in self.factor_rows.items():
            factor_score, factor_earned, factor_total = result.factor_score(key)
            if factor_total == 0:
                value.configure(text="无可用检查")
                continue
            self._paint_bar(bar, factor_score)
            value.configure(text=f"{factor_score:.0f}  ({factor_earned}/{factor_total})", fg=TEXT)

        order = {"bad": 0, "warn": 1, "unknown": 2, "good": 3}
        for check in sorted(result.checks, key=lambda c: (order[c.status], c.factor)):
            self._add_check(check)

        self._add_selfcheck()

        self.scan_button.configure(state="normal", text="重新检测")
        self.copy_button.configure(state="normal")
        self.export_button.configure(state="normal")

    def _paint_bar(self, bar: tk.Canvas, percent: float) -> None:
        bar.delete("all")
        bar.update_idletasks()
        width = max(bar.winfo_width(), 140)
        color = STATUS_COLOR["good"] if percent >= 80 else (
            STATUS_COLOR["warn"] if percent >= 55 else STATUS_COLOR["bad"]
        )
        bar.create_rectangle(0, 0, width, 8, fill=ALT, outline="")
        filled = int(width * max(0.0, min(100.0, percent)) / 100)
        if filled:
            bar.create_rectangle(0, 0, filled, 8, fill=color, outline="")

    def _add_check(self, check) -> None:
        card = tk.Frame(self.list, bg=SURFACE, padx=16, pady=11,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", pady=(0, 8))

        top = tk.Frame(card, bg=SURFACE)
        top.pack(fill="x")
        tk.Label(top, text=STATUS_LABEL[check.status], bg=SURFACE, fg=STATUS_COLOR[check.status],
                 font=("Microsoft YaHei UI", 9, "bold"), width=5, anchor="w").pack(side="left")
        tk.Label(top, text=check.title, bg=SURFACE,
                 fg=TEXT if check.status != "unknown" else DIM,
                 font=("Microsoft YaHei UI", 10), anchor="w").pack(side="left", padx=(6, 0))
        tk.Label(top, text=f"权重 {check.weight}", bg=SURFACE, fg=WEIGHT_DIM,
                 font=("Microsoft YaHei UI", 8)).pack(side="right")

        tk.Label(card, text=check.detail, bg=SURFACE,
                 fg=DIM if check.status == "unknown" else "#4a4740",
                 font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
                 wraplength=880).pack(fill="x", pady=(5, 0))
        if check.advice:
            tk.Label(card, text="→ " + check.advice, bg=SURFACE, fg="#6f6b63",
                     font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
                     wraplength=880).pack(fill="x", pady=(4, 0))

    def _add_selfcheck(self) -> None:
        head = tk.Frame(self.list, bg=BG)
        head.pack(fill="x", pady=(16, 8))
        tk.Label(head, text="自检列表", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        tk.Label(head, text="　机器测不到、只有你自己知道的事", bg=BG, fg=DIM,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")

        panel = tk.Frame(self.list, bg=SURFACE, padx=16, pady=12,
                         highlightbackground=BORDER, highlightthickness=1)
        panel.pack(fill="x", pady=(0, 8))

        tk.Label(
            panel,
            text="这些不是检测项：勾选只统计「已确认」，不参与上面的分数——"
                 "自报混进测量，分数就不诚实了。",
            bg=SURFACE, fg="#6f6b63", font=("Microsoft YaHei UI", 9),
            anchor="w", justify="left", wraplength=880,
        ).pack(fill="x", pady=(0, 6))

        self._selfcheck.clear()
        total = 0
        for group, items in SELF_CHECK:
            tk.Label(panel, text=group, bg=SURFACE, fg=ACCENT_TEXT,
                     font=("Microsoft YaHei UI", 9, "bold"), anchor="w").pack(fill="x", pady=(8, 2))
            for key, text, hint in items:
                total += 1
                var = tk.BooleanVar(value=False)
                self._selfcheck[key] = var
                row = tk.Frame(panel, bg=SURFACE)
                row.pack(fill="x", pady=1)
                tk.Checkbutton(
                    row, text=text, variable=var, command=self._update_selfcheck,
                    bg=SURFACE, fg=TEXT, selectcolor="#ffffff",
                    activebackground=SURFACE, activeforeground=TEXT,
                    font=("Microsoft YaHei UI", 9), anchor="w", cursor="hand2",
                    highlightthickness=0, bd=0,
                ).pack(side="left")
                tk.Label(row, text=hint, bg=SURFACE, fg=WEIGHT_DIM,
                         font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(8, 0))

        self.selfcheck_summary = tk.Label(
            panel, text="", bg=SURFACE, fg=DIM,
            font=("Microsoft YaHei UI", 9), anchor="w",
        )
        self.selfcheck_summary.pack(fill="x", pady=(10, 0))
        self._selfcheck_total = total
        self._update_selfcheck()

    def _update_selfcheck(self) -> None:
        done = sum(1 for var in self._selfcheck.values() if var.get())
        total = self._selfcheck_total
        self.selfcheck_summary.configure(
            text=f"已确认 {done}/{total}",
            fg=STATUS_COLOR["good"] if total and done == total else DIM,
        )

    # ---------- output ----------

    def copy(self) -> None:
        if self.report is None:
            return
        text = report_mod.to_text(self.report)
        done = sum(1 for var in self._selfcheck.values() if var.get())
        text += f"\n\n自检列表：已确认 {done}/{self._selfcheck_total}"
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text="已复制文本报告到剪贴板", fg=DIM)

    def export(self) -> None:
        if self.report is None:
            return
        path = filedialog.asksaveasfilename(
            title="保存检测报告",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("Markdown", "*.md"), ("所有文件", "*.*")],
            initialfile="claude-env-report.json",
        )
        if not path:
            return
        done = [key for key, var in self._selfcheck.items() if var.get()]
        try:
            if path.lower().endswith(".md"):
                body = report_mod.to_markdown(self.report)
                body += f"\n\n## 自检列表\n\n已确认 {len(done)}/{self._selfcheck_total}\n\n"
                for group, items in SELF_CHECK:
                    for key, text, _hint in items:
                        tick = "x" if key in done else " "
                        body += f"- [{tick}] {group}｜{text}\n"
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(body)
            else:
                payload = report_mod.to_dict(self.report)
                payload["selfCheck"] = done
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as error:
            messagebox.showerror("保存失败", str(error))
            return
        self.status.configure(text=f"已保存到 {path}", fg=DIM)


def main() -> int:
    App().mainloop()
    return 0
