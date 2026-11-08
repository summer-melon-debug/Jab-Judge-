"""
boxing_phone_bridge.py — 拳击计分器的手机端桥接层
================================================
架构沿用 AccuBox phone_bridge.py / Cardiac Superphone：
    手机浏览器 (GitHub Pages 上的 index.html)
        │  wss://xxxx.ngrok-free.dev/ws
        ▼
    FastAPI + uvicorn（后台线程，和 matplotlib 主线程共存）
        ▼
    计分器（HIT / BLOCKED / MISS）+ Claude 教练 + Fish Audio

和 AccuBox 版的区别：AccuBox 只发文字，手机用系统声音朗读；
这里直接发 Fish Audio 生成的音频，手机播放的就是 Ethan 的声音。
  - 连接时一次性下发「语音包」：Hit / Blocked / Miss 三段 wav（base64）
  - 每次判定只发 {"type":"call","kind":"HIT"}，手机播语音包里的那段
  - 教练回答：先发文字，再发 Fish 生成（并放慢）的 wav
  - Fish 不可用时不发音频，手机自动退回系统朗读

依赖：pip install fastapi "uvicorn[standard]"
"""

import asyncio
import base64
import json
import threading
import time
from collections import deque

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    HAS_SERVER = True
except ImportError as e:
    print(f"[WARN] phone bridge deps missing: {e}  (pip install fastapi \"uvicorn[standard]\")")
    HAS_SERVER = False

PUSH_HZ = 12


class PhoneBridge:
    def __init__(self, snapshot_fn, announcer, coach, port=8000, mute_pc=False):
        """
        snapshot_fn() -> dict   比分 + 波形（主程序提供）
        announcer               Announcer：取语音包 wav、合成教练回答、PC 静音开关
        coach                   VoiceCoach：用它的 _ask() 问 Claude（带实时比分）
        """
        self.snapshot_fn = snapshot_fn
        self.ann = announcer
        self.coach = coach
        self.port = port
        self.clients = 0
        self.ann.pc_muted = mute_pc

        self._events = deque(maxlen=200)
        self._seq = 0
        self._lock = threading.Lock()
        self._last_snap = {"type": "state"}
        self.markers = deque(maxlen=50)      # 手机录像开始/结束标记，主线程取走写 CSV

        self.app = self._build_app() if HAS_SERVER else None
        self._server = None

    # ---------- 事件（主线程调用） ----------
    def push_event(self, kind, text="", t=None):
        """t：事件在计分器时钟上的时间（秒），手机用来给视频打标"""
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "kind": kind, "text": text, "t": t})

    def _events_after(self, seq):
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    def _snapshot(self):
        try:
            s = self.snapshot_fn()
            s["type"] = "state"
            self._last_snap = s
        except Exception:              # 主线程正在改 deque 或尚未初始化，用上一帧
            pass
        return self._last_snap

    def _voice_pack(self):
        pack = {}
        for kind in ("HIT", "BLOCKED", "MISS"):
            b = self.ann.wav_bytes(kind)
            if b:
                pack[kind] = base64.b64encode(b).decode()
        return pack

    # ---------- 教练（在 executor 线程里跑） ----------
    def _ask(self, text):
        if self.coach is None or self.coach.client is None:
            return "Coach offline — no Claude key loaded."
        try:
            return self.coach._ask(text)
        except Exception as e:
            print(f"[Phone] Claude error: {e}")
            return "Coach hit an error. Try again."

    # ---------- FastAPI ----------
    def _build_app(self):
        app = FastAPI(title="Boxing Scorer Phone Bridge")
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                           allow_headers=["*"])
        bridge = self

        @app.get("/")
        async def root():
            s = bridge._snapshot()
            return {"status": "ok", "clients": bridge.clients,
                    "hits": s.get("hits", 0), "voice": bridge.ann.mode}

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket):
            await ws.accept()
            bridge.clients += 1
            print(f"[Phone] client connected ({bridge.clients} total)")
            last_seq = bridge._seq
            loop = asyncio.get_running_loop()
            send_lock = asyncio.Lock()

            async def send(obj):
                async with send_lock:
                    await ws.send_text(json.dumps(obj))

            await send({"type": "voice_pack", "mode": bridge.ann.mode,
                        "clips": bridge._voice_pack()})

            async def pusher():
                nonlocal last_seq
                while True:
                    await send(bridge._snapshot())
                    for ev in bridge._events_after(last_seq):
                        last_seq = ev["seq"]
                        if ev["kind"] in ("HIT", "BLOCKED", "MISS"):
                            await send({"type": "call", "kind": ev["kind"], "t": ev["t"]})
                        elif ev["kind"] == "coach_pc":
                            await send({"type": "coach_pc", "text": ev["text"]})
                    await asyncio.sleep(1.0 / PUSH_HZ)

            async def receiver():
                while True:
                    msg = json.loads(await ws.receive_text())
                    kind = msg.get("type")
                    if kind == "speech":
                        text = (msg.get("text") or "").strip()
                        if not text:
                            continue
                        print(f"[Phone] you: {text!r}")
                        reply = await loop.run_in_executor(None, bridge._ask, text)
                        print(f"[Phone] coach: {reply}")
                        has_audio = bridge.ann.mode == "fish"
                        await send({"type": "ai_reply", "user": text, "text": reply,
                                    "audio_pending": has_audio})
                        if has_audio:
                            wav = await loop.run_in_executor(None, bridge.ann.synth_wav, reply)
                            await send({"type": "ai_audio",
                                        "audio": base64.b64encode(wav).decode() if wav else None,
                                        "text": reply})
                    elif kind == "mute_pc":
                        bridge.ann.pc_muted = bool(msg.get("value"))
                        print(f"[Phone] PC audio muted = {bridge.ann.pc_muted}")
                    elif kind == "video":
                        bridge.markers.append({"state": msg.get("state", "?"), "rx_t": msg.get("rx_t"),
                                               "file": msg.get("file", "")})
                    elif kind == "ping":
                        await send({"type": "pong"})

            try:
                await asyncio.gather(pusher(), receiver())
            except (WebSocketDisconnect, RuntimeError):
                pass
            except Exception as e:
                print(f"[Phone] ws closed: {e}")
            finally:
                bridge.clients = max(0, bridge.clients - 1)
                print("[Phone] client disconnected")

        return app

    def start(self):
        if not HAS_SERVER:
            print("[Phone] server disabled (missing fastapi/uvicorn)")
            return
        cfg = uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning")
        self._server = uvicorn.Server(cfg)
        threading.Thread(target=self._server.run, daemon=True).start()
        print(f"[Phone] bridge listening on 0.0.0.0:{self.port}  →  ngrok http {self.port}, "
              f"then use wss://<ngrok-host>/ws on the phone")

    def stop(self):
        if self._server:
            self._server.should_exit = True
