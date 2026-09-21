# -*- mode: python ; coding: utf-8 -*-
"""Empacotamento do ProjectCommit num unico .exe.

    pyinstaller build.spec --noconfirm

A pasta web/ vai como dado (o frontend nao tem build step -- e' HTML/CSS/JS
puro), e core/api.py a encontra via sys._MEIPASS.

O motor da janela e' o WebView2 do proprio Windows: nao ha' Chromium embutido,
por isso o executavel fica na casa das dezenas de MB e nao das centenas.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = [
    # pywebview carrega o backend por nome em tempo de execucao; sem isto o
    # PyInstaller nao enxerga a dependencia e o .exe abre sem janela.
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
    "clr",
]
hidden += collect_submodules("webview.platforms")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[("web", "web")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Peso morto que o Flask/Jinja as vezes arrasta junto.
        "tkinter", "matplotlib", "numpy", "pandas", "scipy",
        "PyQt5", "PySide6", "PIL.ImageQt", "test", "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ProjectCommit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # sem janela de console atras da interface
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join("web", "icone.ico") if os.path.isfile(os.path.join("web", "icone.ico")) else None,
    version=None,
)
