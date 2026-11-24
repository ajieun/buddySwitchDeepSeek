# -*- mode: python ; coding: utf-8 -*-
"""bb（浏览器遥控终端）的打包配置。

与 BuddyBridge.spec 的区别：
- ``console=True`` —— bb 是命令行工具，需要控制台显示地址与日志
- ``datas`` 带上 ``web/`` 目录 —— 前端页面必须一起打包，否则页面 404
"""
import os

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules('buddy_bridge')
# Windows 下 PTY 依赖 pywinpty 的 C 扩展
hiddenimports += ['winpty', 'winpty.ptyprocess']

a = Analysis(
    [os.path.join(SPECPATH, 'bb.py')],
    pathex=[],
    binaries=[],
    datas=[(os.path.join(SPECPATH, 'web'), 'web')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='bb',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
