void setup() {
  Serial.begin(115200);
  analogReadResolution(12);
}

void loop() {
  int z = analogRead(4);

  Serial.printf("X:%d,Y:%d,Z:%d\n", x, y, z);
  delay(20);
}