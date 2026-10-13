# -*- mode: python ; coding: utf-8 -*-
"""Octopus 桌面版 PyInstaller 打包配置（onedir）。

  cd <仓库根目录>
  pyinstaller octopus.spec --noconfirm

产物: dist\\Octopus\\Octopus.exe（连同 _internal/ 整个文件夹分发）。
默认 local 模式：agent、出图、出视频都在本机调度。
"""
import os
import sys as _sys

from PyInstaller.utils.hooks import collect_submodules, collect_all

REPO = SPECPATH  # PyInstaller 注入：spec 文件所在目录（即仓库根）
# 护城河：正常构建用真 backend；发行构建由 desktop/build_desktop.py 把 staging（agent 已剥成壳）
# 路径塞进 OCTOPUS_BUILD_BACKEND，于是打包进去的是壳、不含任何 prompt。
BACKEND = os.environ.get('OCTOPUS_BUILD_BACKEND') or (REPO + r'\backend')
# collect_submodules('app') 在 spec 求值期按当前 sys.path 找 app，必须先把目标 backend 顶到最前
if BACKEND in _sys.path:
    _sys.path.remove(BACKEND)
_sys.path.insert(0, BACKEND)
# ffmpeg.exe / ffprobe.exe 所在目录：环境变量 OCTOPUS_FFMPEG_DIR 优先，否则取 PATH 里的 ffmpeg
import shutil as _shutil
FFDIR = os.environ.get('OCTOPUS_FFMPEG_DIR') or os.path.dirname(_shutil.which('ffmpeg') or '')
print(f'[octopus.spec] BACKEND = {BACKEND}')

# ── 隐藏 import：后端大量按名字动态 import（agents / providers / modules） ──
hiddenimports = []
hiddenimports += collect_submodules('app')        # backend/app/** 全量
hiddenimports += collect_submodules('uvicorn')
hiddenimports += collect_submodules('websockets')
hiddenimports += [
    'aiosqlite', 'crcmod', 'crcmod._crcfunext',
    'multipart', 'email_validator',
    'anyio', 'sniffio', 'httpx', 'httpcore',
]

# ── 数据文件 ──
datas = [
    (REPO + r'\frontend\dist', 'frontend_dist'),   # 前端构建产物 → <bundle>/frontend_dist
    (FFDIR + r'\ffmpeg.exe', 'ffmpeg'),            # → <bundle>/ffmpeg/ffmpeg.exe（launcher 塞 PATH）
    (FFDIR + r'\ffprobe.exe', 'ffmpeg'),
    (REPO + r'\comfyui-workflow-multiple-angles.json', '.'),  # Qwen 转视角工作流 → bundle 根（resolve_workflow_path 读）
    (REPO + r'\docs\ComfyUI转视角_安装指南.md', 'docs'),      # 用户可见的进阶功能安装教程（设置页 hint 指向它）
]
binaries = []

# oss2 有数据/二进制依赖（crcmod 等），整包收集
for pkg in ('oss2', 'cryptography'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [REPO + r'\desktop\launcher.py'],
    pathex=[BACKEND, REPO],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # cloud/ 是服务器侧，客户端不需要；其余是明显无关的大包
    excludes=['cloud', 'tkinter', 'matplotlib', 'notebook', 'pandas', 'scipy', 'pytest', 'IPython'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Octopus',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # 无控制台窗口（小白友好）；日志写 %LOCALAPPDATA%\Octopus\logs
    disable_windowed_traceback=False,
    icon=REPO + r'\desktop\octopus.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Octopus',
)
