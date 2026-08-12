# -*- coding: utf-8 -*-
"""
自选股票托盘小工具 (StockTray)
================================
一个只常驻 Windows 右下角系统托盘的小应用：
  - 右键点击托盘图标，直接在弹出的菜单里显示自选股行情
  - 菜单功能：
      1) 行情直接展示在菜单中（含刷新）
      2) 修改自选股（用默认文本编辑器打开配置文件）
      3) 退出

行情数据来源（多源容灾，无需鉴权，运行时联网获取）：
  - 腾讯 gtimg（主用，最稳定，收盘后同样返回收盘价与涨跌幅）
  - 新浪财经（备用）
  - 东方财富（兜底）
  任一路可用即取数，避免单点故障（例如东方财富连接被重置）。

配置文件位置：%APPDATA%/StockTray/stocks.csv
  - 纯股票代码，每行一个（可加 # 注释）；股票名称由程序自动从行情接口获取。
"""

import os
import subprocess
import threading
import time
from datetime import datetime

import tkinter as tk
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

# ----------------------------------------------------------------------------
# 路径与配置
# ----------------------------------------------------------------------------
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "StockTray")
CONFIG_PATH = os.path.join(CONFIG_DIR, "stocks.csv")

APP_NAME = "StockTray"
APP_TITLE = "自选股票"
BUILD = "20260812.2"

# 默认自选（仅代码，名称运行时自动获取）
DEFAULT_STOCKS = [
    "sh600519",
    "sz000858",
    "sh601318",
    "sz300750",
]

QUOTE_API = "https://push2.eastmoney.com/api/qt/ulist.np/get"  # 仅作兜底数据源
REQUEST_TIMEOUT = 8
CACHE_MAX_AGE = 60          # 菜单内行情的最大有效秒数
BG_REFRESH_INTERVAL = 30    # 后台定时刷新间隔（秒）

# ----------------------------------------------------------------------------
# 行情缓存（后台线程刷新，菜单读取缓存以保证弹出即显）
# ----------------------------------------------------------------------------
_quote_cache = {"rows": None, "time": 0.0, "error": None, "trading": True}
_cache_lock = threading.Lock()

DEBUG_LOG = os.path.join(CONFIG_DIR, "debug.log")


def _debug_log(msg):
    """诊断日志：追加写入 %APPDATA%/StockTray/debug.log（不影响主流程）。"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _fetch_into_cache():
    trading = is_trading_time()
    stocks = load_config()
    if not stocks:
        with _cache_lock:
            _quote_cache.update(rows=[], time=time.time(), error="未配置自选股", trading=trading)
        return
    rows = fetch_quotes(stocks)
    with _cache_lock:
        _quote_cache.update(rows=rows, time=time.time(), error=None, trading=trading)


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
            trading = is_trading_time()
            rows = fetch_quotes(stocks) if stocks else []
            with _cache_lock:
                _quote_cache.update(rows=rows, time=time.time(), error=None, trading=trading)
        except Exception as e:
            with _cache_lock:
                err = str(e)
    with _cache_lock:
        return _quote_cache["rows"], _quote_cache["error"]


def _bg_refresh_loop():
    time.sleep(2)  # 等待托盘图标窗口就绪
    last_mtime = _get_cfg_mtime()
    first = True
    while True:
        try:
            trading = is_trading_time()
            mtime = _get_cfg_mtime()
            cfg_changed = mtime != last_mtime
            last_mtime = mtime
            # 首次强制拉取；交易时段定时刷新；非交易时段仅在改配置或手动刷新时拉取
            if first or trading or cfg_changed:
                _fetch_into_cache()
                rebuild_menu()
            first = False
        except Exception:
            pass
        # 固定间隔轮询；“刷新行情”或配置变更会经事件立即唤醒
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
# 股票代码 -> 东方财富 secid（仅兜底数据源使用）
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
# 交易时段判断 / 配置文件变更检测
# ----------------------------------------------------------------------------
def is_trading_time(now=None):
    """判断当前是否为 A 股交易时段（周一~周五，含开盘前后缓冲）。
    9:15-11:35 与 13:00-15:05；节假日不在此列（无法穷举），但非交易时段
    会改为显示收盘价，不影响使用。"""
    now = now or datetime.now()
    if now.weekday() >= 5:           # 周六、周日
        return False
    hm = now.hour * 60 + now.minute
    if (9 * 60 + 15) <= hm <= (11 * 60 + 35):
        return True
    if (13 * 60) <= hm <= (15 * 60 + 5):
        return True
    return False


def _get_cfg_mtime():
    """返回配置文件的修改时间，用于检测用户是否改过自选股。"""
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0


def _to_float(v):
    """把接口返回的字符串/数字安全转 float（容忍 '12.34%' 这类带百分号的值）。"""
    if v is None:
        return 0.0
    try:
        return float(str(v).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _valid_price(v):
    """判断行情接口返回的 price 是否为有效数值（剔除 '-' / '' / None / nan）。"""
    if v is None:
        return False
    s = str(v).replace("%", "").strip()
    if s in ("-", "", "None", "nan"):
        return False
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


# ----------------------------------------------------------------------------
# 行情抓取（多数据源：腾讯 / 新浪 / 东方财富，任一可用即取数）
# ----------------------------------------------------------------------------
def _make_session():
    """带重试的会话，降低偶发网络抖动导致的失败。"""
    s = requests.Session()
    retry = Retry(total=1, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_SESSION = _make_session()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "close",
}


def _decode_gbk(resp):
    """qt.gtimg.cn / hq.sinajs.cn 返回 GBK 编码，手动设定避免中文乱码。"""
    try:
        resp.encoding = "gb18030"
    except Exception:
        pass
    return resp.text


def _norm_name(name):
    """折叠名称中的空白（腾讯接口偶有多余空格，如『五 粮 液』）。"""
    return "".join(str(name).split()) if name else ""


def _fetch_tencent(codes):
    """腾讯 gtimg 行情：单次请求批量，无需特殊头，最稳定。收盘后同样返回收盘价与涨跌幅。"""
    secs = ",".join(c.strip().lower() for c in codes)
    url = "https://qt.gtimg.cn/q=" + secs
    resp = _SESSION.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    _debug_log(f"  腾讯 HTTP={resp.status_code} len={len(resp.content)}")
    resp.raise_for_status()
    text = _decode_gbk(resp)
    out = {}
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line or "v_" not in line:
            continue
        var, _, val = line.partition("=")
        code = var.replace("v_", "").strip().lower()
        val = val.strip().strip('"').strip("'")
        if not val:
            continue
        p = val.split("~")
        if len(p) < 33:
            continue
        name = _norm_name(p[1])
        price = _to_float(p[3])
        if not _valid_price(price):
            continue
        out[code] = {
            "code": code,
            "name": name or code,
            "price": price,
            "pct": _to_float(p[32]),
            "ok": True,
        }
    if not out:
        raise RuntimeError("腾讯接口未返回任何有效数据")
    return out


def _fetch_sina(codes):
    """新浪行情：需带 Referer，单次请求批量。"""
    secs = ",".join(c.strip().lower() for c in codes)
    url = "https://hq.sinajs.cn/list=" + secs
    headers = dict(HEADERS, Referer="https://finance.sina.com.cn/")
    resp = _SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    _debug_log(f"  新浪 HTTP={resp.status_code} len={len(resp.content)}")
    resp.raise_for_status()
    text = _decode_gbk(resp)
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if "hq_str_" not in line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        code = key.replace("var", "").replace("hq_str_", "").strip().lower()
        val = val.strip().strip('"').strip("'")
        if not val:
            continue
        p = val.split(",")
        if len(p) < 4:
            continue
        name = _norm_name(p[0])
        price = _to_float(p[3])
        prev = _to_float(p[2])
        if not _valid_price(price):
            continue
        chg = price - prev if prev else 0.0
        pct = (chg / prev * 100.0) if prev else 0.0
        out[code] = {
            "code": code,
            "name": name or code,
            "price": price,
            "pct": pct,
            "ok": True,
        }
    if not out:
        raise RuntimeError("新浪接口未返回任何有效数据")
    return out


def _fetch_eastmoney(codes):
    """东方财富实时接口（兜底）：收盘后同样返回收盘价与当日涨跌幅。"""
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
    resp = _SESSION.get(QUOTE_API, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    _debug_log(f"  东方财富 HTTP={resp.status_code} len={len(resp.content)}")
    resp.raise_for_status()
    payload = resp.json()
    diff = (payload.get("data") or {}).get("diff") or []
    if not diff:
        raise RuntimeError("东方财富未返回数据")
    by_secid = {f"{r.get('f13')}.{r.get('f12')}": r for r in diff}
    out = {}
    for c in codes:
        secid = resolve_secid(c)
        row = by_secid.get(secid)
        if not row:
            continue
        name = row.get("f14") or c
        price = row.get("f2")
        if not _valid_price(price):
            continue
        out[c.strip().lower()] = {
            "code": c,
            "name": name,
            "price": price,
            "pct": _to_float(row.get("f3")),
            "ok": True,
        }
    if not out:
        raise RuntimeError("东方财富无有效行情")
    return out


def fetch_quotes(codes):
    """多数据源依次尝试，某只股票在任一可用源取到即采用，避免单点故障。"""
    if not codes:
        return []
    codes = list(dict.fromkeys(c.strip().lower() for c in codes))
    _debug_log(f"fetch_quotes 开始, codes={codes}, 本地时间={datetime.now():%H:%M:%S}")
    results = {}
    last_err = None
    sources = [
        ("腾讯", _fetch_tencent),
        ("新浪", _fetch_sina),
        ("东方财富", _fetch_eastmoney),
    ]
    for label, fn in sources:
        missing = [c for c in codes if c not in results]
        if not missing:
            break
        try:
            got = fn(missing)
            for c, r in got.items():
                results[c] = r
            _debug_log(f"  [{label}] 成功 {len(got)} 只（累计 {len(results)}/{len(codes)}）")
        except Exception as e:
            last_err = f"{label}: {e}"
            _debug_log(f"  [{label}] 失败: {e}")
    out = []
    for c in codes:
        if c in results:
            out.append(results[c])
        else:
            out.append({"code": c, "name": c, "ok": False,
                        "error": "全部数据源失败" + (f"（{last_err}）" if last_err else "")})
    for r in out:
        if not r.get("ok"):
            _debug_log(f"  最终失败: {r['code']} - {r.get('error')}")
    return out


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
    pct = r.get("pct") or 0.0
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
            trading = _quote_cache["trading"]
        tstr = time.strftime("%H:%M:%S", time.localtime(t)) if t else "--:--:--"
        kind = "收盘" if not trading else "行情"
        items.append(MenuItem(f"📈 自选股票{kind}  (更新 {tstr})", None, enabled=False))
        items.append(Menu.SEPARATOR)
        if not rows:
            items.append(MenuItem("（暂无自选股，请『修改自选股』）", None, enabled=False))
        else:
            for r in rows:
                if not r.get("ok"):
                    label = f"{r['name']}   ❌ {str(r.get('error', ''))[:60]}"
                    items.append(MenuItem(label, None, enabled=False))
                else:
                    items.append(MenuItem(_fmt_quote_label(r), None, enabled=False))

    items.append(Menu.SEPARATOR)
    items.append(MenuItem("🔄 刷新行情", on_refresh, default=True))
    items.append(MenuItem("⚙ 修改自选股", on_edit))
    items.append(Menu.SEPARATOR)
    items.append(MenuItem("退出", on_exit))
    return Menu(*items)


# ----------------------------------------------------------------------------
# 菜单回调（经 root.after 切回 Tk 主线程）
# ----------------------------------------------------------------------------
def on_refresh(icon, item):
    _fetch_into_cache()
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
    _debug_log(f"=== StockTray 启动 build={BUILD} ===")

    icon = Icon(APP_NAME, build_icon(), APP_TITLE, menu=build_menu(skip_fetch=True))
    threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=_bg_refresh_loop, daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    main()
