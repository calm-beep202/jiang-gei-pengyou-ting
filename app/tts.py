# -*- coding: utf-8 -*-
"""腾讯云语音合成（小问的声音）

用子账号（只有 TTS 权限）的 key，走 TC3-HMAC-SHA256 签名。
**只在用户没静音时才会被调用**——静音时前端根本不发请求，不消耗额度。
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HOST = "tts.tencentcloudapi.com"
SERVICE = "tts"
ACTION = "TextToVoice"
VERSION = "2019-08-23"
ALGO = "TC3-HMAC-SHA256"


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def synthesize(text, secret_id, secret_key, voice_type, region="ap-guangzhou"):
    """合成一段语音。成功返回 (音频字节, None)，失败返回 (None, 错误说明)"""
    text = (text or "").strip()
    if not text:
        return None, "空文本"
    # TTS 单次上限，超了截一段（我们最长也就百来字，正常够）
    if len(text) > 150:
        text = text[:150]

    ts = int(time.time())
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")

    payload = json.dumps({
        "Text": text,
        "SessionId": "jgpnt-%d" % ts,
        "VoiceType": int(voice_type),
        "Codec": "mp3",
        "SampleRate": 16000,
        "Speed": 0,          # 0 = 正常语速
        "Volume": 0,
        "PrimaryLanguage": 1,
    }, ensure_ascii=False)

    ct = "application/json; charset=utf-8"
    canonical = "\n".join([
        "POST", "/", "",
        "content-type:%s\nhost:%s\n" % (ct, HOST),
        "content-type;host",
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    ])
    scope = "%s/%s/tc3_request" % (date, SERVICE)
    to_sign = "\n".join([
        ALGO, str(ts), scope,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    ])
    k = _hmac(("TC3" + secret_key).encode("utf-8"), date)
    k = _hmac(k, SERVICE)
    k = _hmac(k, "tc3_request")
    signature = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        "https://" + HOST,
        data=payload.encode("utf-8"),
        headers={
            "Authorization": ("%s Credential=%s/%s, SignedHeaders=content-type;host, Signature=%s"
                              % (ALGO, secret_id, scope, signature)),
            "Content-Type": ct,
            "Host": HOST,
            "X-TC-Action": ACTION,
            "X-TC-Timestamp": str(ts),
            "X-TC-Version": VERSION,
            "X-TC-Region": region,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read(200).decode("utf-8", "replace"))
    except Exception as e:
        return None, str(e)[:160]

    resp = data.get("Response") or {}
    if resp.get("Error"):
        err = resp["Error"]
        return None, "%s: %s" % (err.get("Code", "?"), err.get("Message", "?"))

    audio = resp.get("Audio")
    if not audio:
        return None, "响应里没有音频"
    return base64.b64decode(audio), None
