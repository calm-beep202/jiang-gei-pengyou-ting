# -*- coding: utf-8 -*-
"""讲给朋友听 - 本地 HTTP 服务"""
import datetime
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from chat import (ENV, build_system_prompt, chat_stream, display_so_far,
                  load_topics, parse_reply)
from tts import synthesize

TTS_SECRET_ID = ENV.get("TTS_SECRET_ID", "")
TTS_SECRET_KEY = ENV.get("TTS_SECRET_KEY", "")
TTS_REGION = ENV.get("TTS_REGION", "ap-guangzhou")
TTS_VOICE_TYPE = ENV.get("TTS_VOICE_TYPE", "501004")
TTS_READY = bool(TTS_SECRET_ID and TTS_SECRET_KEY)

# 访问口令：.env 里设了 ACCESS_PASS 才启用；空 = 不拦（本地自测时不用设）
ACCESS_PASS = ENV.get("ACCESS_PASS", "").strip()
AUTH_COOKIE = "jgpnt_auth"


def _auth_token():
    """口令不进 cookie，只放它的摘要"""
    return hashlib.sha256(("jgpnt:" + ACCESS_PASS).encode()).hexdigest()[:24]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(PROJECT_ROOT, "app", "static")
MEMORY_FILE = os.path.join(PROJECT_ROOT, "app", "memory.json")
HISTORY_FILE = os.path.join(PROJECT_ROOT, "app", "history.json")
PORT = 8100
TOPICS = {t["id"]: t for t in load_topics()}

CHAT_KEEP = 200   # 每个设备最多留多少条对话（防止文件无限长）


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json(path, data):
    """先写临时文件再替换——避免中途挂掉把文件写坏"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# 记忆：{ "<设备id>": { "<材料id>": {"summary": "...", "at": "2026-10-10"} } }
def load_memory(): return _load_json(MEMORY_FILE)
def save_memory(d): _save_json(MEMORY_FILE, d)


# 对话历史：{ "<设备id>": {"cur": "<材料id>", "sessions": {"<材料id>": {"messages": [...], "at": "..."}}} }
# 一段对话 = 一张材料（甲方案）；记忆仍是全局的，不分段
def load_history(): return _load_json(HISTORY_FILE)
def save_history(d): _save_json(HISTORY_FILE, d)


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not ACCESS_PASS:
            return True
        return (AUTH_COOKIE + "=" + _auth_token()) in (self.headers.get("Cookie") or "")

    def _do_login(self):
        """口令换 cookie——cookie 里只有摘要，没有明文"""
        if not ACCESS_PASS:
            self._send_json({"ok": True})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "bad request"}, 400)
            return
        if str(payload.get("pass") or "").strip() != ACCESS_PASS:
            self._send_json({"error": "口令不对"}, 403)
            return
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie",
                         "%s=%s; Path=/; Max-Age=2592000; SameSite=Lax" % (AUTH_COOKIE, _auth_token()))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
                body = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif not self._authed():
            self._send_json({"error": "需要口令"}, 401)
        elif path == "/api/topics":
            self._send_json(list(TOPICS.values()))
        elif path == "/api/memory":
            device = parse_qs(urlparse(self.path).query).get("device_id", [""])[0]
            mem = load_memory().get(device, {}) if device else {}
            # 只回还在架上的材料，防止旧记忆里留着已下架的话题
            self._send_json({k: v for k, v in mem.items() if k in TOPICS})
        elif path == "/api/history":
            device = parse_qs(urlparse(self.path).query).get("device_id", [""])[0]
            rec = load_history().get(device) if device else None
            if not isinstance(rec, dict):
                rec = {}
            sessions = rec.get("sessions")
            sessions = sessions if isinstance(sessions, dict) else {}
            # 只回还在架上的材料，防止旧记录里留着已下架的话题
            sessions = {k: v for k, v in sessions.items() if k in TOPICS}
            cur = rec.get("cur")
            self._send_json({
                "cur": cur if cur in TOPICS else None,
                "sessions": sessions,
            })
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            return self._do_login()
        if not self._authed():
            self._send_json({"error": "需要口令"}, 401)
            return
        if path == "/api/tts":
            return self._do_tts()
        if path != "/api/chat":
            self._send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            topic = TOPICS[payload["topic_id"]]
            messages = payload["messages"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
            self._send_json({"error": "bad request"}, 400)
            return

        device = str(payload.get("device_id") or "").strip()[:64]
        all_mem = load_memory()
        memory = all_mem.get(device, {}) if device else {}

        # 把记忆和"还没讲的"喂给提示词
        learned = []
        for tid, val in memory.items():
            t = TOPICS.get(tid)
            summary = val.get("summary", "") if isinstance(val, dict) else str(val)
            if t and summary:
                learned.append({"title": t.get("name") or t["title"], "summary": summary[:40]})
        remaining = [{"id": t["id"], "title": t.get("name") or t["title"]}
                     for t in TOPICS.values() if t["id"] not in memory]

        # 当前这张是不是她以前学过的
        cur_mem = memory.get(topic["id"])
        already = cur_mem.get("summary", "") if isinstance(cur_mem, dict) else None

        # 每次请求都重建 system prompt（人设 + 参考材料 + 记忆 + 候选材料）
        convo = [{"role": "system",
                  "content": build_system_prompt(topic, learned, remaining, already)}] + messages

        # ---- 流式返回（SSE）：她一边想，字一边往外蹦 ----
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        gone = False    # 用户关页面/断网后，别让"写不出去"把后面的存历史也带崩

        def sse(obj):
            nonlocal gone
            if gone:
                return
            try:
                self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                gone = True     # 安静地放弃输出，但这一轮照常跑完、历史照常存

        raw, last = "", None
        try:
            for piece in chat_stream(convo):
                raw += piece
                cur = display_so_far(raw)
                if cur != last:          # 只有能显示的部分变了才发（含标记时它会没变）
                    last = cur
                    sse({"t": cur})
        except Exception as e:
            sse({"error": str(e)[:160]})
            return

        result = parse_reply(raw)

        # 她懂了 → 记账（写失败就静默降级，不能影响对话）
        if device and result["understood"] and result["summary"]:
            all_mem.setdefault(device, {})[topic["id"]] = {
                "summary": result["summary"],
                "at": datetime.date.today().isoformat(),
            }
            try:
                save_memory(all_mem)
            except OSError:
                pass

        # 存对话历史（**只在成功之后**——失败的轮次不该污染上下文）
        if device:
            full = list(messages) + [{"role": "assistant", "content": result["text"]}]
            all_hist = load_history()
            rec = all_hist.get(device)
            rec = rec if isinstance(rec, dict) else {}
            sessions = rec.get("sessions")
            sessions = sessions if isinstance(sessions, dict) else {}
            sessions[topic["id"]] = {"messages": full[-CHAT_KEEP:],
                                     "at": datetime.date.today().isoformat()}
            all_hist[device] = {"cur": topic["id"], "sessions": sessions}
            try:
                save_history(all_hist)
            except OSError:
                pass

        sse({
            "done": True,
            "reply": result["text"],          # 最终干净正文（标记已剥）
            "understood": result["understood"],
            "summary": result["summary"],
            "next_id": result["next_id"],
        })

    def _do_tts(self):
        """按需合成语音——**前端静音时根本不会调到这里**，所以不消耗额度"""
        if not TTS_READY:
            self._send_json({"error": "没配语音服务"}, 503)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            text = str(payload.get("text") or "")
            want = payload.get("voice")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "bad request"}, 400)
            return

        # 用户挑的音色（只认 6 位数字，别的忽略，用默认）
        voice = TTS_VOICE_TYPE
        if want is not None and str(want).isdigit() and len(str(want)) == 6:
            voice = str(want)

        audio, err = synthesize(text, TTS_SECRET_ID, TTS_SECRET_KEY, voice, TTS_REGION)
        if err:
            self._send_json({"error": err[:160]}, 502)
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(audio)

    def log_message(self, fmt, *args):
        pass


def main():
    print(f"讲给朋友听 · demo 已启动：http://localhost:{PORT}")
    print("按 Ctrl+C 停止")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
