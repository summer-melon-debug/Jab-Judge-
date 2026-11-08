"""
boxing_scorer.py
----------------
读 boxing_rx 的串口输出，做「手套 + 头盔」重合判定，实时显示和记录。

判定规则（全部在 receiver 时基下，sender 已经对齐时钟）：
  MISS      加速度计检测到出拳，出拳后 MISS_WIN 内手套压电没有事件              → 打空
  HIT       手套事件 g，存在头盔事件 h，|h.onset - g.onset| <= WINDOW_MS  → 有效击打
  BLOCKED   手套事件 g，窗口内没有头盔事件                                → 防御（打在手臂/手套上）
  HEAD_ONLY 头盔事件 h，窗口内没有手套事件                                → 非计分接触（另一只手、头撞等），只记录

每个手套事件等 WINDOW + MARGIN 之后再下结论（覆盖无线到达顺序的抖动）。

语音播报（HIT / BLOCKED / MISS 各一句人声）：
  优先用 Fish Audio，启动时生成一次、缓存到 tts_cache/，之后离线直接播；
  没有 key 或没装依赖时，自动退回 Windows 自带的 SAPI 语音；两者都不可用就静音运行。
  新的播报会打断还没播完的上一句，连续出拳不会排队积压。
  依赖：pip install pygame python-dotenv pywin32（Fish Audio 直接走 HTTP，不需要额外 SDK）
  Key：在运行目录的 .env 里写
         FISH_API_KEY=你的key
         FISH_VOICE_ID=声音模型id   （可选；fish.audio/m/<这一串> 就是 id，不填用默认声音）

语音教练（仿 Cardiac Superphone 的交互）：
  按 t 或空格开始说话 → 说完自动停（静音 0.8 s）→ 语音识别 → Claude 带着实时比分回答（≤2 句）→ Fish Audio 念出来。
  录音期间暂停 Hit/Blocked/Miss 播报，避免麦克风把喇叭声当成你的话（Cardiac 的「播放时暂停识别」同理）。
  依赖：pip install anthropic sounddevice SpeechRecognition numpy
  Key：.env 里加 ANTHROPIC_API_KEY=...（可选 CLAUDE_MODEL，默认 claude-sonnet-4-6，与 Cardiac 相同）
  语音识别用 Google 免费接口（需联网、不用 key），对应 Cardiac 里 Chrome 的 Web Speech。

手机端（仿 AccuBox phone 版）：
  加 --phone 启动手机桥接（FastAPI，默认 8000 端口），再开 ngrok http 8000，
  手机打开 index.html，填 wss://<ngrok-host>/ws。手机播放的是 Fish 生成的同一个声音。
  --phone-audio：只从手机出声，电脑静音（手机 SETUP 里也能随时切换）。
  依赖：pip install fastapi "uvicorn[standard]"

Usage:
  python jab_judge.py --port COM14
  python jab_judge.py --port COM14 --window 30 --margin 60
  python jab_judge.py --port COM14 --no-voice

Keys (plot window):  r  清零比分   q  退出
Output: boxing_<ts>.csv
"""

import argparse
import array
import csv
import hashlib
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
import wave

import matplotlib.animation as animation
import matplotlib.pyplot as plt
import serial

# ============== Config ==============
ROLE_NAMES  = {1: "Glove", 2: "Helmet", 5: "Glove acc Z"}
COLORS      = {1: "#ff7f0e", 2: "#1f77b4", 5: "#9467bd"}
ACC_ROLE    = 5
ACC_V_PER_G = 0.33    # ADXL335 @3.3 V
ACC_ZERO_V  = 1.65
MISS_PRE_MS = 50.0    # 出拳前 50 ms 内的手套事件也算「打到了」
MISS_WIN_MS = 400.0   # 出拳后 400 ms 内没有手套压电事件 → MISS
LABELS      = {"0": "none", "1": "slow", "2": "guard", "3": "shake", "4": "jab_miss"}
GLOVE_ROLES = {1}
HELMET_ROLE = 2

WINDOW_MS = 30.0      # 手套与头盔 onset 差的容许范围
MARGIN_MS = 60.0      # 下结论前额外等待（无线延迟/乱序）
LOCKOUT_MS = 400.0    # 同一传感器两次事件间隔小于此值 → 视为同一拳的振荡，后一次忽略
CNT_TO_V  = 3.3 / 4095
PLOT_S    = 5.0
PLOT_MS   = 40
V_REF     = 3.3       # ADC 满量程；图上画 0 / 3.3 V 两条线，顶到线上就是饱和

# ----- 语音 -----
VOICE_TEXT     = {"HIT": "Hit!", "BLOCKED": "Blocked!", "MISS": "Miss!"}
FISH_URL       = "https://api.fish.audio/v1/tts"
FISH_MODEL     = "s2.1-pro-free"          # 免费版（与 s2.1-pro 同一模型，无延迟/可用性保证）；付费版改成 "s2.1-pro"
TTS_RATE       = 44100                    # Fish 文档示例用的采样率；wav 用 pygame.Sound 预加载，播放延迟最低
TTS_CACHE_DIR  = "tts_cache"

# ----- 语音教练 -----
CLAUDE_MODEL   = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
COACH_PROMPT   = """You are an energetic, concise boxing coach talking to a fighter during a sparring session.
Keep every reply under 2 short sentences; it is spoken aloud while they train.
You can see live scoring from sensors in the glove and the opponent's helmet:
- HIT: a punch landed on the helmet (scores)
- BLOCKED: a punch landed on the opponent's gloves or arms
- MISS: a punch was thrown but touched nothing
Only use the numbers you are given; never invent stats. If the data cannot answer, say so briefly."""
COACH_SPEED    = 1.0      # 教练语速：1.0 = 原速（不做变速处理）；想放慢可在 .env 里写 COACH_SPEED=0.85
MIC_RATE       = 16000
MIC_BLOCK_S    = 0.03
SILENCE_END_S  = 0.8      # 说完后静音这么久就停止录音
NO_SPEECH_S    = 5.0      # 按下后这么久都没开口就放弃
MAX_UTTER_S    = 8.0

line_q = queue.Queue(maxsize=50000)
stop_event = threading.Event()


# ============== Serial reader ==============
def reader(port, baud):
    try:
        with serial.Serial(port, baud, timeout=1) as ser:
            print(f"[SER] {port} open")
            ser.reset_input_buffer()
            while not stop_event.is_set():
                s = ser.readline().decode("utf-8", errors="ignore").strip()
                if s:
                    try:
                        line_q.put_nowait(s)
                    except queue.Full:
                        pass
    except serial.SerialException as e:
        print(f"[SER] {e}")
        stop_event.set()


# ============== Time stretch (WSOLA，变速不变调) ==============
def time_stretch_pcm(pcm, speed, sr):
    """speed < 1 变慢、> 1 变快，音高不变。输入输出都是 16-bit 单声道 PCM bytes。"""
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    frame = int(0.04 * sr)                  # 40 ms 帧
    hop_out = frame // 2
    hop_in = hop_out * speed
    tol = int(0.008 * sr)                   # ±8 ms 搜索最相似的拼接点
    win = np.hanning(frame).astype(np.float32)
    x = np.concatenate([x, np.zeros(frame + 2 * tol, np.float32)])
    n_frames = int((len(x) - frame - 2 * tol) / hop_in)
    y = np.zeros(n_frames * hop_out + frame, np.float32)
    wsum = np.zeros_like(y)
    prev = 0
    for k in range(n_frames):
        target = int(k * hop_in)
        if k == 0:
            pos = 0
        else:
            nat = prev + hop_out                       # 上一帧的自然延续
            ref = x[nat:nat + frame]
            lo = max(0, target - tol)
            region = x[lo:target + tol + frame]
            if len(region) < frame or len(ref) < frame:
                break
            corr = np.correlate(region, ref, mode="valid")
            pos = lo + int(np.argmax(corr))
        seg = x[pos:pos + frame]
        if len(seg) < frame:
            break
        o = k * hop_out
        y[o:o + frame] += seg * win
        wsum[o:o + frame] += win
        prev = pos
    y /= np.maximum(wsum, 1e-3)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


# ============== Voice announcer ==============
class Announcer:
    """HIT / BLOCKED 人声播报。后台初始化，不阻塞画图；没准备好之前的事件静默跳过。"""

    def __init__(self, enabled=True):
        self.mode = "off"
        self.model = FISH_MODEL
        self.sounds = {}
        self.channel = None
        self.reply_channel = None
        self.muted = False          # 教练录音期间置 True，暂停比分播报
        self.pc_muted = False       # 手机端选择「只从手机出声」时置 True
        self.paths = {}
        self.key = None
        self.voice_id = ""
        self.sapi_q = queue.Queue()
        if enabled:
            threading.Thread(target=self._setup, daemon=True).start()

    # ---- 对外接口 ----
    def say(self, kind):
        text = VOICE_TEXT.get(kind)
        if not text or self.muted or self.pc_muted:
            return
        if self.mode == "fish":
            self.channel.play(self.sounds[kind])          # 同一声道：新的一句直接打断上一句
        elif self.mode == "sapi":
            self.sapi_q.put(text)

    def stop(self):
        self.sapi_q.put(None)

    def wav_bytes(self, kind):
        """语音包：Hit/Blocked/Miss 缓存好的 wav 原文件（给手机）"""
        p = self.paths.get(kind)
        try:
            return p.read_bytes() if p else None
        except OSError:
            return None

    def synth_wav(self, text):
        """教练回答合成为 wav（Fish + 去静音 + 变速），给手机播放；失败返回 None"""
        if self.mode != "fish":
            return None
        pcm = self._fish_pcm(self.key, self.voice_id, text, model=self.model)
        if pcm is None:
            return None
        pcm = self._trim_silence(pcm)
        speed = float(os.getenv("COACH_SPEED", COACH_SPEED))
        if abs(speed - 1.0) > 0.02:
            pcm = time_stretch_pcm(pcm, speed, TTS_RATE)
        import io
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TTS_RATE)
            wf.writeframes(pcm)
        return buf.getvalue()

    def speak_text(self, text):
        """念任意一句话（教练回答）。阻塞到念完，供教练线程调用。"""
        if self.mode == "fish":
            pcm = self._fish_pcm(self.key, self.voice_id, text, model=self.model)
            if pcm is not None:
                import pygame
                pcm = self._trim_silence(pcm)
                speed = float(os.getenv("COACH_SPEED", COACH_SPEED))
                if abs(speed - 1.0) > 0.02:
                    pcm = time_stretch_pcm(pcm, speed, TTS_RATE)   # 本地变速不变调
                snd = pygame.mixer.Sound(buffer=pcm)
                self.reply_channel.play(snd)        # 独立声道：不会被 Hit/Miss 播报打断
                while self.reply_channel.get_busy():
                    time.sleep(0.05)
                return
        if self.mode in ("fish", "sapi"):
            self._speak_sapi_blocking(text)

    @staticmethod
    def _speak_sapi_blocking(text):
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            sp = win32com.client.Dispatch("SAPI.SpVoice")
            sp.Rate = 0               # SAPI 原速
            sp.Speak(text)
        except Exception as e:
            print(f"[COACH] SAPI: {e}")

    # ---- 初始化：Fish Audio → SAPI → 静音 ----
    def _setup(self):
        if self._setup_fish():
            self.mode = "fish"
            print(f"[VOICE] Fish Audio ready (voice={self.voice_id or 'default'}, model={self.model})")
            return
        if self._setup_sapi():
            print("[VOICE] using Windows SAPI")
            return
        print("[VOICE] no voice backend — running silent")

    def _setup_fish(self):
        try:
            import pygame
            from dotenv import load_dotenv
        except ImportError as e:
            print(f"[VOICE] {e}")
            return False
        load_dotenv()
        key = os.getenv("FISH_API_KEY")
        voice_id = os.getenv("FISH_VOICE_ID", "")
        self.key, self.voice_id = key, voice_id
        self.model = os.getenv("FISH_MODEL", FISH_MODEL)
        cache = Path(TTS_CACHE_DIR)
        cache.mkdir(exist_ok=True)
        paths = {}
        for kind, text in VOICE_TEXT.items():
            h = hashlib.md5(f"fish::{self.model}::{voice_id}::{text}".encode()).hexdigest()[:12]
            path = cache / f"boxing_{kind.lower()}_{h}.wav"
            if not path.exists():
                if not key:
                    print("[VOICE] FISH_API_KEY not set and no cached audio")
                    return False
                pcm = self._fish_pcm(key, voice_id, text, model=self.model)
                if pcm is None:
                    return False
                self._write_wav(path, self._trim_silence(pcm))
            paths[kind] = path
        try:
            pygame.mixer.init(frequency=TTS_RATE, size=-16, channels=1, buffer=512)
            self.sounds = {k: pygame.mixer.Sound(str(p)) for k, p in paths.items()}
            self.paths = paths
            self.channel = pygame.mixer.Channel(0)
            self.reply_channel = pygame.mixer.Channel(1)
        except Exception as e:
            print(f"[VOICE] pygame mixer: {e}")
            return False
        return True

    @staticmethod
    def _fish_pcm(key, voice_id, text, speed=1.0, model=FISH_MODEL):
        """请求 wav，再取出 16-bit 单声道 PCM（转成单声道并核对采样率）"""
        import io
        import json
        import urllib.error
        import urllib.request
        body = {"text": text, "format": "wav", "sample_rate": TTS_RATE}
        if abs(speed - 1.0) > 1e-3:
            body["prosody"] = {"speed": speed}
        if voice_id:
            body["reference_id"] = voice_id
        req = urllib.request.Request(
            FISH_URL, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "model": model})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            with wave.open(io.BytesIO(data), "rb") as wf:
                ch, sw, sr = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
                frames = wf.readframes(wf.getnframes())
            if sw != 2:
                print(f"[VOICE] Fish returned {sw * 8}-bit audio, expected 16-bit")
                return None
            if sr != TTS_RATE:
                print(f"[VOICE] Fish returned {sr} Hz (expected {TTS_RATE}); playback pitch may be off")
            if ch == 2:
                a = array.array("h"); a.frombytes(frames)
                frames = array.array("h", ((a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2))).tobytes()
            return frames
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")[:300]
            print(f"[VOICE] Fish Audio '{text}': HTTP {e.code} — {detail}")
            return None
        except Exception as e:
            print(f"[VOICE] Fish Audio '{text}': {e}")
            return None

    @staticmethod
    def _trim_silence(pcm, thresh=400):
        """去掉开头的静音，让声音紧跟判定出来"""
        a = array.array("h")
        a.frombytes(pcm[: len(pcm) // 2 * 2])
        i = next((k for k, v in enumerate(a) if abs(v) > thresh), 0)
        i = max(0, i - int(0.01 * TTS_RATE))            # 留 10 ms 起音
        return a[i:].tobytes()

    @staticmethod
    def _write_wav(path, pcm):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TTS_RATE)
            w.writeframes(pcm)

    def _setup_sapi(self):
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            return False
        self.mode = "sapi"
        threading.Thread(target=self._sapi_loop, daemon=True).start()
        return True

    def _sapi_loop(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        sp.Rate = 2
        while True:
            text = self.sapi_q.get()
            if text is None:
                break
            sp.Speak(text, 3)       # 1 = 异步, 2 = 打断上一句


# ============== Voice coach (Cardiac Superphone style) ==============
class VoiceCoach:
    """按键说话 → 识别 → Claude（带实时比分）→ 念出来。一次一轮，忙的时候忽略按键。"""

    def __init__(self, announcer, stats_fn, enabled=True):
        self.ann = announcer
        self.stats_fn = stats_fn
        self.state = "off"          # off / ready / listening / thinking / speaking
        self.last_q = self.last_a = ""
        self.on_reply = None
        self.client = None
        if enabled:
            threading.Thread(target=self._setup, daemon=True).start()

    def _setup(self):
        try:
            import anthropic
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
            import speech_recognition  # noqa: F401
            from dotenv import load_dotenv
        except ImportError as e:
            print(f"[COACH] disabled: {e}")
            return
        load_dotenv()
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("[COACH] disabled: ANTHROPIC_API_KEY not set")
            return
        self.client = anthropic.Anthropic()
        self.state = "ready"
        spd = float(os.getenv("COACH_SPEED", COACH_SPEED))
        src = ".env COACH_SPEED" if os.getenv("COACH_SPEED") else "default"
        print(f"[COACH] ready ({CLAUDE_MODEL}) — speech speed {spd:g}x ({src}) — press t or SPACE to talk")

    def trigger(self):
        if self.state != "ready":
            return
        self.state = "listening"
        threading.Thread(target=self._turn, daemon=True).start()

    # ---- 一轮对话 ----
    def _turn(self):
        try:
            self.ann.muted = True
            print("[COACH] listening...")
            pcm = self._record()
            self.ann.muted = False
            if pcm is None:
                print("[COACH] no speech heard")
                return
            self.state = "thinking"
            text = self._stt(pcm)
            if not text:
                print("[COACH] didn't catch that")
                self.state = "speaking"
                self.ann.speak_text("Sorry, I didn't catch that.")
                return
            self.last_q = text
            print(f"[COACH] you: {text}")
            reply = self._ask(text)
            self.last_a = reply
            if self.on_reply:
                self.on_reply(f"{text}  →  {reply}")
            print(f"[COACH] coach: {reply}")
            self.state = "speaking"
            self.ann.speak_text(reply)
        except Exception as e:
            print(f"[COACH] error: {e}")
        finally:
            self.ann.muted = False
            if self.state != "off":
                self.state = "ready"

    def _record(self):
        """能量门限 VAD：先测 0.3 s 环境噪声，开口后静音 SILENCE_END_S 即停。"""
        import numpy as np
        import sounddevice as sd
        blk = int(MIC_RATE * MIC_BLOCK_S)
        frames, floor, started = [], [], False
        silent = elapsed = 0.0
        with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=blk) as st:
            while True:
                data, _ = st.read(blk)
                x = data[:, 0]
                rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2)))
                elapsed += MIC_BLOCK_S
                if elapsed <= 0.3:
                    floor.append(rms)
                    continue
                thr = max(3.0 * float(np.median(floor)), 300.0)
                if not started:
                    if rms > thr:
                        started = True
                        frames.append(x.copy())
                    elif elapsed > NO_SPEECH_S:
                        return None
                    continue
                frames.append(x.copy())
                silent = silent + MIC_BLOCK_S if rms < thr else 0.0
                if silent >= SILENCE_END_S or len(frames) * MIC_BLOCK_S >= MAX_UTTER_S:
                    break
        return np.concatenate(frames).tobytes()

    @staticmethod
    def _stt(pcm):
        import speech_recognition as sr
        try:
            return sr.Recognizer().recognize_google(sr.AudioData(pcm, MIC_RATE, 2), language="en-US")
        except sr.UnknownValueError:
            return ""
        except Exception as e:
            print(f"[COACH] speech recognition: {e}")
            return ""

    def _ask(self, user_text):
        st = self.stats_fn()
        context = f"""Live session data:
- Session time: {st['minutes']:.1f} min
- Totals: {st['hits']} hits, {st['blocked']} blocked, {st['misses']} misses ({st['thrown']} punches thrown)
- Hit rate: {st['hit_rate']}
- Last 60 s: {st['hits_60']} hits, {st['blocked_60']} blocked, {st['misses_60']} misses
- Last outcomes (oldest to newest): {st['recent'] or 'none yet'}

The fighter says: "{user_text}"
"""
        msg = self.client.messages.create(model=CLAUDE_MODEL, max_tokens=150,
                                          system=COACH_PROMPT,
                                          messages=[{"role": "user", "content": context}])
        return msg.content[0].text.strip()


# ============== Clock unwrap (micros() 每 ~71.6 min 回绕) ==============
class Clock:
    def __init__(self):
        self.last_raw = None
        self.full = 0
        self.origin = None

    @staticmethod
    def _diff(a, b):
        d = (a - b) & 0xFFFFFFFF
        return d - (1 << 32) if d >= (1 << 31) else d

    def update(self, raw):
        if self.last_raw is None:
            self.full = raw
            self.origin = raw
        else:
            self.full += self._diff(raw, self.last_raw)
        self.last_raw = raw
        return self.full

    def to_full(self, raw):
        if self.last_raw is None:
            return None
        return self.full + self._diff(raw, self.last_raw)

    def sec(self, full_us):
        return (full_us - self.origin) / 1e6


# ============== Scorer ==============
class Scorer:
    def __init__(self, window_ms, margin_ms, lockout_ms=LOCKOUT_MS):
        self.win = window_ms * 1000.0
        self.margin = margin_ms * 1000.0
        self.lockout = lockout_ms * 1000.0
        self.reset()

    def reset(self):
        self.gloves, self.heads, self.punches = [], [], []
        self.glove_times = deque(maxlen=200)       # 所有手套压电事件时间，供 MISS 判定
        self.hits = self.blocked = self.head_only = self.misses = 0
        self.last_t = {}                            # 每个传感器上一次被采纳的事件时间
        self.ignored = 0

    def add(self, ev):
        """返回 False 表示该事件落在锁定期内（同一拳的振荡），已忽略"""
        role = ev["role"]
        if role in GLOVE_ROLES:
            self.glove_times.append(ev["t"])        # 接触确实发生过，MISS 判定仍要知道
        prev = self.last_t.get(role)
        if role != ACC_ROLE and prev is not None and ev["t"] - prev < self.lockout:
            self.ignored += 1
            return False
        self.last_t[role] = ev["t"]
        if role in GLOVE_ROLES:
            self.gloves.append(ev)
        elif role == HELMET_ROLE:
            self.heads.append(ev)
        elif role == ACC_ROLE:
            self.punches.append(ev)
        return True

    def tick(self, now_us):
        """返回本次新产生的判定列表"""
        out = []
        self.gloves.sort(key=lambda e: e["t"])
        keep = []
        for g in self.gloves:
            if now_us < g["t"] + self.win + self.margin:
                keep.append(g)
                continue
            cands = [h for h in self.heads
                     if not h.get("used") and abs(h["t"] - g["t"]) <= self.win]
            if cands:
                h = min(cands, key=lambda h: abs(h["t"] - g["t"]))
                h["used"] = True
                self.hits += 1
                out.append({"kind": "HIT", "glove": g, "head": h,
                            "dt_ms": (h["t"] - g["t"]) / 1000.0})
            else:
                self.blocked += 1
                out.append({"kind": "BLOCKED", "glove": g, "head": None, "dt_ms": None})
        self.gloves = keep

        keep = []
        for h in self.heads:
            # 头盔事件要等所有可能与它配对的手套事件先判完
            if now_us < h["t"] + 2 * self.win + self.margin:
                keep.append(h)
                continue
            if not h.get("used"):
                self.head_only += 1
                out.append({"kind": "HEAD_ONLY", "glove": None, "head": h, "dt_ms": None})
        self.heads = keep

        keep = []
        pre, win = MISS_PRE_MS * 1000, MISS_WIN_MS * 1000
        for a in self.punches:
            if now_us < a["t"] + win + self.margin:
                keep.append(a)
                continue
            touched = any(a["t"] - pre <= tg <= a["t"] + win for tg in self.glove_times)
            if not touched:
                self.misses += 1
                out.append({"kind": "MISS", "glove": None, "head": None, "acc": a, "dt_ms": None})
        self.punches = keep
        return out


# ============== Main / plot ==============
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--window", type=float, default=WINDOW_MS)
    ap.add_argument("--margin", type=float, default=MARGIN_MS)
    ap.add_argument("--lockout", type=float, default=LOCKOUT_MS,
                    help="同一传感器两次事件的最小间隔 ms，小于它的视为振荡并忽略")
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--no-coach", action="store_true", help="关闭语音教练")
    ap.add_argument("--phone", action="store_true", help="开启手机端桥接")
    ap.add_argument("--phone-port", type=int, default=8000)
    ap.add_argument("--phone-audio", action="store_true", help="只从手机出声，电脑静音")
    ap.add_argument("--log-wave", action="store_true",
                    help="另存全部波形到 *_wave.csv（采集阈值数据用），按 0-4 键打动作标签")
    args = ap.parse_args()
    voice = Announcer(enabled=not args.no_voice)

    csv_path = f"boxing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    fcsv = open(csv_path, "w", newline="")
    w = csv.writer(fcsv)
    w.writerow(["t_s", "kind", "role", "seq", "peak_V", "slope_cnt_per_ms",
                "n_above", "synced", "dt_ms", "glove_peak_V", "head_peak_V"])
    print(f"[LOG] {csv_path}")
    fw = ww = None
    if args.log_wave:
        wave_path = csv_path.replace(".csv", "_wave.csv")
        fw = open(wave_path, "w", newline="")
        ww = csv.writer(fw)
        ww.writerow(["t_s", "role", "value", "unit", "label"])
        print(f"[LOG] {wave_path}   keys: 0 none | 1 slow | 2 guard | 3 shake | 4 jab_miss")
    label = {"cur": "none"}

    # 教练用的统计：会话开始时间 + 最近判定（墙钟时间）
    session = {"t0": time.time()}
    recent_dec = deque(maxlen=500)

    def stats():
        now = time.time()
        last60 = [k for t, k in recent_dec if now - t <= 60]
        thrown = scorer.hits + scorer.blocked + scorer.misses
        return {
            "minutes": (now - session["t0"]) / 60,
            "hits": scorer.hits, "blocked": scorer.blocked, "misses": scorer.misses,
            "thrown": thrown,
            "hit_rate": f"{100 * scorer.hits / thrown:.0f}%" if thrown else "n/a",
            "hits_60": last60.count("HIT"), "blocked_60": last60.count("BLOCKED"),
            "misses_60": last60.count("MISS"),
            "recent": ", ".join(k for _, k in list(recent_dec)[-8:]),
        }

    coach = VoiceCoach(voice, stats, enabled=not (args.no_coach or args.no_voice))

    # ---- 手机端 ----
    def wave_tail(role, n=120, span=2.4):
        dq = list(wave.get(role, ()))
        if not dq:
            return []
        t_end = dq[-1][0]
        pts = [v for t, v in dq if t >= t_end - span]
        step = max(1, len(pts) // n)
        return [round(v, 3) for v in pts[::step]][-n:]

    def phone_snapshot():
        st = stats()
        thrown60 = st["hits_60"] + st["blocked_60"] + st["misses_60"]
        rx_now = clk.sec(clk.full + min(time.perf_counter() - t_wall["T"], 0.2) * 1e6) \
            if clk.last_raw is not None else None
        return {
            "t_rx": rx_now,                               # 计分器时钟（秒），手机用它给视频对时
            "hits": st["hits"], "blocked": st["blocked"], "misses": st["misses"],
            "thrown": st["thrown"], "hit_rate": st["hit_rate"], "per_min": thrown60,
            "elapsed": round(st["minutes"] * 60, 1), "recent": st["recent"],
            "glove": wave_tail(1), "helmet": wave_tail(HELMET_ROLE),
            "acc": wave_tail(ACC_ROLE),            # 手套 Z 轴加速度（g）
        }

    bridge = None
    if args.phone:
        from boxing_phone_bridge import PhoneBridge
        bridge = PhoneBridge(phone_snapshot, voice, coach, port=args.phone_port,
                             mute_pc=args.phone_audio)
        bridge.start()
        coach.on_reply = lambda txt: bridge.push_event("coach_pc", txt)

    threading.Thread(target=reader, args=(args.port, args.baud), daemon=True).start()

    clk = Clock()
    scorer = Scorer(args.window, args.margin, args.lockout)
    wave = {r: deque(maxlen=int(PLOT_S * 600)) for r in ROLE_NAMES}
    ev_marks = deque(maxlen=300)     # (t_s, role, peak_V)
    dec_marks = deque(maxlen=300)    # (t_s, kind, y)
    unsynced_warned = set()
    base_v = {r: None for r in ROLE_NAMES}   # 各 sender 最新基线（V），用于把 ΔV 还原成绝对电压

    for k in ("keymap.yscale", "keymap.xscale", "keymap.save", "keymap.grid"):
        plt.rcParams[k] = []
    fig, (ax_g, ax_h, ax_a) = plt.subplots(3, 1, figsize=(13, 9), sharex=True)
    lines = {}
    for r in ROLE_NAMES:
        ax = ax_h if r == HELMET_ROLE else (ax_a if r == ACC_ROLE else ax_g)
        lines[r], = ax.plot([], [], color=COLORS[r], lw=1.0, label=ROLE_NAMES[r])
    for ax, name in ((ax_g, "Glove"), (ax_h, "Helmet")):
        ax.set_ylabel(f"{name}\nADC input (V)")
        ax.set_ylim(-0.15, V_REF + 0.15)
        for y in (0.0, V_REF):
            ax.axhline(y, color="#d62728", ls="--", lw=0.8, alpha=0.6)
        ax.grid(alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)
    ax_a.set_ylabel("Glove acc Z\n(g)")
    ax_a.set_ylim(-3.5, 3.5)
    ax_a.grid(alpha=0.3)
    ax_a.legend(loc="upper right", fontsize=8)
    for y in (-3.0, 3.0):
        ax_a.axhline(y, color="#d62728", ls="--", lw=0.8, alpha=0.6)
    ax_a.set_xlabel("Time (s)  [receiver clock]")
    ev_sc_g, = ax_g.plot([], [], "o", ms=5, mfc="none", color="k")
    ev_sc_h, = ax_h.plot([], [], "o", ms=5, mfc="none", color="k")
    hit_sc, = ax_g.plot([], [], "v", ms=12, color="#2ca02c")
    blk_sc, = ax_g.plot([], [], "v", ms=12, color="#7f7f7f")
    ho_sc, = ax_h.plot([], [], "x", ms=10, color="#d62728")
    ev_sc_a, = ax_a.plot([], [], "o", ms=5, mfc="none", color="k")
    miss_sc, = ax_a.plot([], [], "s", ms=10, color="#e377c2")
    txt = fig.text(0.5, 0.975, "", ha="center", va="top", fontsize=18, fontweight="bold")
    txt_s = fig.text(0.01, 0.975, "", va="top", fontsize=8, family="monospace")
    last_dec = {"text": "", "t": 0.0}

    t_wall = {"T": time.perf_counter()}

    def handle(line):
        p = line.split(",")
        tag = p[0]
        if tag == "T" and len(p) == 2:
            clk.update(int(p[1]))
            t_wall["T"] = time.perf_counter()
        elif tag == "E" and len(p) == 9:
            t = clk.to_full(int(p[3]))
            if t is None:
                return
            role, synced = int(p[1]), int(p[8])
            if role == ACC_ROLE:
                ev = {"role": role, "seq": int(p[2]), "t": t, "synced": synced,
                      "peak": int(p[5]) * CNT_TO_V / ACC_V_PER_G}        # 动态加速度 g
                scorer.add(ev)
                ts = clk.sec(t)
                ev_marks.append((ts, role, min(ev["peak"], 3.4)))
                w.writerow([f"{ts:.4f}", "PUNCH", role, ev["seq"], f"{ev['peak']:.2f}",
                            int(p[6]), "", synced, "", "", ""])
                print(f"  punch          t={ts:8.3f}s  acc={ev['peak']:+.1f} g")
                return
            b = base_v.get(role) or 0.0
            ev = {"role": role, "seq": int(p[2]), "t": t, "peak": int(p[5]) * CNT_TO_V,
                  "peak_abs": b + int(p[5]) * CNT_TO_V,
                  "slope": int(p[6]), "n_above": int(p[7]), "synced": synced}
            if not synced and role not in unsynced_warned:
                print(f"[WARN] {ROLE_NAMES.get(role, role)} not time-synced yet — event timing unreliable")
                unsynced_warned.add(role)
            if not scorer.add(ev):
                print(f"  {ROLE_NAMES.get(role, role):8s} event  t={clk.sec(t):8.3f}s  ignored (within {args.lockout:.0f} ms lockout)")
                return
            ts = clk.sec(t)
            ev_marks.append((ts, role, min(max(ev["peak_abs"], 0.0), V_REF)))
            w.writerow([f"{ts:.4f}", "EVENT", role, ev["seq"], f"{ev['peak']:.3f}",
                        ev["slope"], ev["n_above"], synced, "", "", ""])
            print(f"  {ROLE_NAMES.get(role, role):8s} event  t={ts:8.3f}s  peak={ev['peak']:+.2f} V")
        elif tag == "W" and len(p) >= 6:
            role = int(p[1])
            t0 = clk.to_full(int(p[2]))
            if t0 is None or role not in wave:
                return
            dt = int(p[3])
            b = int(p[4]) * CNT_TO_V
            base_v[role] = b
            for i, v in enumerate(p[5:]):
                ts = clk.sec(t0 + i * dt)
                volt = b + int(v) * CNT_TO_V
                if role == ACC_ROLE:
                    val, unit = (volt - ACC_ZERO_V) / ACC_V_PER_G, "g"
                else:
                    val, unit = volt, "V"
                wave[role].append((ts, val))
                if ww:
                    ww.writerow([f"{ts:.4f}", role, f"{val:.4f}", unit, label["cur"]])
        elif tag.startswith("#"):
            print(line)

    def update(_):
        n = 0
        while n < 3000:
            try:
                line = line_q.get_nowait()
            except queue.Empty:
                break
            n += 1
            try:
                handle(line)
            except (ValueError, IndexError):
                pass

        if clk.last_raw is not None:
            # 两次 T 行之间按电脑时钟外推，判定不必等下一个 100 ms 时钟行
            now_est = clk.full + min(time.perf_counter() - t_wall["T"], 0.2) * 1e6
            for d in scorer.tick(now_est):
                g, h = d["glove"], d["head"]
                if d["kind"] == "MISS":
                    a = d["acc"]
                    ts = clk.sec(a["t"])
                    msg = f"MISS  (punch {a['peak']:+.1f} g, no contact)"
                    dec_marks.append((ts, "MISS", min(a["peak"], 3.4)))
                    print(f"[MISS] t={ts:.3f}s  {msg}")
                    voice.say("MISS")
                    recent_dec.append((time.time(), "MISS"))
                    if bridge:
                        bridge.push_event("MISS", t=ts)
                    last_dec["text"], last_dec["t"] = msg, time.time()
                    w.writerow([f"{ts:.4f}", "MISS", ACC_ROLE, a["seq"], "", "", "", "", "", "", ""])
                    continue
                ref = g if g else h
                ts = clk.sec(ref["t"])
                if d["kind"] == "HIT":
                    msg = f"HIT  ({ROLE_NAMES[g['role']]}, Δt={d['dt_ms']:+.1f} ms)"
                    dec_marks.append((ts, "HIT", min(max(g["peak_abs"], 0), V_REF)))
                elif d["kind"] == "BLOCKED":
                    msg = f"BLOCKED  ({ROLE_NAMES[g['role']]})"
                    dec_marks.append((ts, "BLOCKED", min(max(g["peak_abs"], 0), V_REF)))
                else:
                    msg = "HEAD_ONLY (not scored)"
                    dec_marks.append((ts, "HEAD_ONLY", min(max(h["peak_abs"], 0), V_REF)))
                print(f"[{d['kind']}] t={ts:.3f}s  {msg}")
                voice.say(d["kind"])
                if d["kind"] in ("HIT", "BLOCKED"):
                    recent_dec.append((time.time(), d["kind"]))
                    if bridge:
                        bridge.push_event(d["kind"], t=ts)
                last_dec["text"], last_dec["t"] = msg, time.time()
                w.writerow([f"{ts:.4f}", d["kind"], g["role"] if g else HELMET_ROLE,
                            g["seq"] if g else h["seq"], "", "", "", "",
                            f"{d['dt_ms']:.2f}" if d["dt_ms"] is not None else "",
                            f"{g['peak']:.3f}" if g else "", f"{h['peak']:.3f}" if h else ""])
            if bridge:
                while bridge.markers:                       # 手机端录像开始/结束，写进同一个 CSV 方便对齐
                    m = bridge.markers.popleft()
                    w.writerow([f"{m['rx_t']:.4f}" if m.get("rx_t") is not None else "",
                                "VIDEO_" + m["state"].upper(), "", "", "", "", "", "", "", "", m.get("file", "")])
                    print(f"[VIDEO] {m['state']}  rx_t={m.get('rx_t')}  {m.get('file', '')}")
            fcsv.flush()
            if fw:
                fw.flush()

            now_s = clk.sec(clk.full)
            x0 = now_s - PLOT_S
            for r, dq in wave.items():
                pts = [(a, b) for a, b in dq if a >= x0]
                lines[r].set_data([a for a, _ in pts], [b for _, b in pts])
            eg = [(a, c) for a, r, c in ev_marks if a >= x0 and r in GLOVE_ROLES]
            eh = [(a, c) for a, r, c in ev_marks if a >= x0 and r == HELMET_ROLE]
            ev_sc_g.set_data([a for a, _ in eg], [c for _, c in eg])
            ev_sc_h.set_data([a for a, _ in eh], [c for _, c in eh])
            ea = [(a, c) for a, r, c in ev_marks if a >= x0 and r == ACC_ROLE]
            ev_sc_a.set_data([a for a, _ in ea], [c for _, c in ea])
            for art, kind in ((hit_sc, "HIT"), (blk_sc, "BLOCKED"), (ho_sc, "HEAD_ONLY"), (miss_sc, "MISS")):
                m = [(a, c) for a, k, c in dec_marks if a >= x0 and k == kind]
                art.set_data([a for a, _ in m], [c for _, c in m])
            ax_g.set_xlim(x0, now_s + 0.05)

        txt.set_text(f"HITS {scorer.hits}    BLOCKED {scorer.blocked}    MISS {scorer.misses}")
        recent = last_dec["text"] if time.time() - last_dec["t"] < 2.0 else ""
        txt_s.set_text(f"window ±{args.window:.0f} ms  margin {args.margin:.0f} ms   "
                       f"head-only {scorer.head_only}   coach: {coach.state}\n{recent}\n"
                       f"keys: t/space talk | r reset | q quit")
        if stop_event.is_set():
            plt.close(fig)
        return []

    def on_key(e):
        if e.key in ("t", " "):
            coach.trigger()
        elif e.key == "r":
            scorer.reset()
            dec_marks.clear()
            recent_dec.clear()
            session["t0"] = time.time()
            print("[RESET]")
        elif e.key == "q":
            stop_event.set()
        elif e.key in LABELS:
            label["cur"] = LABELS[e.key]
            print(f"[LABEL] {label['cur']}")

    fig.canvas.mpl_connect("key_press_event", on_key)
    fig.canvas.mpl_connect("close_event", lambda e: stop_event.set())
    ani = animation.FuncAnimation(fig, update, interval=PLOT_MS, blit=False,
                                  cache_frame_data=False)
    plt.tight_layout(rect=[0, 0, 1, 0.92])
    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        fcsv.close()
        if fw:
            fw.close()
        voice.stop()
        if bridge:
            bridge.stop()
        print(f"[DONE] hits={scorer.hits} blocked={scorer.blocked} miss={scorer.misses} head_only={scorer.head_only}")


if __name__ == "__main__":
    main()
