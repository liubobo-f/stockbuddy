# -*- coding: utf-8 -*-
"""
自选股票托盘小工具 (StockTray)
================================
一个只常驻 Windows 右下角系统托盘的小应用：
  - 右键点击托盘图标，直接在弹出的菜单里显示自选股行情
  - 菜单功能：
      1) 行情直接展示在菜单中（交易时段含刷新按钮）
      2) 修改自选股（用默认文本编辑器打开配置文件）
      3) 退出

行情数据由 QuoteFetcher 编排器提供（多源容灾，无需鉴权，运行时联网获取）。

配置文件位置：%APPDATA%/StockTray/stocks.csv
  - 纯股票代码，每行一个（可加 # 注释）；股票名称由程序自动从行情接口获取。
"""

import os
import subprocess
import threading
import time
from datetime import datetime

import tkinter as tk
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from quote_fetcher import (
    QuoteFetcher, TradingSchedule, QuoteRow,
    debug_log, BG_TIMEOUT, MENU_TIMEOUT,
)

# ----------------------------------------------------------------------------
# 路径与配置
# ----------------------------------------------------------------------------
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "StockTray")
CONFIG_PATH = os.path.join(CONFIG_DIR, "stocks.csv")

APP_NAME = "StockTray"
APP_TITLE = "自选股票"
BUILD = "20260812.4"

# 默认自选（仅代码，名称运行时自动获取）
DEFAULT_STOCKS = [
    "sh600519",
    "sz000858",
    "sh601318",
    "sz300750",
]

BG_REFRESH_INTERVAL = 30    # 交易时段后台定时刷新间隔（秒）

# ----------------------------------------------------------------------------
# 行情编排器（单例，管理数据源、缓存、并发）
# ----------------------------------------------------------------------------
fetcher = QuoteFetcher()    # 默认源：腾讯 → 新浪 → 东方财富

# ----------------------------------------------------------------------------
# 配置读写
# ----------------------------------------------------------------------------
def ensure_config_dir():
    if not os.path.isdir(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)


def load_config():
    """读取自选股代码列表（CSV，每行一个代码）。缺失时写入默认列表。"""
    ensure_config_dir()
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_STOCKS)
        return list(DEFAULT_STOCKS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            codes = []
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                codes.append(line.split(",")[0].strip())  # 容忍 "code,..." 这类 CSV
        return codes
    except Exception:
        return list(DEFAULT_STOCKS)


def save_config(codes):
    """把代码列表写入 CSV（每行一个代码）。"""
    ensure_config_dir()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(codes) + "\n")


def _get_cfg_mtime():
    """返回配置文件的修改时间，用于检测用户是否改过自选股。"""
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0


# ----------------------------------------------------------------------------
# 后台刷新循环
# ----------------------------------------------------------------------------
menu_rebuild_event = threading.Event()


def _bg_refresh_loop():
    """后台刷新循环：
      - 交易时段：每 BG_REFRESH_INTERVAL 秒刷新一次
      - 非交易时段：仅在需要时拉取一次（上次刷新早于今日开盘），
        然后睡眠到下一个交易时段开始
    """
    time.sleep(2)  # 等待托盘图标窗口就绪
    last_mtime = _get_cfg_mtime()
    first = True
    while True:
        try:
            now = datetime.now()
            trading = TradingSchedule.is_open(now)
            mtime = _get_cfg_mtime()
            cfg_changed = mtime != last_mtime
            last_mtime = mtime

            # 首次启动、交易时段、配置变更、或非交易时段但尚未拿到今日数据 → 刷新
            if first or trading or cfg_changed or fetcher.needs_refresh(now):
                fetcher.refresh(load_config, timeout=BG_TIMEOUT)
                rebuild_menu()
            first = False

            if trading:
                # 交易时段：固定间隔轮询；"刷新行情"或配置变更会经事件立即唤醒
                menu_rebuild_event.wait(BG_REFRESH_INTERVAL)
                menu_rebuild_event.clear()
            else:
                # 非交易时段：睡眠到下一个开盘时刻，不再空转
                wait = TradingSchedule.seconds_until_next(now)
                if wait > 0:
                    debug_log(f"非交易时段，等待 {wait:.0f}s 到下一个开盘时刻")
                    menu_rebuild_event.wait(wait)
                    menu_rebuild_event.clear()
        except Exception:
            menu_rebuild_event.wait(BG_REFRESH_INTERVAL)
            menu_rebuild_event.clear()


# ----------------------------------------------------------------------------
# 托盘图标
# ----------------------------------------------------------------------------
def build_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(0, 122, 204, 255))
    d.line([16, 46, 27, 35, 38, 41, 49, 22], fill=(255, 255, 255, 255), width=4, joint="curve")
    d.ellipse([45, 18, 53, 26], fill=(255, 255, 255, 255))
    return img


# ----------------------------------------------------------------------------
# 全局托盘图标引用
# ----------------------------------------------------------------------------
icon = None


def rebuild_menu():
    """重建菜单实例并刷新系统托盘菜单（异常静默，避免后台线程出错崩程序）。"""
    if icon is None:
        return
    try:
        icon.menu = build_menu()
        icon.update_menu()
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 菜单构建（动态：每次右键弹出时按缓存生成）
# ----------------------------------------------------------------------------
def _fmt_quote_label(r):
    """格式化单只股票菜单项（r 为 QuoteRow 或 dict）。"""
    if isinstance(r, QuoteRow):
        pct, price, name = r.pct, r.price, r.name
    else:
        pct = r.get("pct") or 0.0
        price = r.get("price")
        name = r["name"]
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "—")
    sign = "+" if pct > 0 else ""
    price_s = f"{price:.2f}" if isinstance(price, (int, float)) else "-"
    return f"{name}   {price_s}   {arrow}{sign}{pct:.2f}%"


def _row_failed(r):
    """判断行情行是否失败。"""
    if isinstance(r, QuoteRow):
        return not r.ok
    return not r.get("ok")


def _row_error(r):
    if isinstance(r, QuoteRow):
        return r.error
    return r.get("error", "")


def _row_name(r):
    if isinstance(r, QuoteRow):
        return r.name
    return r["name"]


def build_menu(skip_fetch=False):
    rows, err = fetcher.get_for_menu(load_config, skip_fetch=skip_fetch)
    items = []

    if err:
        items.append(MenuItem(f"行情获取失败：{err}", None, enabled=False))
    elif rows is None:
        items.append(MenuItem("行情加载中…", None, enabled=False))
    else:
        cache = fetcher.cache
        t = cache["time"]
        trading = cache["trading"]
        tstr = time.strftime("%H:%M:%S", time.localtime(t)) if t else "--:--:--"
        kind = "收盘" if not trading else "行情"
        items.append(MenuItem(f"📈 自选股票{kind}  (更新 {tstr})", None, enabled=False))
        items.append(Menu.SEPARATOR)
        if not rows:
            items.append(MenuItem("（暂无自选股，请『修改自选股』）", None, enabled=False))
        else:
            for r in rows:
                if _row_failed(r):
                    label = f"{_row_name(r)}   ❌ {str(_row_error(r))[:60]}"
                    items.append(MenuItem(label, None, enabled=False))
                else:
                    items.append(MenuItem(_fmt_quote_label(r), None, enabled=False))

    items.append(Menu.SEPARATOR)
    # 仅交易时段显示刷新按钮
    trading = fetcher.cache["trading"]
    if trading:
        items.append(MenuItem("🔄 刷新行情", on_refresh, default=True))
    items.append(MenuItem("⚙ 修改自选股", on_edit))
    items.append(Menu.SEPARATOR)
    items.append(MenuItem("退出", on_exit))
    return Menu(*items)


# ----------------------------------------------------------------------------
# 菜单回调
# ----------------------------------------------------------------------------
def on_refresh(icon, item):
    """手动刷新：强制拉取最新行情。"""
    fetcher.refresh(load_config, timeout=MENU_TIMEOUT)
    rebuild_menu()


def on_edit(icon, item):
    # 确保配置存在，再用记事本直接打开（不会弹 cmd 窗口）
    try:
        load_config()
    except Exception:
        pass
    path = os.path.abspath(CONFIG_PATH)
    try:
        subprocess.Popen(["notepad", path])
    except Exception:
        if hasattr(os, "startfile"):
            os.startfile(path, "open")
        else:
            os.system(f'notepad "{path}"')


def on_exit(icon, item):
    icon.stop()
    root.after(0, root.quit)


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def main():
    global root, icon
    root = tk.Tk()
    root.withdraw()
    debug_log(f"=== StockTray 启动 build={BUILD} ===")

    icon = Icon(APP_NAME, build_icon(), APP_TITLE, menu=build_menu(skip_fetch=True))
    threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=_bg_refresh_loop, daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    main()
