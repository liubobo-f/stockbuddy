# -*- coding: utf-8 -*-
"""
自选股票托盘小工具 (StockTray)
================================
常驻 Windows 系统托盘，自定义弹窗菜单展示自选股行情。
菜单跟随系统深色/浅色主题，点击顶部时间行可刷新行情。

行情数据由 QuoteFetcher 编排器提供（多源容灾，无需鉴权，运行时联网获取）。
配置文件位置：%APPDATA%/StockTray/stocks.csv
"""

import os
import subprocess
import threading
import time
import winreg
from datetime import datetime

import tkinter as tk
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from quote_fetcher import QuoteFetcher, TradingSchedule, QuoteRow, debug_log

from config import (
    APP_NAME, APP_TITLE, BUILD,
    BG_TIMEOUT, MENU_TIMEOUT, CONFIG_DIR, BG_REFRESH_INTERVAL,
    DEFAULT_STOCKS,
    MENU_ITEM_WIDTH, MENU_PRICE_WIDTH, MENU_PCT_WIDTH,
)


# ============================================================================
# 配置管理
# ============================================================================
class StockConfig:
    """自选股配置管理。

    文件位置: %APPDATA%/StockTray/stocks.csv
    格式: 每行一个股票代码，支持 # 注释。
    """

    def __init__(self, config_dir=CONFIG_DIR):
        self.config_dir = config_dir
        self.path = os.path.join(config_dir, "stocks.csv")
        os.makedirs(config_dir, exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("\n".join(DEFAULT_STOCKS) + "\n")

    def load(self):
        """读取自选股代码列表。"""
        try:
            with open(self.path, encoding="utf-8") as f:
                return [
                    line.strip().split(",")[0].strip()
                    for line in f
                    if line.strip() and not line.strip().startswith("#")
                ]
        except Exception:
            return list(DEFAULT_STOCKS)

    def mtime(self):
        """配置文件修改时间（用于检测用户是否改过自选股）。"""
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def open_in_editor(self):
        """用系统默认文本编辑器打开配置文件。"""
        abs_path = os.path.abspath(self.path)
        try:
            subprocess.Popen(["notepad", abs_path])
        except Exception:
            if hasattr(os, "startfile"):
                os.startfile(abs_path, "open")
            else:
                os.system(f'notepad "{abs_path}"')


# ============================================================================
# 菜单格式化（调试/测试用，弹窗菜单使用 tkinter grid 布局）
# ============================================================================
def _display_width(text):
    """估算字符串在等宽字体中的显示宽度（CJK 字符约 2 格，ASCII 约 1 格）。"""
    width = 0
    for char in text:
        width += 2 if ord(char) > 127 else 1
    return width


def _pad_right(text, target_width):
    """右填充空格使文本达到目标显示宽度。"""
    current = _display_width(text)
    return text + " " * max(1, target_width - current)


def _format_quote_label(row: QuoteRow):
    """格式化单只股票菜单项文本：名称左对齐，价格和涨跌幅分别右对齐。"""
    arrow = "▲" if row.pct > 0 else ("▼" if row.pct < 0 else "—")
    sign = "+" if row.pct > 0 else ""
    price_str = f"{row.price:.2f}" if isinstance(row.price, (int, float)) else "-"

    price_col = f"{price_str:>{MENU_PRICE_WIDTH}}"
    pct_col = f"{arrow}{sign}{row.pct:.2f}%".rjust(MENU_PCT_WIDTH)

    left = _pad_right(row.name, MENU_ITEM_WIDTH - MENU_PRICE_WIDTH - MENU_PCT_WIDTH)
    return f"{left}{price_col}  {pct_col}"


# ============================================================================
# 系统主题检测 + 配色方案
# ============================================================================
_THEME_LIGHT = "light"
_THEME_DARK = "dark"

_PALETTE = {
    _THEME_LIGHT: {
        "bg":          "#F0F0F0",
        "fg":          "#1A1A1A",
        "header_bg":   "#E4E4E4",
        "header_fg":   "#333333",
        "hover":       "#D8D8D8",
        "separator":   "#D0D0D0",
        "up":          "#C62828",
        "down":        "#2E7D32",
        "flat":        "#757575",
        "error":       "#C62828",
    },
    _THEME_DARK: {
        "bg":          "#202020",
        "fg":          "#E8E8E8",
        "header_bg":   "#2A2A2A",
        "header_fg":   "#CCCCCC",
        "hover":       "#383838",
        "separator":   "#404040",
        "up":          "#EF5350",
        "down":        "#66BB6A",
        "flat":        "#9E9E9E",
        "error":       "#EF5350",
    },
}


def _detect_theme():
    """读取 Windows 注册表判断系统主题（深色/浅色）。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return _THEME_DARK if val == 0 else _THEME_LIGHT
    except OSError:
        return _THEME_LIGHT


# ============================================================================
# 自定义行情弹窗菜单
# ============================================================================
class StockMenuWindow:
    """自定义行情弹窗：跟随系统深色/浅色主题，点击顶部刷新行情。

    替代 pystray 原生菜单，使用 tkinter 顶层窗口实现，
    支持主题自适应、小数点对齐、点击交互。
    """

    _FONT_FAMILY = "Microsoft YaHei UI"
    _HEADER_SIZE = 10
    _ITEM_SIZE = 9
    _FOOTER_SIZE = 9
    _PAD_X = 14
    _PAD_Y = 8

    def __init__(self, root, fetcher, config, icon):
        self._root = root
        self._fetcher = fetcher
        self._config = config
        self._icon = icon

        self._theme = _detect_theme()
        self._colors = _PALETTE[self._theme]
        debug_log(f"弹窗主题: {self._theme}")

        self._win = tk.Toplevel(root)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.configure(bg=self._colors["bg"])

        self._build()
        self._position()

        # 失去焦点时自动关闭
        self._win.bind("<FocusOut>", self._on_focus_out)
        self._win.bind("<Escape>", lambda e: self._destroy())

    # ---- 构建界面 ----

    def _build(self):
        c = self._colors
        rows, error = self._fetcher.get_for_menu(self._config.load)

        # 顶部标题行（可点击刷新）
        self._build_header()
        self._add_separator()

        if error:
            self._add_text(f"  ⚠ 行情获取失败：{error}", c["error"])
        elif rows is None:
            self._add_text("  行情加载中…", c["flat"])
        else:
            if not rows:
                self._add_text("  （暂无自选股）", c["flat"])
            else:
                for row in rows:
                    self._add_stock_row(row)

        self._add_separator()
        self._build_footer()

    def _build_header(self):
        c = self._colors
        cache = self._fetcher.cache
        update_time = cache["time"]
        is_trading = cache["trading"]
        time_str = (time.strftime("%H:%M:%S", time.localtime(update_time))
                    if update_time else "--:--:--")
        label = "收盘" if not is_trading else "行情"

        header = tk.Frame(self._win, bg=c["header_bg"], cursor="hand2")
        header.pack(fill="x", padx=1, pady=(1, 0))

        tk.Label(
            header,
            text=f"  📈 自选股票{label}",
            bg=c["header_bg"], fg=c["header_fg"],
            font=(self._FONT_FAMILY, self._HEADER_SIZE),
            anchor="w",
        ).pack(side="left", padx=self._PAD_X, pady=self._PAD_Y)

        tk.Label(
            header,
            text=f"更新 {time_str}  🔄  ",
            bg=c["header_bg"], fg=c["header_fg"],
            font=(self._FONT_FAMILY, self._HEADER_SIZE - 1),
            anchor="e",
        ).pack(side="right", padx=self._PAD_X, pady=self._PAD_Y)

        header.bind("<Button-1>", lambda e: self._do_refresh())
        for child in header.winfo_children():
            child.bind("<Button-1>", lambda e: self._do_refresh())

        # hover 效果
        def _hover_on(e):
            header.configure(bg=c["hover"])
            for w in header.winfo_children():
                w.configure(bg=c["hover"])

        def _hover_off(e):
            header.configure(bg=c["header_bg"])
            for w in header.winfo_children():
                w.configure(bg=c["header_bg"])

        header.bind("<Enter>", _hover_on)
        header.bind("<Leave>", _hover_off)
        for child in header.winfo_children():
            child.bind("<Enter>", _hover_on)
            child.bind("<Leave>", _hover_off)

    def _build_footer(self):
        c = self._colors
        self._add_clickable("  ⚙ 修改自选股", c["fg"], self._do_edit)
        self._add_clickable("  ✕ 退出", c["fg"], self._do_exit)

    def _add_stock_row(self, row: QuoteRow):
        c = self._colors

        if not row.ok:
            self._add_text(f"  {row.name}   ❌ {row.error[:60]}", c["error"])
            return

        arrow = "▲" if row.pct > 0 else ("▼" if row.pct < 0 else "—")
        sign = "+" if row.pct > 0 else ""
        pct_color = (c["up"] if row.pct > 0
                     else c["down"] if row.pct < 0 else c["flat"])
        price_str = (f"{row.price:.2f}"
                     if isinstance(row.price, (int, float)) else "-")

        frame = tk.Frame(self._win, bg=c["bg"])
        frame.pack(fill="x", padx=1)

        # 名称（左对齐）
        tk.Label(
            frame, text=row.name,
            bg=c["bg"], fg=c["fg"],
            font=(self._FONT_FAMILY, self._ITEM_SIZE),
            anchor="w",
        ).pack(side="left", padx=(self._PAD_X, 4), pady=3)

        # 涨跌幅（右对齐，带颜色）
        tk.Label(
            frame, text=f"{arrow}{sign}{row.pct:.2f}%",
            bg=c["bg"], fg=pct_color,
            font=(self._FONT_FAMILY, self._ITEM_SIZE),
            anchor="e", width=8,
        ).pack(side="right", padx=(2, self._PAD_X), pady=3)

        # 价格（右对齐，小数点对齐）
        tk.Label(
            frame, text=price_str,
            bg=c["bg"], fg=c["fg"],
            font=(self._FONT_FAMILY, self._ITEM_SIZE),
            anchor="e", width=8,
        ).pack(side="right", pady=3)

        # hover 效果
        def _hover_on(e):
            frame.configure(bg=c["hover"])
            for w in frame.winfo_children():
                w.configure(bg=c["hover"])

        def _hover_off(e):
            frame.configure(bg=c["bg"])
            for w in frame.winfo_children():
                w.configure(bg=c["bg"])

        frame.bind("<Enter>", _hover_on)
        frame.bind("<Leave>", _hover_off)
        for child in frame.winfo_children():
            child.bind("<Enter>", _hover_on)
            child.bind("<Leave>", _hover_off)

    def _add_separator(self):
        sep = tk.Frame(self._win, height=1, bg=self._colors["separator"])
        sep.pack(fill="x", padx=4, pady=3)

    def _add_text(self, text, color):
        tk.Label(
            self._win, text=text,
            bg=self._colors["bg"], fg=color,
            font=(self._FONT_FAMILY, self._ITEM_SIZE),
            anchor="w",
        ).pack(fill="x", padx=self._PAD_X, pady=3)

    def _add_clickable(self, text, color, callback):
        label = tk.Label(
            self._win, text=text,
            bg=self._colors["bg"], fg=color,
            font=(self._FONT_FAMILY, self._FOOTER_SIZE),
            anchor="w", cursor="hand2",
        )
        label.pack(fill="x", padx=self._PAD_X, pady=4)
        label.bind("<Button-1>", lambda e: callback())

        def _hover_on(e):
            label.configure(bg=self._colors["hover"])

        def _hover_off(e):
            label.configure(bg=self._colors["bg"])

        label.bind("<Enter>", _hover_on)
        label.bind("<Leave>", _hover_off)

    # ---- 窗口定位 ----

    def _position(self):
        """定位在屏幕右下角（靠近系统托盘）。"""
        self._win.update_idletasks()
        w = self._win.winfo_width()
        h = self._win.winfo_height()

        screen_w = self._win.winfo_screenwidth()
        screen_h = self._win.winfo_screenheight()

        x = max(0, screen_w - w - 20)
        y = max(0, screen_h - h - 60)
        self._win.geometry(f"+{x}+{y}")

    # ---- 事件处理 ----

    def _on_focus_out(self, event):
        if event.widget == self._win:
            self._destroy()

    def _do_refresh(self):
        """点击顶部：关闭弹窗，后台刷新行情。"""
        self._destroy()

        def _refresh():
            self._fetcher.refresh(self._config.load, timeout=MENU_TIMEOUT)
        threading.Thread(target=_refresh, daemon=True).start()

    def _do_edit(self):
        self._destroy()
        self._config.open_in_editor()

    def _do_exit(self):
        self._destroy()
        self._icon.stop()
        self._root.after(0, self._root.quit)

    def _destroy(self):
        try:
            if self._win and self._win.winfo_exists():
                self._win.destroy()
        except Exception:
            pass
        self._win = None


# ============================================================================
# 托盘应用
# ============================================================================
class StockTrayApp:
    """系统托盘行情应用。

    集成 QuoteFetcher（行情编排器）+ StockConfig（配置管理），
    通过 pystray 托盘图标 + 自定义 tkinter 弹窗展示行情菜单。
    """

    def __init__(self):
        self._config = StockConfig()
        self._fetcher = QuoteFetcher()
        self._icon: Icon | None = None
        self._root: tk.Tk | None = None
        self._popup: StockMenuWindow | None = None
        self._menu_event = threading.Event()

    # ---- 托盘图标 ----

    @staticmethod
    def _build_icon():
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([4, 4, 60, 60], radius=14,
                               fill=(0, 122, 204, 255))
        draw.line([16, 46, 27, 35, 38, 41, 49, 22],
                  fill=(255, 255, 255, 255), width=4, joint="curve")
        draw.ellipse([45, 18, 53, 26], fill=(255, 255, 255, 255))
        return img

    # ---- 托盘菜单（右键） ----

    def _build_tray_menu(self):
        """构建最小化 pystray 右键菜单（触发弹窗）。"""
        return Menu(
            MenuItem("📈 自选股票", lambda: self._root.after(0, self._show_popup)),
        )

    # ---- 弹窗菜单 ----

    def _show_popup(self):
        """显示行情弹窗（若已打开则关闭）。"""
        if self._popup and self._popup._win:
            self._popup._destroy()
            self._popup = None
            return

        self._popup = StockMenuWindow(
            self._root, self._fetcher, self._config, self._icon)
        self._popup._win.focus_force()

    def _rebuild_popup(self):
        """后台刷新后重建弹窗（仅弹窗打开时生效）。"""
        if self._popup and self._popup._win:
            self._popup._destroy()
            self._popup = StockMenuWindow(
                self._root, self._fetcher, self._config, self._icon)
            self._popup._win.focus_force()

    # ---- 后台刷新 ----

    def _refresh_loop(self):
        """后台刷新循环：
          - 交易时段：每 BG_REFRESH_INTERVAL 秒刷新一次
          - 非交易时段：仅在需要时拉取一次，然后睡眠到下一个开盘时刻
        """
        time.sleep(2)  # 等待托盘图标就绪
        last_mtime = self._config.mtime()
        first_run = True

        while True:
            try:
                now = datetime.now()
                is_trading = TradingSchedule.is_open(now)
                mtime = self._config.mtime()
                config_changed = mtime != last_mtime
                last_mtime = mtime

                if (first_run or is_trading
                        or config_changed
                        or self._fetcher.needs_refresh(now)):
                    self._fetcher.refresh(self._config.load, timeout=BG_TIMEOUT)
                    self._rebuild_popup()
                first_run = False

                if is_trading:
                    self._menu_event.wait(BG_REFRESH_INTERVAL)
                    self._menu_event.clear()
                else:
                    wait = TradingSchedule.seconds_until_next(now)
                    if wait > 0:
                        debug_log(f"非交易时段，等待 {wait:.0f}s 到下一个开盘时刻")
                        self._menu_event.wait(wait)
                        self._menu_event.clear()
            except Exception:
                self._menu_event.wait(BG_REFRESH_INTERVAL)
                self._menu_event.clear()

    # ---- 入口 ----

    def run(self):
        """启动托盘应用。"""
        self._root = tk.Tk()
        self._root.withdraw()
        debug_log(f"=== StockTray 启动 build={BUILD} ===")

        # 托盘图标：左键/右键均弹出自定义行情弹窗
        self._icon = Icon(
            APP_NAME, self._build_icon(), APP_TITLE,
            menu=self._build_tray_menu(),
            action=self._on_icon_click)
        threading.Thread(target=self._icon.run, daemon=True).start()
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        self._root.mainloop()

    def _on_icon_click(self, icon):
        """托盘图标点击：弹出/关闭行情菜单。"""
        self._root.after(0, self._show_popup)


# ============================================================================
# 入口
# ============================================================================
if __name__ == "__main__":
    StockTrayApp().run()
