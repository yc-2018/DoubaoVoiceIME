# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

name = "DoubaoVoice"
datas = [('app/icon.ico', '.'), ('app/opus.dll', '.'), ('app/dll', 'dll')]
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
    # 云端引擎（豆包/千问/微信/百度/讯飞）走 TLS 与 hashlib，不能再裁掉 ssl/hashlib
    excludes=['numpy'],
    noarchive=False,
)
# SDK 的 audioeffect-mt.dll 依赖会从 dll 目录被重复收集到根目录，运行时只用 dll\ 下的副本
# sounddevice 的 hook 会把所有平台的 portaudio 都收进来，Windows 只用 64 位非 ASIO 版
def _keep_sounddevice(name):
    n = name.replace('\\', '/').lower()
    if '_sounddevice_data/portaudio-binaries/' in n:
        return n.endswith('/libportaudio64bit.dll')
    return True


def _keep_binary(name):
    return name.replace('\\', '/').lower() != 'sscronet.dll' and _keep_sounddevice(name)


a.datas = [d for d in a.datas if _keep_sounddevice(d[0])]
a.binaries = [b for b in a.binaries if _keep_binary(b[0])]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=name,
    icon='app/icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['opus.dll'],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
