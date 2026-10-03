"""
cloud.py - K6 cloud connector cho edge server.

Edge đóng vai trò GATEWAY giữa fleet STM32 (MFRC522, servo, LEDs, buzzer)
và platform NEXUS K6 (README.cloud.md):

  1. REST : login operator -> POST /api/devices (provision:true) để đăng ký
            các device (STM32 + components) và lấy device token.
  2. MQTT : publish telemetry lên  k6/{projectId}/{deviceId}/telemetry
            publish status lên     k6/{projectId}/{deviceId}/status
            (retained, có token, cùng format với simulator của K6)

Telemetry frame (capability-driven):

  {
    "token": "<device token>",
    "wifiSignalStrength": -60,          # health: network (dBm)
    "metrics": { "tagId": "04A1B2C3" }, # data: theo capability từng loại
    "timestamp": "2024-01-15T10:30:00.000Z"
  }

Lịch gửi:
  - Health status (connection/network) của MỌI device: mỗi 2 phút
  - Access event (RFID + servo + LEDs + buzzer): ngay sau mỗi decision

Cấu hình qua biến môi trường:

    K6_ENABLED               = 1 (bật, mặc định) | 0
    K6_MQTT_URL              = mqtt://127.0.0.1:1883
    K6_API_URL               = http://127.0.0.1:8080
    K6_PROJECT_ID            = <uuid project>
    K6_AUTH_EMAIL            = operator@nexus.local
    K6_AUTH_PASSWORD         = nexus
    K6_HEALTH_INTERVAL_SEC   = 120
    K6_NETWORK_DBM           = -60
    K6_CREDENTIALS_FILE      = cloud_credentials.json
"""

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Device type codes (từ backend/config/device-types.yaml của K6)
# ---------------------------------------------------------------------------
K6_TYPE_CONTROLLER = "5122000001"   # Industrial Controller  -> STM32
K6_TYPE_CARD_READER = "4211000008"  # Card Reader (actuator)  -> MFRC522
K6_TYPE_SERVO = "4211000006"        # Servo Motor            -> servo
K6_TYPE_LED = "4211000001"          # Smart LED              -> green/red LED
K6_TYPE_SWITCH = "4211000002"       # Smart Switch           -> buzzer

# Capabilities khai báo cho từng loại (health dùng chung `network`;
# data metrics bọc trong `metrics` của telemetry frame).
COMPONENTS = {
    "mfrc522": {"type": K6_TYPE_CARD_READER, "label": "MFRC522 RFID reader",
                "data": ["tagId"]},
    "servo": {"type": K6_TYPE_SERVO, "label": "Barrier servo",
              "data": ["position"]},
    "green_led": {"type": K6_TYPE_LED, "label": "Green LED",
                  "data": ["power"]},
    "red_led": {"type": K6_TYPE_LED, "label": "Red LED",
                "data": ["power"]},
    "buzzer": {"type": K6_TYPE_SWITCH, "label": "Buzzer",
               "data": ["power"]},
}

DEFAULTS = {
    "mqtt_url": "mqtt://127.0.0.1:1883",
    "api_url": "http://127.0.0.1:8080",
    "auth_email": "root@nexus.local",
    "auth_password": "nexus",
    "health_interval_sec": 120,
    "network_dbm": -60,
    "credentials_file": "cloud_credentials.json",
}


def _env(name, default=None):
    value = os.getenv(name)
    return value if value else default


# Sau mỗi decision, actuators (servo/LED/buzzer) giữ trạng thái này rồi tự
# reset về trạng thái nghỉ. Cấu hình được bằng env K6_ACTUATOR_RESET_SEC.
ACTUATOR_RESET_SEC = float(_env("K6_ACTUATOR_RESET_SEC", "5"))

# Sau mỗi decision, actuators (servo/LED/buzzer) giữ trạng thái này rồi tự
# reset về trạng thái nghỉ. Cấu hình được bằng env K6_ACTUATOR_RESET_SEC.
ACTUATOR_RESET_SEC = float(_env("K6_ACTUATOR_RESET_SEC", "5"))


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _hex12(seed):
    """Chuẩn hoá device_id về 12 ký tự hex (wire identity của K6)."""
    digits = "".join(c for c in str(seed).upper() if c in "0123456789ABCDEF")
    if len(digits) >= 12:
        return digits[:12]
    return hashlib.md5(str(seed).encode("utf-8")).hexdigest().upper()[:12]


def component_id(base_device_id, name):
    """
    Device id 12-hex deterministic cho 1 component (kéo dài qua restart):
    4 nibble đầu giữ đặc tính family/power/mobility/network của STM32,
    8 nibble sau băm từ tên + type code (đổi type = đổi id, tránh xung đột
    với device cũ đã provision nhầm type).
    """
    base = _hex12(base_device_id)
    type_code = COMPONENTS.get(name, {}).get("type", "")
    digest = hashlib.md5(f"{base}:{name}:{type_code}".encode("utf-8")).hexdigest().upper()
    return base[:4] + digest[:8]


class K6Cloud:
    """Kết nối edge -> K6: REST provisioning + MQTT telemetry/status."""

    def __init__(self, mqtt_url, api_url, project_id, auth_email, auth_password,
                 network_dbm=-60, health_interval_sec=120,
                 credentials_file=DEFAULTS["credentials_file"]):
        self.mqtt_url = mqtt_url
        self.api_url = api_url.rstrip("/")
        self.project_id = (project_id or "").strip()
        self.auth_email = auth_email
        self.auth_password = auth_password
        self.network_dbm = int(network_dbm)
        self.health_interval_sec = max(5, int(health_interval_sec))
        self.credentials_file = credentials_file

        self.enabled = bool(mqtt_url and self.project_id)
        self._lock = threading.Lock()
        self._bearer = None          # access token của operator (REST)
        self._refresh = None         # refresh token (REST)
        self._expires_at = 0.0
        # registry: { stm32_edge_id: {"deviceId", "token", status, components: {
        #              name: {"deviceId", "token", data: {...}} }} }
        self._fleet = {}
        self._mqtt = None
        self._connected = False
        self._stopped = False
        self._load_credentials()

    # ------------------------------------------------------------------
    # Bootstrapping
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, disabled=False):
        disabled = disabled or _env("K6_ENABLED", "1").lower() in ("0", "false", "no")
        return cls(
            mqtt_url=None if disabled else _env("K6_MQTT_URL", DEFAULTS["mqtt_url"]),
            api_url=_env("K6_API_URL", DEFAULTS["api_url"]),
            project_id=None if disabled else _env("K6_PROJECT_ID"),
            auth_email=_env("K6_AUTH_EMAIL", DEFAULTS["auth_email"]),
            auth_password=_env("K6_AUTH_PASSWORD", DEFAULTS["auth_password"]),
            network_dbm=_env("K6_NETWORK_DBM", DEFAULTS["network_dbm"]),
            health_interval_sec=_env("K6_HEALTH_INTERVAL_SEC",
                                     DEFAULTS["health_interval_sec"]),
            credentials_file=_env("K6_CREDENTIALS_FILE", DEFAULTS["credentials_file"]),
        )

    def start(self, disabled=False):
        if disabled:
            self.enabled = False
        if not self.enabled:
            print("[CLOUD] tắt (K6_ENABLED=0 hoặc thiếu K6_MQTT_URL/K6_PROJECT_ID)")
            return
        threading.Thread(target=self._bootstrap, name="cloud-bootstrap",
                         daemon=True).start()
        threading.Thread(target=self._health_loop, name="cloud-health",
                         daemon=True).start()
        print(f"[CLOUD] K6 cloud: mqtt={self.mqtt_url} api={self.api_url} "
              f"project={self.project_id}")

    def stop(self):
        self._stopped = True
        with self._lock:
            self._broadcast_status("offline")
        if self._mqtt is not None:
            try:
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # REST: operator session + provisioning
    # ------------------------------------------------------------------
    def _login(self):
        import requests

        resp = requests.post(f"{self.api_url}/api/auth/login",
                             json={"email": self.auth_email,
                                   "password": self.auth_password},
                             timeout=10)
        resp.raise_for_status()
        session = resp.json()
        self._bearer = session["token"]
        self._refresh = session.get("refreshToken")
        try:
            from datetime import datetime as dt
            self._expires_at = dt.fromisoformat(
                session["expiresAt"].replace("Z", "+00:00")).timestamp()
        except Exception:
            self._expires_at = time.time() + 25 * 60
        print("[CLOUD] login K6 API thành công")

    def _ensure_session(self):
        import requests

        if self._bearer and time.time() < self._expires_at - 60:
            return
        if self._refresh:
            try:
                resp = requests.post(f"{self.api_url}/api/auth/refresh",
                                     json={"refreshToken": self._refresh},
                                     timeout=10)
                if resp.ok:
                    session = resp.json()
                    self._bearer = session["token"]
                    self._refresh = session.get("refreshToken", self._refresh)
                    self._expires_at = time.time() + 25 * 60
                    return
            except Exception as exc:
                print(f"[CLOUD] refresh session lỗi: {exc}")
        self._login()

    def _api(self, method, path, payload=None):
        import requests

        self._ensure_session()
        resp = requests.request(method, f"{self.api_url}{path}",
                                json=payload,
                                headers={"Authorization": f"Bearer {self._bearer}"},
                                timeout=15)
        if resp.status_code == 401:  # hết hạn giữa chừng -> login lại 1 lần
            self._login()
            resp = requests.request(method, f"{self.api_url}{path}",
                                    json=payload,
                                    headers={"Authorization": f"Bearer {self._bearer}"},
                                    timeout=15)
        return resp

    def _provision(self, device_id, name, device_type, parent_id=None,
                   data_keys=()):
        """Đăng ký device lên K6 (idempotent) và trả về token."""
        capabilities = {"network": {}}
        for key in data_keys:
            capabilities[key] = {}

        resp = self._api("POST", "/api/devices", {
            "projectId": self.project_id,
            "deviceId": device_id,
            "name": name,
            "deviceTypeId": device_type,
            "parentDeviceId": parent_id,
            "capabilities": capabilities,
            "metadata": {"fleet": "access-control", "source": "edge"},
            "provision": True,
        })
        if resp.status_code == 201:
            token = resp.json().get("token")
            print(f"[CLOUD] đã provision device {device_id} ({name})")
            self._save_token(device_id, token)
            return token
        if resp.status_code == 409:
            # Device đã tồn tại: dùng token cache, nếu không có thì rotate.
            token = self._cached_token(device_id)
            if token:
                return token
            resp = self._api("POST", f"/api/devices/{device_id}/token")
            if resp.status_code == 200:
                token = resp.json().get("token")
                self._save_token(device_id, token)
                print(f"[CLOUD] đã rotate token cho device {device_id}")
                return token
        print(f"[CLOUD] provision {device_id} lỗi HTTP {resp.status_code}: "
              f"{resp.text[:200]}")
        return None

    # Token cache -------------------------------------------------------------
    def _load_credentials(self):
        self._cache = {}
        try:
            with open(self.credentials_file, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if data.get("projectId") == self.project_id:
                self._cache = data.get("devices", {})
        except Exception:
            pass

    def _save_credentials(self):
        try:
            with open(self.credentials_file, "w", encoding="utf-8") as fp:
                json.dump({"projectId": self.project_id,
                           "devices": self._cache}, fp, indent=2)
        except Exception as exc:
            print(f"[CLOUD] không ghi được token cache: {exc}")

    def _cached_token(self, device_id):
        return self._cache.get(device_id)

    def _save_token(self, device_id, token):
        if device_id and token:
            self._cache[device_id] = token
            self._save_credentials()

    # ------------------------------------------------------------------
    # Registry: đăng ký STM32 + components, publish lên K6
    # ------------------------------------------------------------------
    def _bootstrap(self):
        """Chạy nền khi start(): đăng ký trước STM32 mặc định (nếu đã biết)
        để fleet sẵn sàng trên K6 ngay cả khi chưa có thẻ quẹt nào."""
        try:
            stm32_device_id = os.environ.get("EDGE_DEVICE_ID") or ""
            if not stm32_device_id:
                try:
                    import server
                    stm32_device_id = getattr(server, "STM32_DEVICE_ID", "") or ""
                except Exception:
                    pass
            if stm32_device_id:
                self.register_device(stm32_device_id)
            else:
                print("[CLOUD] bootstrap bỏ qua: chưa biết device_id STM32 "
                      "(sẽ đăng ký khi có thẻ quẹt đầu tiên / serial link up)")
        except Exception as exc:
            print(f"[CLOUD] bootstrap lỗi (sẽ thử lại khi first decision): {exc}")

    def register_device(self, stm32_device_id):
        """Đảm bảo STM32 + 5 components đã có trên K6; publish status online.
        Trả về entry registry hoặc None nếu chưa kết nổi cloud."""
        stm32_device_id = str(stm32_device_id or "").strip()
        if not self.enabled or not stm32_device_id:
            return None

        # Đăng ký ngoài lock (REST chậm); race song song vẫn an toàn vì
        # provisioning phía K6 là idempotent (409 -> dùng token cache).
        with self._lock:
            entry = self._fleet.get(stm32_device_id)
            if entry:
                return entry
        entry = self._register(stm32_device_id)
        if entry:
            with self._lock:
                self._fleet.setdefault(stm32_device_id, entry)
        return entry

    def _register(self, stm32_device_id):
        try:
            stm32_k6_id = _hex12(stm32_device_id)
            token = self._provision(stm32_k6_id, f"STM32 {stm32_device_id}",
                                    K6_TYPE_CONTROLLER)
            if not token:
                return None

            components = {}
            for name, spec in COMPONENTS.items():
                cid = component_id(stm32_device_id, name)
                ctoken = self._provision(
                    cid, f"{spec['label']} ({stm32_device_id})", spec["type"],
                    parent_id=stm32_k6_id, data_keys=spec["data"])
                if ctoken:
                    components[name] = {"deviceId": cid, "token": ctoken,
                                        "data": {}}
            entry = {"deviceId": stm32_k6_id, "token": token, "data": {},
                     "components": components}
            self._connect_mqtt_if_needed()
            self._wait_mqtt()
            self._publish_frame(entry, {})
            self._publish_status(entry, "online")
            for component in components.values():
                self._publish_frame(component, {})
                self._publish_status(component, "online")
            print(f"[CLOUD] fleet sẵn sàng: STM32 {stm32_device_id} "
                  f"({stm32_k6_id}) + {len(components)} components")
            return entry
        except Exception as exc:
            print(f"[CLOUD] provisioning lỗi: {exc}")
            return None

    # ------------------------------------------------------------------
    # Access event: gọi ngay sau khi verify_access xong
    # ------------------------------------------------------------------
    def on_decision(self, stm32_device_id, rfid_uid, result, reason):
        """
        Publish 1 loạt telemetry sau decision + tự reset sau ACTUATOR_RESET_SEC:
          - accepted : servo mở 90°, green LED sáng, red/buzzer tắt
          - denied   : red LED sáng, buzzer kêu, green tắt, servo đóng
        """
        entry = self.register_device(stm32_device_id)
        if not entry:
            return
        accepted = result == "granted"
        try:
            with self._lock:
                updates = {
                    "mfrc522": {"tagId": rfid_uid},
                    "servo": {"position": 90 if accepted else 0},
                    "green_led": {"power": accepted},
                    "red_led": {"power": not accepted},
                    "buzzer": {"power": not accepted},
                }
                for name, data in updates.items():
                    component = entry["components"].get(name)
                    if component:
                        component["data"].update(data)
                        self._publish_frame(component, component["data"])
                print(f"[CLOUD] access event -> K6: {result} ({reason})")

                # Timer reset: sau ACTUATOR_RESET_SEC hồi trạng thái nghỉ.
                # Hủy timer cũ nếu có thẻ quẹt liên tiếp trong khoảng reset.
                self._cancel_reset_timer(entry)
                timer = threading.Timer(
                    ACTUATOR_RESET_SEC,
                    self._reset_actuators,
                    args=(stm32_device_id, entry),
                )
                timer.daemon = True
                entry["resetTimer"] = timer
                timer.start()
        except Exception as exc:
            print(f"[CLOUD] publish access event lỗi: {exc}")

    def _reset_actuators(self, stm32_device_id, entry):
        """Sau 5s: servo về 0°, tắt cả green/red LED + buzzer."""
        try:
            with self._lock:
                entry["resetTimer"] = None
                if self._fleet.get(stm32_device_id) is not entry:
                    return
                resets = {
                    "servo": {"position": 0},
                    "green_led": {"power": False},
                    "red_led": {"power": False},
                    "buzzer": {"power": False},
                }
                for name, data in resets.items():
                    component = entry["components"].get(name)
                    if component:
                        component["data"].update(data)
                        self._publish_frame(component, component["data"])
                print(f"[CLOUD] reset actuators sau {ACTUATOR_RESET_SEC:.0f}s "
                      f"(STM32 {stm32_device_id})")
        except Exception as exc:
            print(f"[CLOUD] reset actuators lỗi: {exc}")

    def _cancel_reset_timer(self, entry):
        timer = entry.get("resetTimer")
        if timer:
            timer.cancel()
            entry["resetTimer"] = None

    # ------------------------------------------------------------------
    # Serial link health (rút cáp / mất kết nối STM32)
    # ------------------------------------------------------------------
    def set_link(self, up, device_id=None):
        """""Serial tới STM32 vừa (mất) kết nối -> online/offline cả fleet;
        khi lên lại sẽ đăng ký (nếu chưa) rồi mới báo online."""
        if not self.enabled:
            return
        if up and device_id:
            entry = self.register_device(device_id)
            if not entry:
                return
        with self._lock:
            self._broadcast_status("online" if up else "offline")
        print(f"[CLOUD] serial link {'UP -> fleet online' if up else 'DOWN -> fleet offline'}")

    # ------------------------------------------------------------------
    # Health loop: mỗi 2 phút báo connection/network status mọi device
    # ------------------------------------------------------------------
    def _health_loop(self):
        interval = self.health_interval_sec
        # Chờ bootstrap xong rồi mới bắt đầu chu kỳ.
        time.sleep(min(15, interval / 10))
        while not self._stopped:
            try:
                with self._lock:
                    for entry in self._fleet.values():
                        self._publish_frame(entry, entry.get("data") or {})
                        for component in entry["components"].values():
                            self._publish_frame(component,
                                                component.get("data") or {})
                if self._fleet:
                    print(f"[CLOUD] health heartbeat: {sum(1 + len(e['components']) for e in self._fleet.values())} devices")
            except Exception as exc:
                print(f"[CLOUD] health heartbeat lỗi: {exc}")
            time.sleep(interval)

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------
    def _connect_mqtt_if_needed(self):
        if self._mqtt is not None:
            return  # loop_start() đã tự động reconnect
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("[CLOUD] thiếu paho-mqtt -> pip install paho-mqtt")
            self.enabled = False
            return

        from urllib.parse import urlparse
        parsed = urlparse(self.mqtt_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 1883

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"k6-edge-{int(time.time())}")
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.connect_async(host, port, keepalive=60)
        client.loop_start()
        self._mqtt = client

    def _wait_mqtt(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline and not self._connected:
            time.sleep(0.2)
        return self._connected

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = True
        with self._lock:
            self._broadcast_status("online")
        print(f"[CLOUD] MQTT connected ({reason_code})")

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False
        if not self._stopped:
            print("[CLOUD] MQTT mất kết nối, tự reconnect...")

    def _publish_frame(self, device, metrics):
        """Publish 1 telemetry frame; metrics rỗng = health-only frame."""
        if not (self._mqtt and self._connected):
            return
        frame = {
            "token": device["token"],
            "wifiSignalStrength": self.network_dbm,
            "timestamp": _now_iso(),
        }
        clean = {k: v for k, v in metrics.items() if v is not None}
        if clean:
            frame["metrics"] = clean
            frame["metrics"] = clean
        topic = f"k6/{self.project_id}/{device['deviceId']}/telemetry"
        try:
            self._mqtt.publish(topic, json.dumps(frame), qos=1)
        except Exception as exc:
            print(f"[CLOUD] publish {topic} lỗi: {exc}")

    def _publish_status(self, device, status):
        if not (self._mqtt and self._connected):
            return
        topic = f"k6/{self.project_id}/{device['deviceId']}/status"
        payload = json.dumps({"status": status, "token": device["token"]})
        try:
            self._mqtt.publish(topic, payload, qos=1, retain=True)
        except Exception as exc:
            print(f"[CLOUD] publish {topic} lỗi: {exc}")

    def _broadcast_status(self, status):
        for entry in self._fleet.values():
            self._publish_status(entry, status)
            for component in entry["components"].values():
                self._publish_status(component, status)