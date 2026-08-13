# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['stock_tray.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')],
    hiddenimports=['pystray._win32', 'pythoncom', 'win32gui', 'win32con', 'pystray'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 未使用的 tkinter 子模块（体积较大）
        'tkinter.ttk', 'tkinter.tix', 'tkinter.scrolledtext',
        'tkinter.colorchooser', 'tkinter.dnd', 'tkinter.font',
        'tkinter.simpledialog', 'tkinter.messagebox',
        'tkinter.filedialog', 'tkinter.test',
        # 未使用的标准库大模块
        'unittest', 'test', 'pydoc', 'doctest',
        'xml', 'multiprocessing', 'concurrent.futures',
        'curses', 'lib2to3', 'ensurepip',
        'pdb', 'pickletools', 'difflib',
        # Pillow（使用静态 .ico 图标，无需运行时依赖）
        'PIL', 'PIL.Image', 'PIL.ImageDraw',
        # 未使用的第三方依赖
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'cryptography', 'OpenSSL',
    ],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='StockTray',
    icon='StockTray.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
