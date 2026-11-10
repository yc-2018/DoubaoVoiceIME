# -*- coding: utf-8 -*-
"""豆包智能文字整理：纯 HTTP 接口。"""

import json
import urllib.error
import urllib.request

URL = ("https://ime.oceancloudapi.com/api/v2/ai/text_organization"
       "?aid=685343&device_platform=windows&os=windows&channel=release")
SCENE_TEXT_ORGANIZATION = 6


def organize(text, timeout=15.0):
    body = json.dumps({
        "query": text,
        "scene": SCENE_TEXT_ORGANIZATION,
        "stream": False,
        "space_at_cn_en_nb": 0,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} {detail}") from None
    obj = json.loads(raw.decode("utf-8", "replace"))
    if obj.get("code") != 0:
        raise RuntimeError(f"{obj.get('code')} {obj.get('msg')}")
    data = obj.get("data") or {}
    if data.get("no_rewrite") is True:
        return "", True
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("empty result")
    return content, False
