# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('app/dll', 'dll'), ('app/icon.ico', '.')]
binaries = []
hiddenimports = []
tmp_ret = collect_all('sounddevice')
datas += tmp_ret[0]
binaries += [b for b in tmp_ret[1]
             if b[0].replace('\\', '/').endswith('portaudio-binaries/libportaudio64bit.dll')]
hiddenimports += tmp_ret[2]


a = Analysis(
    ['app\\voice_ime.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'hashlib', '_hashlib', 'ssl', '_ssl'],
    noarchive=False,
)
# SDK 的 audioeffect-mt.dll 依赖会从 dll 目录被重复收集到根目录，运行时只用 dll\ 下的副本
a.binaries = [b for b in a.binaries if b[0].lower() != 'sscronet.dll']
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DoubaoVoice_new',
    icon='app/icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
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
