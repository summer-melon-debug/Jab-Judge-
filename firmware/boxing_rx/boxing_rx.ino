/*
 * boxing_rx.ino — 拳击计分 receiver（接电脑的那块）
 * =====================================================================
 * 1. 每 100 ms 广播同步信标（自己的 micros()），所有 sender 都对齐到这个时基
 * 2. ESP-NOW 回调里只把包塞进队列（不在回调里 Serial.print，避免阻塞 WiFi 任务、两路 sender 时丢包）
 * 3. loop() 里出队、按 (role, seq) 去重、打印成文本行给 Python：
 *      T,<rx_us>                                               时钟（每 100 ms）
 *      E,<role>,<seq>,<onset_us>,<peak_us>,<peak>,<slope>,<n_above>,<synced>
 *      W,<role>,<t0_us>,<dt_us>,<baseline>,v1,...,v50           抽稀波形（相对基线 counts）
 *      # ...                                                   状态信息
 * 重合判定放在 Python 里做，方便调窗口和阈值。
 */

#include <esp_now.h>
#include <WiFi.h>
#include <esp_wifi.h>

const int      WIFI_CHANNEL   = 1;
const uint32_t SYNC_PERIOD_MS = 100;
uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ---------- 包格式（必须和 boxing_positive_tx.ino 完全一致） ----------
enum : uint8_t { PKT_SYNC = 0xA1, PKT_EVENT = 0xA2, PKT_WAVE = 0xA3 };
#define WAVE_N 50

typedef struct __attribute__((packed)) {
  uint8_t  type;
  uint32_t rx_us;
} SyncPkt;

typedef struct __attribute__((packed)) {
  uint8_t  type, role;
  uint16_t seq;
  uint32_t onset_us, peak_us;
  int16_t  peak;
  uint16_t slope, n_above;
  uint8_t  synced;
} EventPkt;

typedef struct __attribute__((packed)) {
  uint8_t  type, role;
  uint16_t seq;
  uint32_t t0_us;
  uint16_t dt_us, baseline;
  int16_t  v[WAVE_N];
} WavePkt;

// ---------- 队列 ----------
typedef struct { uint8_t len; uint8_t buf[128]; } RxItem;
QueueHandle_t rxq;
volatile uint32_t q_drop = 0;

#define MAX_ROLE 8
int32_t  last_ev_seq[MAX_ROLE];
uint32_t n_ev[MAX_ROLE], n_wave[MAX_ROLE];

#if ESP_IDF_VERSION_MAJOR >= 5
void onRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
#else
void onRecv(const uint8_t* mac, const uint8_t* data, int len) {
#endif
  if (len <= 0 || len > 128) return;
  RxItem it;
  it.len = (uint8_t)len;
  memcpy(it.buf, data, len);
  if (xQueueSend(rxq, &it, 0) != pdTRUE) q_drop++;
}

void setup() {
  Serial.begin(921600);             // S3 原生 USB 下波特率无所谓；外接 USB-UART 时需 921600
  delay(500);
  Serial.println("# === boxing_rx ===");

  rxq = xQueueCreate(256, sizeof(RxItem));
  for (int i = 0; i < MAX_ROLE; i++) { last_ev_seq[i] = -1; n_ev[i] = n_wave[i] = 0; }

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
  Serial.print("# RX MAC: ");
  Serial.println(WiFi.macAddress());

  if (esp_now_init() != ESP_OK) {
    Serial.println("# [ERROR] ESP-NOW init failed");
    while (true) delay(1000);
  }
  esp_now_register_recv_cb(onRecv);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = WIFI_CHANNEL;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
  Serial.println("# listening");
}

void loop() {
  static uint32_t t_sync = 0, t_stat = 0;

  if (millis() - t_sync >= SYNC_PERIOD_MS) {
    t_sync = millis();
    SyncPkt s;
    s.type  = PKT_SYNC;
    s.rx_us = micros();
    esp_now_send(BCAST, (uint8_t*)&s, sizeof(s));
    Serial.printf("T,%lu\n", (unsigned long)s.rx_us);
  }

  RxItem it;
  while (xQueueReceive(rxq, &it, 0) == pdTRUE) {
    uint8_t type = it.buf[0];
    if (type == PKT_EVENT && it.len == sizeof(EventPkt)) {
      EventPkt e;
      memcpy(&e, it.buf, sizeof(e));
      if (e.role >= MAX_ROLE) continue;
      if ((int32_t)e.seq == last_ev_seq[e.role]) continue;   // 去重（sender 连发两次）
      last_ev_seq[e.role] = e.seq;
      n_ev[e.role]++;
      Serial.printf("E,%u,%u,%lu,%lu,%d,%u,%u,%u\n", e.role, e.seq,
                    (unsigned long)e.onset_us, (unsigned long)e.peak_us,
                    e.peak, e.slope, e.n_above, e.synced);
    } else if (type == PKT_WAVE && it.len == sizeof(WavePkt)) {
      WavePkt w;
      memcpy(&w, it.buf, sizeof(w));
      if (w.role >= MAX_ROLE) continue;
      n_wave[w.role]++;
      Serial.printf("W,%u,%lu,%u,%u", w.role, (unsigned long)w.t0_us, w.dt_us, w.baseline);
      for (int i = 0; i < WAVE_N; i++) Serial.printf(",%d", w.v[i]);
      Serial.println();
    }
  }

  if (millis() - t_stat >= 2000) {
    t_stat = millis();
    Serial.print("# wave pkts:");
    for (int r = 1; r < MAX_ROLE; r++)
      if (n_wave[r]) Serial.printf(" role%d=%lu", r, (unsigned long)n_wave[r]);
    Serial.printf("  q_drop=%lu\n", (unsigned long)q_drop);
  }
  delay(1);
}
