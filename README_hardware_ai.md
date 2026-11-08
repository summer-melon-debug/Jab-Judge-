# Jab Judge

**A ringside judge you wear.** Impact sensors in a boxing glove and in the opponent's headgear, plus an accelerometer on the glove, call every punch as a **HIT**, **BLOCKED**, or **MISS** in real time. The call is announced out loud, shown on a phone, and burned into a sparring video, and a voice AI coach answers questions about your accuracy using the live numbers.

Built at HackRPI 2026.

## What it does

| Call | What the sensors see | Scored |
|---|---|---|
| **HIT** | Glove impact and headgear impact within ±30 ms of each other | Yes |
| **BLOCKED** | Glove impact, no headgear impact (the punch landed on arms or gloves) | Counted as defended |
| **MISS** | The accelerometer sees a punch, but the glove touches nothing within 400 ms | Counted as missed |
| HEAD_ONLY | Headgear impact with no glove impact (other hand, head contact) | Logged only |

- **Voice calls:** "Hit!", "Blocked!", "Miss!" generated with **Fish Audio** and cached, so playback is instant and offline.
- **Voice coach:** hold a button on the phone (or press `t` on the computer) and ask "How am I doing?". The question and the live stats go to **Claude**, and the short answer is spoken back with the same Fish Audio voice.
- **Phone app:** a web app (GitHub Pages) shows the score, a call banner, and live signal lanes for glove impact, headgear impact, and arm motion.
- **Video with the calls burned in:** the phone can record the sparring session with the score, each call, and the scorer clock drawn on the video. It also saves a JSON list of every call with its time in the video, so footage lines up with the sensor log.

## How it works

```
 Glove sender (ESP32-S3)          Headgear sender (ESP32-S3)
 piezo impact + Z accelerometer   piezo impact
        │  ESP-NOW                       │  ESP-NOW
        └──────────────►  Receiver ESP32  ◄──────────┘
                          (sync beacon every 100 ms)
                                 │ USB serial
                                 ▼
                     Laptop: jab_judge.py
           pairing logic · plots · CSV · Fish Audio · Claude
                                 │ FastAPI WebSocket (ngrok)
                                 ▼
                        Phone web app (GitHub Pages)
             score · signals · voice · coach · video recording
```

- **Detection happens on each sender**, sampled at 4 kHz: a rising impact must pass both an amplitude threshold and a slope threshold, which rejects slow drift and the mechanical ringing after a hit.
- **Clock sync:** the receiver broadcasts its clock every 100 ms; each sender converts its event times to the receiver's clock, so glove and headgear timestamps can be compared directly regardless of radio delay.
- **Punch detection (MISS):** the glove's Z-axis accelerometer looks for a jab: at least 1.5 g of fast acceleration above a slow baseline. Thresholds were set from recorded data; slow movement, guarding, and shaking never reached them.
- **Lockout:** a second event from the same sensor within 400 ms is treated as ringing from the same punch and ignored.

## Hardware

- 3 × ESP32-S3: glove sender, headgear sender, receiver
- 2 × piezoelectric impact sensors with a level-shift front end, on GPIO1 (ADC1)
- 1 × ADXL335 analog accelerometer (Z axis toward the punch), on GPIO2 of the glove board
- LiPo batteries for the two senders

## Setup

### 1. Firmware (Arduino IDE, board: ESP32S3 Dev Module)

1. Flash `firmware/boxing_rx/boxing_rx.ino` to the receiver and note the MAC it prints (serial monitor at 921600 baud).
2. In `firmware/boxing_positive_acc_tx/boxing_positive_acc_tx.ino`, set `receiverMac[]` to that MAC.
3. Flash it to the glove with `#define ROLE 1` and to the headgear with `#define ROLE 2`.
4. Power on and keep both sensors still for about 3 seconds while they calibrate.

### 2. Laptop

```bash
cd pc
pip install -r requirements.txt
cp .env.example .env        # then add your FISH_API_KEY and ANTHROPIC_API_KEY
python jab_judge.py --port COM14 --phone
```

Useful options: `--phone-audio` (voice only on the phone), `--lockout 300`, `--window 30`, `--no-coach`, `--log-wave` (save all waveforms for tuning).

### 3. Phone

1. In another terminal: `ngrok http 8000`
2. Open the web app (`phone/index.html`, hosted on GitHub Pages), tap **Setup**, enter `wss://<your-ngrok-host>/ws`, and tap **Connect**. That tap also unlocks audio on the phone.
3. Tap **Camera** to preview and record a session.

## Output files

- `boxing_<time>.csv`: every sensor event, every call, and `VIDEO_START` / `VIDEO_STOP` markers, all on the scorer clock (`t_s`)
- `boxing_<time>_wave.csv`: full waveforms (with `--log-wave`)
- `jabjudge_<time>.mp4` and `jabjudge_<time>_events.json`: the recorded video and the call list with each call's time in the video

## Built with

ESP32-S3 · Arduino / C++ · ESP-NOW · Python · NumPy · Matplotlib · pygame · FastAPI · WebSockets · ngrok · Fish Audio · Claude (Anthropic) · Web Speech API · Web Audio API · MediaRecorder · GitHub Pages
