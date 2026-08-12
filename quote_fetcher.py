# -*- coding: utf-8 -*-
"""
行情数据获取模块
================
面向对象设计，支持多数据源容灾（腾讯 gtimg → 新浪财经 → 东方财富）。

扩展新数据源只需两步：
  1. 继承 QuoteSource，实现 fetch() 方法
  2. 实例化后添加到 QuoteFetcher(sources=[...]) 列表

无需鉴权，运行时联网获取。
"""

import os
import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    BG_TIMEOUT, MENU_TIMEOUT, CONFIG_DIR, CACHE_MAX_AGE,
    TENCENT_URL, SINA_URL, EASTMONEY_URL, TRADING_SESSIONS,
)

# ----------------------------------------------------------------------------
# 诊断日志
# ----------------------------------------------------------------------------
_DEBUG_LOG = os.path.join(CONFIG_DIR, "debug.log")


def debug_log(msg):
    """追加写入 %APPDATA%/StockTray/debug.log（不影响主流程）。"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# ============================================================================
# 行情数据行
# ============================================================================
@dataclass
class QuoteRow:
    """单只股票的行情快照。"""
    code: str
    name: str
    price: float = 0.0
    pct: float = 0.0
    ok: bool = True
    error: str = ""

    def to_dict(self):
        """兼容旧接口的 dict 形式（供菜单等外部消费者使用）。"""
        return {
            "code": self.code, "name": self.name,
            "price": self.price, "pct": self.pct,
            "ok": self.ok, "error": self.error,
        }

    @classmethod
    def fail(cls, code, error=""):
        return cls(code=code, name=code, ok=False, error=error)


# ============================================================================
# 数据源抽象基类
# ============================================================================
class QuoteSource(ABC):
    """行情数据源抽象基类。

    子类需实现 fetch() 方法：
        - 输入：股票代码列表 + 超时秒数
        - 输出：dict[str, QuoteRow]（code → QuoteRow）
        - 异常：网络错误或数据无效时 raise，由调用方决定 fallback

    基类提供共享的 HTTP 会话和通用工具方法，子类无需关心网络层细节。
    """

    # 共享 HTTP 基础设施（类级别，所有子类共用同一连接池）
    _session: requests.Session = None
    _headers: dict = None

    @classmethod
    def _init_http(cls):
        """懒初始化 HTTP 会话与请求头（首次使用时创建）。"""
        if cls._session is None:
            s = requests.Session()
            retry = Retry(total=1, backoff_factor=0.3,
                          status_forcelist=[500, 502, 503, 504])
            adapter = HTTPAdapter(
                max_retries=retry, pool_connections=5, pool_maxsize=10)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            cls._session = s
            cls._headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"),
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Connection": "keep-alive",
            }

    # ---- 通用工具方法 ----

    @staticmethod
    def to_float(v):
        """安全转 float（容忍 '12.34%' 这类带百分号的值）。"""
        if v is None:
            return 0.0
        try:
            return float(str(v).replace("%", "").strip())
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def valid_price(v):
        """判断价格是否为有效数值（剔除 '-' / '' / None / nan）。"""
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

    @staticmethod
    def norm_name(name):
        """折叠名称中的多余空白（部分接口偶有空格，如『五 粮 液』）。"""
        return "".join(str(name).split()) if name else ""

    @staticmethod
    def decode_gbk(resp):
        """腾讯/新浪返回 GBK 编码，手动设定避免中文乱码。"""
        try:
            resp.encoding = "gb18030"
        except Exception:
            pass
        return resp.text

    @staticmethod
    def resolve_secid(code):
        """股票代码 → 东方财富 secid（仅 EastmoneySource 使用）。"""
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

    # ---- 子类必须实现 ----

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源显示名称（如 '腾讯'）。"""

    @abstractmethod
    def fetch(self, codes: list[str], timeout: float = BG_TIMEOUT
              ) -> dict[str, QuoteRow]:
        """批量获取行情。返回 {code: QuoteRow}，整体失败时 raise。"""


# ============================================================================
# 具体数据源实现
# ============================================================================
class TencentSource(QuoteSource):
    """腾讯 gtimg 行情：单次请求批量，最稳定。收盘后同样返回收盘价与涨跌幅。"""

    URL = TENCENT_URL

    @property
    def name(self):
        return "腾讯"

    def fetch(self, codes, timeout=BG_TIMEOUT):
        self._init_http()
        secs = ",".join(c.strip().lower() for c in codes)
        resp = self._session.get(
            self.URL + secs, headers=self._headers, timeout=timeout)
        debug_log(f"  {self.name} HTTP={resp.status_code} len={len(resp.content)}")
        resp.raise_for_status()
        text = self.decode_gbk(resp)

        quotes = {}
        for line in text.split(";"):
            line = line.strip()
            if "=" not in line or "v_" not in line:
                continue
            var, _, val = line.partition("=")
            code = var.replace("v_", "").strip().lower()
            val = val.strip().strip('"').strip("'")
            if not val:
                continue
            parts = val.split("~")
            if len(parts) < 33:
                continue
            name = self.norm_name(parts[1])
            price = self.to_float(parts[3])
            if not self.valid_price(price):
                continue
            quotes[code] = QuoteRow(
                code=code, name=name or code,
                price=price, pct=self.to_float(parts[32]))
        if not quotes:
            raise RuntimeError(f"{self.name}接口未返回任何有效数据")
        return quotes


class SinaSource(QuoteSource):
    """新浪行情：需带 Referer，单次请求批量。"""

    URL = SINA_URL

    @property
    def name(self):
        return "新浪"

    def fetch(self, codes, timeout=BG_TIMEOUT):
        self._init_http()
        secs = ",".join(c.strip().lower() for c in codes)
        headers = dict(self._headers, Referer="https://finance.sina.com.cn/")
        resp = self._session.get(
            self.URL + secs, headers=headers, timeout=timeout)
        debug_log(f"  {self.name} HTTP={resp.status_code} len={len(resp.content)}")
        resp.raise_for_status()
        text = self.decode_gbk(resp)

        quotes = {}
        for line in text.splitlines():
            line = line.strip()
            if "hq_str_" not in line or "=" not in line:
                continue
            key, _, val = line.partition("=")
            code = key.replace("var", "").replace("hq_str_", "").strip().lower()
            val = val.strip().strip('"').strip("'")
            if not val:
                continue
            parts = val.split(",")
            if len(parts) < 4:
                continue
            name = self.norm_name(parts[0])
            price = self.to_float(parts[3])
            prev_close = self.to_float(parts[2])
            if not self.valid_price(price):
                continue
            chg = price - prev_close if prev_close else 0.0
            pct = (chg / prev_close * 100.0) if prev_close else 0.0
            quotes[code] = QuoteRow(
                code=code, name=name or code, price=price, pct=pct)
        if not quotes:
            raise RuntimeError(f"{self.name}接口未返回任何有效数据")
        return quotes


class EastmoneySource(QuoteSource):
    """东方财富实时接口（兜底）：收盘后同样返回收盘价与当日涨跌幅。"""

    URL = EASTMONEY_URL

    @property
    def name(self):
        return "东方财富"

    def fetch(self, codes, timeout=BG_TIMEOUT):
        self._init_http()
        secids = ",".join(self.resolve_secid(c) for c in codes)
        params = {
            "pn": "1", "pz": str(len(codes)), "fltt": "2",
            "fields": "f12,f13,f14,f2,f3,f4,f6",
            "secids": secids,
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "_": "1",
        }
        resp = self._session.get(
            self.URL, params=params, headers=self._headers, timeout=timeout)
        debug_log(f"  {self.name} HTTP={resp.status_code} len={len(resp.content)}")
        resp.raise_for_status()

        payload = resp.json()
        diff = (payload.get("data") or {}).get("diff") or []
        if not diff:
            raise RuntimeError(f"{self.name}未返回数据")
        by_secid = {f"{r.get('f13')}.{r.get('f12')}": r for r in diff}

        quotes = {}
        for c in codes:
            secid = self.resolve_secid(c)
            row = by_secid.get(secid)
            if not row:
                continue
            name = row.get("f14") or c
            price = row.get("f2")
            if not self.valid_price(price):
                continue
            quotes[c.strip().lower()] = QuoteRow(
                code=c.strip().lower(), name=name,
                price=price, pct=self.to_float(row.get("f3")))
        if not quotes:
            raise RuntimeError(f"{self.name}无有效行情")
        return quotes


# ============================================================================
# 行情编排器
# ============================================================================
class QuoteFetcher:
    """行情获取编排器：管理多数据源优先级、缓存与并发控制。

    典型用法::

        fetcher = QuoteFetcher()  # 默认：腾讯 → 新浪 → 东方财富
        rows = fetcher.fetch(["sh600519", "sz000858"])

    新增数据源::

        fetcher = QuoteFetcher(sources=[MySource(), TencentSource()])
    """

    DEFAULT_SOURCES = [TencentSource, SinaSource, EastmoneySource]

    def __init__(self, sources=None):
        self._sources: list[QuoteSource] = [
            s() if isinstance(s, type) else s
            for s in (sources or self.DEFAULT_SOURCES)
        ]
        self._lock = threading.Lock()
        self._fetching = False
        self._cache: dict = {
            "rows": None,      # list[QuoteRow] | None
            "time": 0.0,
            "error": None,
            "trading": True,
        }
        self._last_success_time = 0.0

    # ---- 公共属性 ----

    @property
    def sources(self):
        return list(self._sources)

    @property
    def cache(self):
        """返回缓存快照的只读副本。"""
        with self._lock:
            return dict(self._cache)

    @property
    def last_success_time(self):
        return self._last_success_time

    # ---- 数据源管理 ----

    def add_source(self, source: QuoteSource, priority: int = -1):
        """注册新数据源。priority 默认追加到末尾（最低优先级）。"""
        if isinstance(source, type):
            source = source()
        with self._lock:
            if priority < 0:
                self._sources.append(source)
            else:
                self._sources.insert(priority, source)

    # ---- 刷新判断 ----

    def needs_refresh(self, now=None):
        """判断当前是否需要重新拉取行情。

        规则：
          1) 从未获取过 → 需要
          2) 上次失败（error 存在）→ 需要
          3) 交易时段且缓存超 60s → 需要
          4) 非交易时段：上次成功时间早于今日最后开盘时刻 → 需要
        """
        now = now or datetime.now()
        with self._lock:
            if self._cache["rows"] is None:
                return True
            if self._cache["error"]:
                return True
            trading = self._cache["trading"]
            cache_t = self._cache["time"]
            last_success = self._last_success_time

        if trading:
            return (time.time() - cache_t) > CACHE_MAX_AGE

        last_open = TradingSchedule.today_last_open(now)
        if last_open is None:
            return False
        return last_success < last_open.timestamp()

    # ---- 核心拉取 ----

    def fetch(self, codes, timeout=BG_TIMEOUT):
        """多数据源依次尝试；整体成功即停，整体失败才 fallback。"""
        if not codes:
            return []
        codes = list(dict.fromkeys(c.strip().lower() for c in codes))
        debug_log(f"fetch 开始, codes={codes}, "
                  f"时间={datetime.now():%H:%M:%S}")

        results = {}
        last_err = None
        for source in self._sources:
            if results:
                break
            try:
                got = source.fetch(codes, timeout=timeout)
                results.update(got)
                debug_log(f"  [{source.name}] 成功 {len(got)} 只"
                          f"（累计 {len(results)}/{len(codes)}）")
            except Exception as e:
                last_err = f"{source.name}: {e}"
                debug_log(f"  [{source.name}] 整体失败，跳过: {e}")

        out = []
        for c in codes:
            if c in results:
                out.append(results[c])
            else:
                err_msg = ("全部数据源失败"
                           + (f"（{last_err}）" if last_err else ""))
                out.append(QuoteRow.fail(c, err_msg))

        for r in out:
            if not r.ok:
                debug_log(f"  最终失败: {r.code} - {r.error}")
        return out

    # ---- 缓存操作（线程安全）----

    def refresh(self, codes_fn, timeout=BG_TIMEOUT):
        """拉取行情并写入缓存（带并发保护）。

        codes_fn: 返回股票代码列表的可调用对象（如 load_config）。
        返回 True 表示本次实际执行了拉取。
        """
        with self._lock:
            if self._fetching:
                return False
            self._fetching = True
        try:
            trading = TradingSchedule.is_open()
            codes = codes_fn()
            if not codes:
                self._update_cache(
                    [], trading=trading, error="未配置自选股")
                return True
            rows = self.fetch(codes, timeout=timeout)
            self._update_cache(rows, trading=trading)
            self._last_success_time = time.time()
            return True
        except Exception as e:
            with self._lock:
                self._cache["error"] = str(e)
            return False
        finally:
            with self._lock:
                self._fetching = False

    def try_refresh(self, codes_fn, timeout=MENU_TIMEOUT):
        """非阻塞刷新：若已有线程在拉取则跳过，返回是否实际执行。"""
        return self.refresh(codes_fn, timeout=timeout)

    def get_cached(self):
        """返回缓存快照 (rows, error)。"""
        with self._lock:
            return self._cache["rows"], self._cache["error"]

    def get_for_menu(self, codes_fn, skip_fetch=False):
        """菜单专用读取：按需同步刷新后返回缓存。

        skip_fetch=True 时仅返回缓存（用于启动阶段避免阻塞）。
        竞态保护：若已有线程在刷新则直接返回旧缓存。
        """
        if not skip_fetch and self.needs_refresh():
            self.try_refresh(codes_fn, timeout=MENU_TIMEOUT)
        return self.get_cached()

    # ---- 内部方法 ----

    def _update_cache(self, rows, trading=True, error=None):
        with self._lock:
            self._cache.update(
                rows=rows, time=time.time(),
                error=error, trading=trading)


# ============================================================================
# 交易时段工具
# ============================================================================
class TradingSchedule:
    """A 股交易时段（周一~周五，含开盘前后缓冲）。

    上午 09:15 ~ 11:35，下午 13:00 ~ 15:05。
    节假日不在此列，但非交易时段显示收盘价，不影响使用。
    """

    # 每个交易日的两个时段（分钟数表示），从 config 读取
    _SESSIONS = TRADING_SESSIONS

    @classmethod
    def _open_times(cls, d: date):
        """返回交易日 d 的两个开盘时刻。"""
        return [
            datetime(d.year, d.month, d.day, m // 60, m % 60)
            for m, _ in cls._SESSIONS
        ]

    @classmethod
    def is_open(cls, now=None):
        """当前是否为交易时段。"""
        now = now or datetime.now()
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        return any(lo <= hm <= hi for lo, hi in cls._SESSIONS)

    @classmethod
    def today_last_open(cls, now=None):
        """今天最后一个开盘时刻（非交易日返回 None）。"""
        now = now or datetime.now()
        if now.weekday() >= 5:
            return None
        return cls._open_times(now.date())[-1]

    @classmethod
    def next_open(cls, now=None):
        """下一个开盘时刻（从今天起最多向前找 7 天）。"""
        now = now or datetime.now()
        for d in range(8):
            day = (now + timedelta(days=d)).date()
            if day.weekday() >= 5:
                continue
            for t in cls._open_times(day):
                if t > now:
                    return t
        return now + timedelta(days=1)

    @classmethod
    def seconds_until_next(cls, now=None):
        """距离下一个开盘时刻还有多少秒。"""
        now = now or datetime.now()
        return max(0, (cls.next_open(now) - now).total_seconds())
