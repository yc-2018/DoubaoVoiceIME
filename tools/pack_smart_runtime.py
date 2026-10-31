# -*- coding: utf-8 -*-
"""把本机已安装的豆包输入法运行时打包成 app/smart_runtime.zip。

用法：python tools/pack_smart_runtime.py [版本目录]
默认取 C:\\Program Files\\DoubaoIME\\versions 下版本号最大的一个。
"""
import os
import re
import sys
import zipfile

FILES = ("ImeService.exe", "ui.dll", "oime-config.dll", "audioeffect-mt.dll",
         "applogrs.dll", "doubaoime-rpc-new.dll", "parfait.dll", "sscronet.dll")
ROOT = r"C:\Program Files\DoubaoIME\versions"


def version_key(name):
    return [int(x) for x in re.findall(r"\d+", name)]


def latest_source():
    if not os.path.isdir(ROOT):
        return None, None
    names = [n for n in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, n))]
    if not names:
        return None, None
    name = max(names, key=version_key)
    return os.path.join(ROOT, name), name


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else None
    version = None
    if src:
        version = os.path.basename(os.path.normpath(src))
    else:
        src, version = latest_source()
    if not src or not os.path.isdir(src):
        print("source not found:", src)
        return 1
    app_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
    out = os.path.join(app_dir, "smart_runtime.zip")
    missing = [f for f in FILES if not os.path.isfile(os.path.join(src, f))]
    if missing:
        print("missing files:", ", ".join(missing))
        return 1
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in FILES:
            zf.write(os.path.join(src, name), name)
    with open(os.path.join(app_dir, "smart_runtime.version"), "w", encoding="ascii") as f:
        f.write(version + "\n")
    print("packed", out, os.path.getsize(out), "bytes; version", version)
    print("uncompressed", sum(os.path.getsize(os.path.join(src, f)) for f in FILES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
