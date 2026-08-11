# 自选股票托盘小工具 (StockTray)

一个只常驻 Windows 右下角系统托盘的轻量工具。运行后看不到主窗口，只有托盘图标；
**右键点击图标** 弹出菜单，行情信息**直接显示在菜单里**（不额外弹窗）：

1. **行情直接显示** —— 菜单顶部直接列出每只自选股的最新价与涨跌幅（▲涨/▼跌，A 股红涨绿跌配色），
   并标注更新时间；点「🔄 刷新行情」可立即更新（后台也会每 30 秒自动刷新）。
2. **修改自选股票配置文件** —— 弹出编辑窗口，每行一个股票代码，保存即生效；
   也可一键「用记事本打开」原始 CSV 直接改。
3. **退出** —— 退出程序，托盘图标消失。

> 股票名称**无需手动配置**：程序运行时会自动从行情接口获取并显示。

## 行情数据
来自**东方财富公开行情接口**（push2.eastmoney.com），运行时联网获取，无需任何账号 / API Key。
仅在菜单弹出或后台定时刷新时才会发起网络请求。

## 配置文件（CSV，仅股票代码）
位置：`%APPDATA%\StockTray\stocks.csv`（首次运行自动生成默认 4 只）。
格式：**每行一个股票代码**，可加 `#` 注释，空行忽略；股票名称由程序自动获取。
```
sh600519
sz000001
# 下面是深市
300750
```
代码写法（自动识别市场）：
- `sh600519` / `SH600519` → 上交所
- `sz000001` / `SZ000001` → 深交所
- `600519`（以 6/9 开头）→ 上交所；`000001` / `300750`（以 0/3 开头）→ 深交所
- 北交所 `8xxxxx` / `4xxxxx` 也支持
- 也可直接写东方财富 secid，如 `1.600519`

## 运行方式
### 方式一：直接跑脚本（需 Python 3.11+ 且带 tkinter）
```bat
python stock_tray.py
```
### 方式二：运行打包好的 exe（推荐，无控制台窗口）
双击 `dist/StockTray.exe` 即可，后台仅托盘图标常驻。
> 若托盘不显示图标，请确认未被系统「隐藏图标」收起（点开托盘箭头查看）。

## 重新打包
```bat
.venv\Scripts\pyinstaller --noconsole --onefile --name StockTray ^
  --hidden-import=pystray._win32 --hidden-import=pythoncom ^
  --hidden-import=win32gui --hidden-import=win32con --hidden-import=pystray ^
  stock_tray.py
```
产物在 `dist/StockTray.exe`。`--noconsole` 保证运行时不弹黑框。
（注意：受管 Python 缺 tkinter，需使用自带 tkinter 的 Python 3.11 建 `.venv` 再打包。）

## 说明 / 限制
- 默认仅支持 A 股（沪 / 深 / 北交所）；港股、美股需另接数据源。
- 行情为实时快照，非逐笔推送；后台自动刷新间隔固定 30 秒（源码 `BG_REFRESH_INTERVAL`）。
- 依赖：`pystray`、`pillow`、`requests`、`pywin32`。


