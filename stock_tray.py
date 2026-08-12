# -*- coding: utf-8 -*-
"""
自选股票托盘小工具 (StockTray)
================================
常驻 Windows 系统托盘，右键菜单展示自选股行情。

行情数据由 QuoteFetcher 编排器提供（多源容灾，无需鉴权，运行时联网获取）。
配置文件位置：%APPDATA%/StockTray/stocks.csv
"""

import os
import subprocess
import threading
import time
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
# 菜单格式化
# ============================================================================
def _display_width(text):
    """估算字符串在菜单中的显示宽度（CJK 字符约 2 格，ASCII 约 1 格）。"""
    width = 0
    for char in text:
        width += 2 if ord(char) > 127 else 1
    return width


def _pad_right(text, target_width):
    """右填充空格使文本达到目标显示宽度。"""
    current = _display_width(text)
    return text + " " * max(1, target_width - current)


def _format_quote_label(row: QuoteRow):
    """格式化单只股票菜单项：名称左对齐，价格和涨跌幅分别右对齐。

    价格右对齐 → 小数点纵向对齐；涨跌幅右对齐 → 百分号纵向对齐。

    示例：贵州茅台    1800.00  +1.50%
          平安银行      52.30  +0.80%
          五粮液       158.70  -0.85%
    """
    arrow = "▲" if row.pct > 0 else ("▼" if row.pct < 0 else "—")
    sign = "+" if row.pct > 0 else ""
    price_str = f"{row.price:.2f}" if isinstance(row.price, (int, float)) else "-"

    # 价格右对齐（保证小数点纵向对齐）
    price_col = f"{price_str:>{MENU_PRICE_WIDTH}}"
    # 涨跌幅右对齐
    pct_col = f"{arrow}{sign}{row.pct:.2f}%".rjust(MENU_PCT_WIDTH)

    left = _pad_right(row.name, MENU_ITEM_WIDTH - MENU_PRICE_WIDTH - MENU_PCT_WIDTH)
    return f"{left}{price_col}  {pct_col}"


# ============================================================================
# 托盘应用
# ============================================================================
class StockTrayApp:
    """系统托盘行情应用。

    集成 QuoteFetcher（行情编排器）+ StockConfig（配置管理），
    通过 pystray 在系统托盘展示行情菜单。
    """

    def __init__(self):
        self._config = StockConfig()
        self._fetcher = QuoteFetcher()
        self._icon: Icon | None = None
        self._root: tk.Tk | None = None
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

    # ---- 菜单构建 ----

    def _build_menu(self, skip_fetch=False):
        rows, error = self._fetcher.get_for_menu(
            self._config.load, skip_fetch=skip_fetch)
        items = []

        if error:
            items.append(MenuItem(f"行情获取失败：{error}", None, enabled=False))
        elif rows is None:
            items.append(MenuItem("行情加载中…", None, enabled=False))
        else:
            cache = self._fetcher.cache
            update_time = cache["time"]
            is_trading = cache["trading"]
            time_str = (time.strftime("%H:%M:%S", time.localtime(update_time))
                        if update_time else "--:--:--")
            label = "收盘" if not is_trading else "行情"
            items.append(MenuItem(
                f"📈 自选股票{label}  (更新 {time_str})",
                None, enabled=False))
            items.append(Menu.SEPARATOR)

            if not rows:
                items.append(MenuItem(
                    "（暂无自选股，请『修改自选股』）", None, enabled=False))
            else:
                for row in rows:
                    if not row.ok:
                        text = f"{row.name}   ❌ {row.error[:60]}"
                        items.append(MenuItem(text, None, enabled=False))
                    else:
                        items.append(MenuItem(
                            _format_quote_label(row), None, enabled=False))

        items.append(Menu.SEPARATOR)
        # 仅交易时段显示刷新按钮
        if self._fetcher.cache["trading"]:
            items.append(MenuItem("🔄 刷新行情", self._on_refresh, default=True))
        items.append(MenuItem("⚙ 修改自选股", self._on_edit))
        items.append(Menu.SEPARATOR)
        items.append(MenuItem("退出", self._on_exit))
        return Menu(*items)

    def _rebuild_menu(self):
        """重建菜单（异常静默，避免后台线程出错导致崩溃）。"""
        if self._icon is None:
            return
        try:
            self._icon.menu = self._build_menu()
            self._icon.update_menu()
        except Exception:
            pass

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
                    self._rebuild_menu()
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

    # ---- 菜单回调 ----

    def _on_refresh(self, icon, item):
        """手动刷新：强制拉取最新行情。"""
        self._fetcher.refresh(self._config.load, timeout=MENU_TIMEOUT)
        self._rebuild_menu()

    def _on_edit(self, icon, item):
        self._config.open_in_editor()

    def _on_exit(self, icon, item):
        icon.stop()
        self._root.after(0, self._root.quit)

    # ---- 入口 ----

    def run(self):
        """启动托盘应用。"""
        self._root = tk.Tk()
        self._root.withdraw()
        debug_log(f"=== StockTray 启动 build={BUILD} ===")

        self._icon = Icon(
            APP_NAME, self._build_icon(), APP_TITLE,
            menu=self._build_menu(skip_fetch=True))
        threading.Thread(target=self._icon.run, daemon=True).start()
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        self._root.mainloop()


# ============================================================================
# 入口
# ============================================================================
if __name__ == "__main__":
    StockTrayApp().run()
