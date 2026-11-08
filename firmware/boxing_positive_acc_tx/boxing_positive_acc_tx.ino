/*
 * boxing_positive_acc_tx.ino — 拳击计分 sender（手套 / 头盔 共用同一份固件）
 * =====================================================================
 * 每块板子只改「配置区」的 ROLE（和必要时的 PIEZO_PIN）。
 *
 * 做的事：
 *   1. 4 kHz 定时采样 1 路压电（替代原来 loop 里能跑多快跑多快的 ~585 Hz）
 *   2. 本地事件检测：|偏离基线| >= TRIG 且 斜率 >= SLOPE_TRIG 记为 onset（斜率门限挡住工频纹波和
 *      饱和后的慢恢复尾巴），之后 PEAK_WIN 内找峰值和最大斜率，
 *      然后 REFRACT 死区（吃掉压电振铃/回弹），只把「事件包」发给 receiver
 *   3. 接收 receiver 每 100 ms 广播的同步信标，把本地 micros() 换算到 receiver 时基，
 *      所以手套和头盔的 onset 时间可以直接相减
 *   4. 另外发一路 500 Hz 抽稀波形（保峰抽稀），只用于 Python 画图和调阈值
 *
 * 引脚：压电必须接 ADC1（ESP32-S3: GPIO1–10；C3: GPIO0–4）。ADC2 和 WiFi 冲突。
 * 依赖：arduino-esp32 core（2.x / 3.x 都可，接收回调签名已做兼容）
 */

#include <esp_now.h>
#include <WiFi.h>
#include <esp_wifi.h>

// ======================= 配置区（每块板子改这里） =======================
#define ROLE 1                      // 1 = 手套（攻击方）, 2 = 头盔（被攻击方）
uint8_t receiverMac[] = {0x58, 0xE6, 0xC5, 0x68, 0x89, 0x44};   // 新 receiver 58:E6:C5:68:89:44
const int PIEZO_PIN    = 1;
const int WIFI_CHANNEL = 1;

// ----- 采样 -----
const uint32_t FS_HZ     = 4000;
const uint32_t PERIOD_US = 1000000UL / FS_HZ;
const int      SMOOTH_N  = 4;       // 滑动平均（4 点 @4 kHz = 1 ms），只压 ADC 噪声

// ----- 检测（单位：ADC counts，12 bit，3.3 V 满量程 ≈ 0.8 mV/count） -----
const int      TRIG        = 600;   // 幅值门限（≈0.48 V），静止时 maxdev 的 2~3 倍以上
const int      SLOPE_TRIG  = 400;   // 斜率门限（counts/ms ≈ 0.32 V/ms）。实测：空挥 jab 时手套压电惯性信号最陡 ≈0.23 V/ms，真实接触 ≈1 V/ms
const int      POLARITY    = +1;    // +1 只认上峰（当前设定）；-1 只认下峰；0 双向
const uint32_t PEAK_WIN_US = 20000; // onset 后 20 ms 内找峰
const uint32_t REFRACT_US  = 150000;// 事件后 150 ms 死区（盖住饱和振荡）
const float    BL_TAU_S    = 1.0f;  // 基线跟踪时间常数（IDLE 时一直跟踪，不再要求 |dev| 很小）
const uint32_t SETTLE_US   = 2000000; // 上电后先等 2 s 让 shift 电路的耦合电容充好
const uint32_t CALIB_US    = 1000000; // 再用 1 s 标定基线（保持静止）

// ----- 波形流（仅显示用） -----
const int DECIM = 8;                // 4 kHz / 8 = 500 Hz
#define WAVE_N 50                   // 每包 50 点 → 每 sender 10 包/s

// ----- 加速度计 Z 轴（仅手套板 ROLE 1 启用）：检测「出拳」事件，供 Python 判 MISS -----
const bool ACC_ENABLED = (ROLE == 1);
const int  ACC_PIN     = 2;         // Z-OUT → GPIO2（ADC1）
const int  ACC_SUB     = 4;         // 每 4 个压电采样读一次 → 1 kHz
const int  ACC_DECIM   = 4;         // 4 点平均 → 250 Hz 波形（ADXL335 带宽约 50 Hz，足够）
#define    ACC_ROLE      5          // 波形/事件包里用 role=5 表示「手套加速度」
// 阈值来自 2026-11-07 实测（250 Hz 平均后）：
//   慢动/护头/晃手：动态加速度 ≤ 0.96 g，变化率 ≤ 39 g/s；空挥 jab：+3.8 ~ +5.7 g，变化率 ~200+ g/s
const int      ACC_TRIG    = 614;   // 1.5 g（≈409.5 counts/g）
const int      ACC_STEP    = 98;    // 60 g/s × 4 ms ≈ 0.24 g / 每个 250 Hz 点
const float    ACC_BL_TAU  = 0.3f;  // 动态基线（去重力、去慢动）
const uint32_t ACC_REFRACT_US = 400000;  // 一次出拳 400 ms 内只记一次
// =======================================================================

// ---------- 包格式（必须和 boxing_rx.ino 完全一致） ----------
enum : uint8_t { PKT_SYNC = 0xA1, PKT_EVENT = 0xA2, PKT_WAVE = 0xA3 };

typedef struct __attribute__((packed)) {
  uint8_t  type;
  uint32_t rx_us;                   // receiver 发出信标时的 micros()
} SyncPkt;

typedef struct __attribute__((packed)) {
  uint8_t  type, role;
  uint16_t seq;
  uint32_t onset_us;                // receiver 时基
  uint32_t peak_us;                 // receiver 时基
  int16_t  peak;                    // 带符号峰值（counts，相对基线）
  uint16_t slope;                   // 最大斜率（counts/ms）
  uint16_t n_above;                 // 窗口内超阈值的采样点数
  uint8_t  synced;                  // 1 = 已收到过同步信标
} EventPkt;

typedef struct __attribute__((packed)) {
  uint8_t  type, role;
  uint16_t seq;
  uint32_t t0_us;                   // receiver 时基
  uint16_t dt_us;
  uint16_t baseline;
  int16_t  v[WAVE_N];               // 相对基线（counts）
} WavePkt;

// ---------- 时钟同步 ----------
#define SYNC_HIST 10
volatile int32_t clk_offset = 0;    // receiver_time ≈ local_time + clk_offset
volatile bool    synced     = false;
int32_t off_hist[SYNC_HIST];
int     off_n = 0, off_i = 0;

#if ESP_IDF_VERSION_MAJOR >= 5
void onRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
#else
void onRecv(const uint8_t* mac, const uint8_t* data, int len) {
#endif
  uint32_t now = micros();
  if (len != (int)sizeof(SyncPkt) || data[0] != PKT_SYNC) return;
  SyncPkt s;
  memcpy(&s, data, sizeof(s));
  // o = 真实偏移 - 单向延迟。取最近 10 次的最大值 = 延迟最小的那次，最接近真实偏移。
  // 两个 sender 的最小延迟基本相同，残差在相减时抵消。
  off_hist[off_i] = (int32_t)(s.rx_us - now);
  off_i = (off_i + 1) % SYNC_HIST;
  if (off_n < SYNC_HIST) off_n++;
  int32_t best = off_hist[0];
  for (int i = 1; i < off_n; i++) if (off_hist[i] > best) best = off_hist[i];
  clk_offset = best;
  synced = true;
}

// ---------- 检测状态 ----------
enum State { ST_CALIB, ST_IDLE, ST_ACTIVE, ST_REFRACT };
State st = ST_CALIB;

float    baseline = 0;
double   calib_sum = 0;
uint32_t calib_n = 0, t_calib0 = 0, t_boot = 0;
bool     boot_set = false;
const float bl_alpha = ((float)PERIOD_US / 1e6f) / BL_TAU_S;

uint32_t ev_onset = 0, ev_peak_t = 0, ev_end = 0;
float    ev_peak = 0, ev_slope = 0;
uint16_t ev_nabove = 0, ev_seq = 0;
float    prev_dev = 0;

int      sm_buf[SMOOTH_N];
long     sm_sum = 0;
int      sm_i = 0;
bool     sm_init = false;

WavePkt  wave;

WavePkt  accWave;
int      acc_k = 0, acc_dec_k = 0, acc_wave_i = 0;
long     acc_sum = 0;
uint32_t acc_block_t = 0;
uint16_t acc_seq = 0;
int      acc_last = 0, acc_min_win = 4095, acc_max_win = 0;
float    acc_bl = 0, acc_prev = 0;
bool     acc_bl_init = false;
uint32_t acc_refract_until = 0;
uint16_t acc_ev_seq = 0;
uint32_t n_punch = 0;
int      wave_i = 0, dec_k = 0;
float    dec_best = 0;
uint32_t block_t = 0;
uint16_t wave_seq = 0;

// ---------- 统计 ----------
uint32_t n_samples = 0, n_late = 0, n_events = 0;
float    maxdev_win = 0, maxslope_win = 0;

static inline int16_t clamp16(float v) {
  if (v > 32767) return 32767;
  if (v < -32768) return -32768;
  return (int16_t)v;
}

void sendEvent() {
  EventPkt e;
  int32_t off = clk_offset;
  e.type     = PKT_EVENT;
  e.role     = ROLE;
  e.seq      = ev_seq++;
  e.onset_us = ev_onset + off;
  e.peak_us  = ev_peak_t + off;
  e.peak     = clamp16(ev_peak);
  e.slope    = (uint16_t)min(ev_slope, 65535.0f);
  e.n_above  = ev_nabove;
  e.synced   = synced ? 1 : 0;
  // 事件包是关键数据：同一 seq 连发两次，receiver 去重
  esp_now_send(receiverMac, (uint8_t*)&e, sizeof(e));
  esp_now_send(receiverMac, (uint8_t*)&e, sizeof(e));
  n_events++;
}

void pushWave(float dev, uint32_t t) {
  if (dec_k == 0) block_t = t;
  if (fabsf(dev) > fabsf(dec_best)) dec_best = dev;   // 保峰抽稀：块内取 |dev| 最大的点
  if (++dec_k < DECIM) return;
  if (wave_i == 0) wave.t0_us = block_t;
  wave.v[wave_i++] = clamp16(dec_best);
  dec_k = 0;
  dec_best = 0;
  if (wave_i == WAVE_N) {
    wave.type     = PKT_WAVE;
    wave.role     = ROLE;
    wave.seq      = wave_seq++;
    wave.t0_us   += clk_offset;
    wave.dt_us    = DECIM * PERIOD_US;
    wave.baseline = (uint16_t)baseline;
    esp_now_send(receiverMac, (uint8_t*)&wave, sizeof(wave));
    wave_i = 0;
  }
}

// 加速度：发原始值（相对 2048 的偏移，baseline 固定 2048），方便 Python 还原成 g
void sampleAcc(uint32_t t) {
  int r = analogRead(ACC_PIN);
  acc_last = r;
  if (r < acc_min_win) acc_min_win = r;
  if (r > acc_max_win) acc_max_win = r;
  if (acc_dec_k == 0) { acc_block_t = t; acc_sum = 0; }
  acc_sum += r;
  if (++acc_dec_k < ACC_DECIM) return;
  acc_dec_k = 0;
  float avg = (float)acc_sum / ACC_DECIM;

  // ---- 出拳检测（在 250 Hz 平均值上做，和实测分析一致） ----
  if (!acc_bl_init) { acc_bl = avg; acc_prev = avg; acc_bl_init = true; }
  float d    = avg - acc_bl;
  float step = avg - acc_prev;
  acc_prev = avg;
  bool in_refract = (int32_t)(acc_block_t - acc_refract_until) < 0;
  if (!in_refract) {
    acc_bl += ((ACC_SUB * ACC_DECIM * PERIOD_US) / 1e6f / ACC_BL_TAU) * d;   // 出拳期间冻结基线
    if (d >= ACC_TRIG && step >= ACC_STEP) {
      EventPkt e;
      int32_t off = clk_offset;
      e.type     = PKT_EVENT;
      e.role     = ACC_ROLE;
      e.seq      = acc_ev_seq++;
      e.onset_us = acc_block_t + off;
      e.peak_us  = acc_block_t + off;
      e.peak     = clamp16(d);
      e.slope    = (uint16_t)max(0.0f, step / 4.0f);       // counts/ms
      e.n_above  = 0;
      e.synced   = synced ? 1 : 0;
      esp_now_send(receiverMac, (uint8_t*)&e, sizeof(e));
      esp_now_send(receiverMac, (uint8_t*)&e, sizeof(e));
      acc_refract_until = acc_block_t + ACC_REFRACT_US;
      n_punch++;
    }
  }

  if (acc_wave_i == 0) accWave.t0_us = acc_block_t;
  accWave.v[acc_wave_i++] = (int16_t)(avg - 2048);
  if (acc_wave_i == WAVE_N) {
    accWave.type     = PKT_WAVE;
    accWave.role     = ACC_ROLE;
    accWave.seq      = acc_seq++;
    accWave.t0_us   += clk_offset;
    accWave.dt_us    = ACC_SUB * ACC_DECIM * PERIOD_US;
    accWave.baseline = 2048;
    esp_now_send(receiverMac, (uint8_t*)&accWave, sizeof(accWave));
    acc_wave_i = 0;
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.printf("=== boxing_positive_acc_tx  ROLE=%d  pin=GPIO%d  fs=%lu Hz ===\n",
                ROLE, PIEZO_PIN, (unsigned long)FS_HZ);

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
  Serial.print("TX MAC: ");
  Serial.println(WiFi.macAddress());

  if (esp_now_init() != ESP_OK) {
    Serial.println("[ERROR] ESP-NOW init failed");
    while (true) delay(1000);
  }
  esp_now_register_recv_cb(onRecv);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, receiverMac, 6);
  peer.channel = WIFI_CHANNEL;
  peer.encrypt = false;
  if (esp_now_add_peer(&peer) != ESP_OK) {
    Serial.println("[ERROR] add peer failed");
    while (true) delay(1000);
  }
  Serial.println("[CAL] settling 2 s, then keep the sensor still for 1 s ...");
}

void loop() {
  static uint32_t t_next = micros();
  static uint32_t t_report = millis();

  // ---- 定时采样（忙等，loop 跑在 core 1，WiFi 在 core 0） ----
  while ((int32_t)(micros() - t_next) < 0) { }
  uint32_t t = t_next;
  t_next += PERIOD_US;
  if ((int32_t)(micros() - t_next) > (int32_t)PERIOD_US) {   // 落后超过一个周期
    n_late++;
    t_next = micros() + PERIOD_US;
  }

  int raw = analogRead(PIEZO_PIN);
  n_samples++;

  // ---- 滑动平均 ----
  if (!sm_init) {
    for (int i = 0; i < SMOOTH_N; i++) sm_buf[i] = raw;
    sm_sum = (long)raw * SMOOTH_N;
    sm_init = true;
  }
  sm_sum += raw - sm_buf[sm_i];
  sm_buf[sm_i] = raw;
  sm_i = (sm_i + 1) % SMOOTH_N;
  float x = (float)sm_sum / SMOOTH_N;

  // ---- 标定 ----
  if (st == ST_CALIB) {
    if (!boot_set) { t_boot = t; boot_set = true; }
    if (t - t_boot < SETTLE_US) return;          // 等 shift 电路稳定
    if (calib_n == 0) t_calib0 = t;
    calib_sum += x;
    calib_n++;
    if (t - t_calib0 >= CALIB_US) {
      baseline = calib_sum / calib_n;
      st = ST_IDLE;
      Serial.printf("[CAL] baseline = %.1f counts (%.3f V)\n", baseline, baseline * 3.3f / 4095.0f);
    }
    return;
  }

  // ---- 检测 ----
  float dev   = x - baseline;
  float a     = fabsf(dev);
  float dslope = (dev - prev_dev) * (FS_HZ / 1000.0f);        // 带符号斜率 counts/ms
  float slope  = fabsf(dslope);
  prev_dev = dev;
  bool pol_ok = (POLARITY == 0) || (POLARITY > 0 ? dev > 0 : dev < 0);
  // 触发用的方向斜率：只认上峰时必须是上升沿，下降沿（包括下峰之后的回弹）不触发
  float trig_slope = (POLARITY == 0) ? slope : dslope * POLARITY;
  if (a > maxdev_win) maxdev_win = a;
  if (slope > maxslope_win) maxslope_win = slope;

  switch (st) {
    case ST_IDLE:
      baseline += bl_alpha * dev;                 // 一直慢跟踪：初始偏差几秒内自动收敛
      if (a >= TRIG && trig_slope >= SLOPE_TRIG && pol_ok) {
        st = ST_ACTIVE;
        ev_onset = t; ev_peak = dev; ev_peak_t = t; ev_slope = slope; ev_nabove = 1;
      }
      break;
    case ST_ACTIVE:
      if (pol_ok && a > fabsf(ev_peak)) { ev_peak = dev; ev_peak_t = t; }
      if (slope > ev_slope) ev_slope = slope;
      if (a >= TRIG && pol_ok) ev_nabove++;
      if (t - ev_onset >= PEAK_WIN_US) {
        sendEvent();
        st = ST_REFRACT;
        ev_end = t;
      }
      break;
    case ST_REFRACT:
      if (t - ev_end >= REFRACT_US) st = ST_IDLE;
      break;
    default:
      break;
  }

  pushWave(dev, t);
  if (ACC_ENABLED && ++acc_k >= ACC_SUB) { acc_k = 0; sampleAcc(t); }

  // ---- 每 5 s 打印状态：静止时的 maxdev / maxslope ×2~3 作为 TRIG / SLOPE_TRIG 的下限 ----
  if (millis() - t_report >= 5000) {
    float secs = (millis() - t_report) / 1000.0f;
    Serial.printf("fs=%.0f Hz  late=%lu  events=%lu  baseline=%.0f (%.2f V)  maxdev=%.0f  maxslope=%.0f  sync=%s off=%ld us\n",
                  n_samples / secs, (unsigned long)n_late, (unsigned long)n_events,
                  baseline, baseline * 3.3f / 4095.0f, maxdev_win, maxslope_win,
                  synced ? "Y" : "N", (long)clk_offset);
    if (ACC_ENABLED) {
      // ADXL335 @3.3 V：0 g ≈ 1.65 V，约 0.33 V/g。拳头朝前静止时 accZ 应在 1.65 V 附近
      Serial.printf("   accZ=%d (%.2f V)  min=%d  max=%d  punches=%lu\n", acc_last, acc_last * 3.3f / 4095.0f,
                    acc_min_win, acc_max_win, (unsigned long)n_punch);
      acc_min_win = 4095; acc_max_win = 0;
    }
    t_report = millis();
    n_samples = 0;
    maxdev_win = 0;
    maxslope_win = 0;
  }
}
