/*
  IoT BREAKDOWN MONITORING SYSTEM
  ESP32 MACHINE STATUS TRANSMITTER
  API contract matched with IoT_Breakdown_System_Updated_Matched.py
*/

#include <WiFi.h>
#include <HTTPClient.h>

// -------- WIFI SETTINGS --------
const char* WIFI_SSID = "Infinix NOTE 50S 5G";
const char* WIFI_PASSWORD = "543212601";

// PC चा LAN IP address आणि Flask port तपासा.
// Python server HOST=0.0.0.0, PORT=5000 वर चालतो.
const char* SERVER_URL = "http://10.118.175.13:5000/machine";

// -------- MACHINE DETAILS --------
// Page 3 / machine_list/machines.json मधील values शी exact match असणे आवश्यक.
const char* MACHINE_ID = "LINE1_500TON_PRESS";
const char* LINE_NAME = "LINE 1";
const char* MACHINE_NUMBER = "M-01";
const char* MACHINE_NAME = "500 TON PRESS";

// -------- INPUT PINS --------
#define EMG_STOP        13
#define AIR_PRESS_LOW   14
#define LUB_PRESS_LOW   18
#define LUB_MOTOR_TRIP  19
#define MAIN_MOTOR_TRIP 21
#define HOL_PRESS_DROP  22

// INPUT_PULLUP + dry contact to GND: LOW means alarm active.
const bool INPUT_ACTIVE_LOW = true;

// -------- TIMING --------
const unsigned long SEND_INTERVAL = 2000;
const unsigned long WIFI_RETRY_INTERVAL = 5000;
const unsigned long HTTP_TIMEOUT_MS = 4000;
unsigned long lastSend = 0;
unsigned long lastWifiRetry = 0;

bool getAlarm(const int pin) {
  const int state = digitalRead(pin);
  return INPUT_ACTIVE_LOW ? (state == LOW) : (state == HIGH);
}

String jsonBool(const bool value) {
  return value ? "true" : "false";
}

String createJSON() {
  const bool emg   = getAlarm(EMG_STOP);
  const bool air   = getAlarm(AIR_PRESS_LOW);
  const bool lubp  = getAlarm(LUB_PRESS_LOW);
  const bool lubm  = getAlarm(LUB_MOTOR_TRIP);
  const bool mainm = getAlarm(MAIN_MOTOR_TRIP);
  const bool hol   = getAlarm(HOL_PRESS_DROP);

  String json;
  json.reserve(500);
  json  = "{\"machine_id\":\"" + String(MACHINE_ID) + "\",";
  json += "\"line\":\"" + String(LINE_NAME) + "\",";
  json += "\"number\":\"" + String(MACHINE_NUMBER) + "\",";
  json += "\"name\":\"" + String(MACHINE_NAME) + "\",";
  json += "\"alarms\":{";
  json += "\"emg_stop\":" + jsonBool(emg) + ",";
  json += "\"air_pressure_low\":" + jsonBool(air) + ",";
  json += "\"lub_pressure_low\":" + jsonBool(lubp) + ",";
  json += "\"lub_motor_trip\":" + jsonBool(lubm) + ",";
  json += "\"main_motor_trip\":" + jsonBool(mainm) + ",";
  json += "\"hol_pressure_drop\":" + jsonBool(hol);
  json += "}}";
  return json;
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWifiRetry = millis();

  Serial.print("Connecting to Wi-Fi");
  const unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 15000) {
    delay(500);
    Serial.print('.');
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WIFI CONNECTED");
    Serial.print("ESP32 IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("WIFI DISCONNECTED - will retry");
  }
}

void checkWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastWifiRetry >= WIFI_RETRY_INTERVAL) {
    lastWifiRetry = millis();
    Serial.println("Reconnecting Wi-Fi...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
}

void sendMachineData() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi not connected; data not sent");
    return;
  }

  const String payload = createJSON();
  Serial.println("POST /machine:");
  Serial.println(payload);

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(SERVER_URL)) {
    Serial.println("HTTP begin failed. Check SERVER_URL.");
    return;
  }

  http.addHeader("Content-Type", "application/json");
  const int code = http.POST((uint8_t*)payload.c_str(), payload.length());
  Serial.print("HTTP response: ");
  Serial.println(code);

  if (code > 0) {
    const String responseBody = http.getString();
    Serial.println(responseBody);
    if (code == 200) {
      Serial.println("DATA ACCEPTED BY PYTHON SERVER");
    } else if (code == 400) {
      Serial.println("BAD REQUEST / MACHINE ID NOT ENABLED / INVALID JSON");
    } else if (code == 409) {
      Serial.println("MACHINE IDENTITY MISMATCH: check ID, Line, Number and Name");
    } else {
      Serial.println("SERVER RETURNED AN UNEXPECTED STATUS CODE");
    }
  } else {
    Serial.print("HTTP error: ");
    Serial.println(http.errorToString(code));
    Serial.println("Check PC IP, port 5000, firewall and Python server.");
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(EMG_STOP, INPUT_PULLUP);
  pinMode(AIR_PRESS_LOW, INPUT_PULLUP);
  pinMode(LUB_PRESS_LOW, INPUT_PULLUP);
  pinMode(LUB_MOTOR_TRIP, INPUT_PULLUP);
  pinMode(MAIN_MOTOR_TRIP, INPUT_PULLUP);
  pinMode(HOL_PRESS_DROP, INPUT_PULLUP);

  Serial.println();
  Serial.println("IoT Breakdown Monitoring System - ESP32 Transmitter");
  connectWiFi();
}

void loop() {
  checkWiFi();
  if (WiFi.status() == WL_CONNECTED &&
      (lastSend == 0 || millis() - lastSend >= SEND_INTERVAL)) {
    lastSend = millis();
    sendMachineData();
  }
  delay(20);
}
