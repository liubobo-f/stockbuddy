# -*- coding: utf-8 -*-
"""
自选股票托盘小工具 (StockTray)
================================
一个只常驻 Windows 右下角系统托盘的小应用：
  - 右键点击托盘图标，直接在弹出的菜单里显示自选股行情
  - 菜单功能：
      1) 行情直接展示在菜单中（含刷新）
      2) 修改自选股票配置文件
      3) 退出

行情数据来源：东方财富公开行情接口（无需鉴权，运行时联网获取）。
配置文件位置：%APPDATA%/StockTray/stocks.csv
  - 纯股票代码，每行一个（可加 # 注释）；股票名称由程序自动从行情接口获取。
"""

import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext

import requests
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

# ----------------------------------------------------------------------------
# 路径与配置
# ----------------------------------------------------------------------------
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "StockTray")
CONFIG_PATH = os.path.join(CONFIG_DIR, "stocks.csv")

APP_NAME = "StockTray"
APP_TITLE = "自选股票"

# 默认自选（仅代码，名称运行时自动获取）
DEFAULT_STOCKS = [
    "sh600519",
    "sz000858",
    "sh601318",
    "sz300750",
]

QUOTE_API = "https://push2.eastmoney.com/api/qt/ulist.np/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
REQUEST_TIMEOUT = 8
CACHE_MAX_AGE = 60          # 菜单内行情的最大有效秒数
BG_REFRESH_INTERVAL = 30    # 后台定时刷新间隔（秒）

# ----------------------------------------------------------------------------
# 行情缓存（后台线程刷新，菜单读取缓存以保证弹出即显）
# ----------------------------------------------------------------------------
_quote_cache = {"rows": None, "time": 0.0, "error": None}
_cache_lock = threading.Lock()


def _fetch_into_cache():
    stocks = load_config()
    if not stocks:
        with _cache_lock:
            _quote_cache.update(rows=[], time=time.time(), error="未配置自选股")
        return
    rows = fetch_quotes(stocks)
    with _cache_lock:
        _quote_cache.update(rows=rows, time=time.time(), error=None)


def get_quotes_for_menu(skip_fetch=False):
    """读取缓存；若过旧或为空则同步拉取一次（短超时）。
    skip_fetch=True 时仅返回缓存（用于启动阶段，避免阻塞图标初始化）。"""
    with _cache_lock:
        rows = _quote_cache["rows"]
        t = _quote_cache["time"]
        err = _quote_cache["error"]
        fresh = rows is not None and (time.time() - t) < CACHE_MAX_AGE
    if (not fresh) and (not skip_fetch):
        try:
            stocks = load_config()
            rows = fetch_quotes(stocks) if stocks else []
            with _cache_lock:
                _quote_cache.update(rows=rows, time=time.time(), error=None)
        except Exception as e:
            with _cache_lock:
                err = str(e)
    with _cache_lock:
        return _quote_cache["rows"], _quote_cache["error"]


def _bg_refresh_loop():
    time.sleep(2)  # 等待托盘图标窗口就绪
    while True:
        try:
            _fetch_into_cache()
            rebuild_menu()
        except Exception:
            pass
        # 平时按固定间隔刷新；配置保存后会触发事件立即刷新
        menu_rebuild_event.wait(BG_REFRESH_INTERVAL)
        menu_rebuild_event.clear()


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


# ----------------------------------------------------------------------------
# 股票代码 -> 东方财富 secid
# ----------------------------------------------------------------------------
def resolve_secid(code):
    c = code.strip().lower()
    if c.count(".") == 1:
        market, num = c.split(".", 1)
        if market in ("1", "0"):
            return f"{market}.{num}"
    if c.startswith("sh"):
        return "1." + c[2:]
    if c.startswith("sz"):
        return "0." + c[2:]
    if c.startswith("bj"):
        return "0." + c[2:]
    if c.isdigit():
        if c[0] in ("6", "9"):
            return "1." + c
        if c[0] in ("0", "3"):
            return "0." + c
        if c[0] in ("8", "4"):
            return "0." + c
    return "1." + c


# ----------------------------------------------------------------------------
# 行情抓取
# ----------------------------------------------------------------------------
def fetch_quotes(codes):
    """根据代码列表批量获取行情；股票名称自动取自接口返回的 f14 字段。"""
    if not codes:
        return []
    secids = ",".join(resolve_secid(c) for c in codes)
    params = {
        "pn": "1",
        "pz": str(len(codes)),
        "fltt": "2",
        "fields": "f12,f13,f14,f2,f3,f4,f6",
        "secids": secids,
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "_": "1",
    }
    try:
        resp = requests.get(QUOTE_API, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        diff = (payload.get("data") or {}).get("diff") or []
    except Exception as e:
        return [{"code": c, "name": c, "ok": False, "error": str(e)} for c in codes]

    by_secid = {}
    for row in diff:
        secid = f"{row.get('f13')}.{row.get('f12')}"
        by_secid[secid] = row

    result = []
    for c in codes:
        secid = resolve_secid(c)
        row = by_secid.get(secid)
        if not row:
            result.append({"code": c, "name": c, "ok": False,
                           "error": "未返回数据（代码可能无效）"})
            continue
        result.append({
            "code": c,
            "name": row.get("f14") or c,  # 名称自动获取
            "price": row.get("f2"),
            "pct": row.get("f3"),
            "amount": row.get("f4"),
            "turnover": row.get("f6"),
            "ok": True,
        })
    return result


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
# 全局托盘图标引用与刷新事件
# ----------------------------------------------------------------------------
icon = None
menu_rebuild_event = threading.Event()


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
    try:
        pct = float(r.get("pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "—")
    sign = "+" if pct > 0 else ""
    price = r.get("price")
    price_s = f"{price:.2f}" if isinstance(price, (int, float)) else "-"
    return f"{r['name']}   {price_s}   {arrow}{sign}{pct:.2f}%"


def build_menu(skip_fetch=False):
    rows, err = get_quotes_for_menu(skip_fetch=skip_fetch)
    items = []

    if err:
        items.append(MenuItem(f"行情获取失败：{err}", None, enabled=False))
    elif rows is None:
        items.append(MenuItem("行情加载中…", None, enabled=False))
    else:
        with _cache_lock:
            t = _quote_cache["time"]
        tstr = time.strftime("%H:%M:%S", time.localtime(t)) if t else "--:--:--"
        items.append(MenuItem(f"📈 自选股票行情  (更新 {tstr})", None, enabled=False))
        items.append(Menu.SEPARATOR)
        if not rows:
            items.append(MenuItem("（暂无自选股，请『修改配置』）", None, enabled=False))
        else:
            for r in rows:
                if not r.get("ok"):
                    label = f"{r['name']}   [获取失败]"
                    items.append(MenuItem(label, None, enabled=False))
                else:
                    items.append(MenuItem(_fmt_quote_label(r), None, enabled=False))

    items.append(Menu.SEPARATOR)
    items.append(MenuItem("🔄 刷新行情", on_refresh, default=True))
    items.append(MenuItem("⚙ 修改自选股票配置文件", on_edit))
    items.append(Menu.SEPARATOR)
    items.append(MenuItem("退出", on_exit))
    return Menu(*items)


# ----------------------------------------------------------------------------
# 配置编辑窗口
# ----------------------------------------------------------------------------
class ConfigWindow:
    def __init__(self, root):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.title("修改自选股票配置")
        self.win.geometry("480x420")
        self._build_ui()
        self._load()

    def _build_ui(self):
        info = tk.Label(self.win,
                        text="每行一个股票代码（可加 # 注释，空行忽略）\n"
                             "代码支持 sh600519 / sz000001 / 600519(默认沪) / 000001(默认深)\n"
                             "股票名称由程序自动获取，无需手动填写",
                        justify="left", fg="gray")
        info.pack(anchor="w", padx=10, pady=6)

        self.text = scrolledtext.ScrolledText(self.win, height=18, font=("Consolas", 11))
        self.text.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        btn = tk.Frame(self.win)
        btn.pack(fill=tk.X, padx=10, pady=8)
        tk.Button(btn, text="保存", command=self._save, width=10).pack(side=tk.LEFT)
        tk.Button(btn, text="用记事本打开", command=self._open_notepad, width=12).pack(side=tk.LEFT, padx=8)
        tk.Button(btn, text="关闭", command=self.win.destroy, width=10).pack(side=tk.RIGHT)

    def _load(self):
        codes = load_config()
        self.text.insert("1.0", "\n".join(codes))

    def _save(self):
        raw = self.text.get("1.0", "end").strip()
        codes = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            codes.append(line.split(",")[0].strip())  # 容忍 "code,..." 形式
        if not codes:
            messagebox.showwarning("提示", "至少需要保留一只自选股代码。")
            return
        try:
            save_config(codes)
            _fetch_into_cache()       # 配置变更后立即刷新缓存
            menu_rebuild_event.set()  # 通知后台线程刷新托盘菜单
            messagebox.showinfo("已保存", f"已保存 {len(codes)} 只自选股代码。\n配置已写入：\n{CONFIG_PATH}")
            self.win.destroy()
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _open_notepad(self):
        try:
            save_config(load_config())
        except Exception:
            pass
        if hasattr(os, "startfile"):
            os.startfile(CONFIG_PATH, "open")
        else:
            os.system(f'notepad "{CONFIG_PATH}"')


# ----------------------------------------------------------------------------
# 菜单回调（经 root.after 切回 Tk 主线程）
# ----------------------------------------------------------------------------
def on_refresh(icon, item):
    _fetch_into_cache()
    rebuild_menu()


def on_edit(icon, item):
    root.after(0, ConfigWindow, root)


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

    icon = Icon(APP_NAME, build_icon(), APP_TITLE, menu=build_menu(skip_fetch=True))
    threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=_bg_refresh_loop, daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    main()
