# StockTray

Windows 系统托盘自选股行情小工具。无主窗口，常驻右下角托盘，右键弹出菜单即显行情。

## 功能

- **行情直显**：菜单列出自选股最新价、涨跌幅（▲涨 ▼跌）
- **持仓盈亏**：CSV 可选填写持仓股数，菜单自动计算当日盈亏
  - 顶部显示今日总盈亏汇总（仅有持仓时）
  - 每行最右侧显示单只股票当日盈亏（`股数 × (现价 - 昨收)`）
- **智能刷新**：
  - 交易时段（周一至周五 9:15–11:35、13:00–15:05）每 30 秒自动刷新
  - 非交易时段显示收盘价，不空转刷新
- **主题自适应**：跟随系统深色/浅色主题
- **多源容灾**：腾讯 → 新浪 → 东方财富，依次尝试，任一可用即取数

## 使用

**双击 exe** 即可运行（推荐），或：

```bat
pip install requests Pillow pystray
python stock_tray.py
```

需要 Python 3.11+，Windows 系统。

## 配置

自选股列表：`%APPDATA%\StockTray\stocks.csv`（首次运行自动生成默认 4 只）

```
sh600519, 100     # 贵州茅台，持仓 100 股
sz000858, 200     # 五粮液，持仓 200 股
sh601318          # 中国平安，仅看行情（逗号和股数可省略）
# 注释行会被忽略
300750            # 纯数字自动识别市场
```

代码格式：`sh600519` / `sz000858`（带前缀）、纯数字 `600519` / `000001` / `300750`（按首位判定市场）、北交所 `8xxxxx` / `4xxxxx`。

## 可调参数

运行时参数集中在 [`config.py`](config.py)

## 编译 exe

```powershell
pip install pyinstaller
python -m PyInstaller StockTray.spec --clean --noconfirm
```

生成 `dist/StockTray.exe`，单文件无需 Python 环境。
