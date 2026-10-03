"""
server.py - Edge server (HTTP API + Serial/COM)

Hai kênh vào, dùng CHUNG một logic verify và CHUNG audit log:

  1. HTTP   : ESP32 gateway gọi qua WiFi/LAN
              POST /api/devices/<device_id>/verify   {"rfid_uid": "..."}
  2. Serial : STM32 nối trực tiếp qua USB-UART (CP2102, CH340... không cần ESP32)
              STM32 -> edge : "RFID:<uid>\n"
              edge  -> STM32: "GRANT\n" / "DENY\n"

Lưu ý: device_id là ID của STM32, không phải MAC của ESP32.

Chạy:
    pip install -r requirements.txt
    python server.py                                   # chỉ HTTP, 0.0.0.0:5000
    python server.py --serial-port COM8                 # cổng CP2102
    python server.py --serial-port auto                # tự dò cổng USB-UART
    python server.py --list-ports                      # liệt kê cổng COM

Cấu hình qua biến môi trường (đều có thể ghi đè bằng CLI):
    EDGE_HOST, EDGE_PORT, EDGE_SERIAL_PORT, EDGE_SERIAL_BAUD, EDGE_DEVICE_ID
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import os
import sys
import threading
import time

from flask import Flask, jsonify, request

import db
import cloud

app = Flask(__name__)

# Kết nối tới NEXUS K6 cloud (REST provisioning + MQTT telemetry).
cloud_bridge = cloud.K6Cloud.from_env()

DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 5000
DEFAULT_SERIAL_BAUD = 115200
SERIAL_READ_TIMEOUT = 1.0        # giây, đọc line
SERIAL_RECONNECT_DELAY = 3.0     # giây, thử mở lại cổng khi lỗi/rút cáp
USB_UART_HINTS = (
    "CP210", "CP2102", "CP210X",           # Silicon Labs CP2102
    "SILICON LABS", "SILABS",
    "CH340", "CH341", "WCH",              # WCH CH340/CH341
    "FT232", "FTDI", "PL2303",            # các USB-UART khác
    "USB-SERIAL", "USB SERIAL", "USB TO UART",
)

# ---------------------------------------------------------------------------
# Device ID của STM32 (HARDCODE tạm thời)
# ---------------------------------------------------------------------------
# Firmware STM32 hiện đang dùng device_id khác nên chưa sửa được: mọi device_id
# gửi lên (HTTP lẫn serial) đều bị ghi đè bằng id chuẩn này, khớp với seed.py.
# Vẫn có thể ghi đè bằng --device-id / EDGE_DEVICE_ID mà không cần chạy lại
# firmware. Khi firmware đã sửa đúng, xoá phần override trong verify_access().
STM32_DEVICE_ID = "421108000003"


def _env(name, default=None):
    value = os.environ.get(name)
    return value if value else default


def _force_utf8_console():
    """Console Windows mặc định là cp1252/cp437 -> print tiếng Việt sẽ crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_console()


# ---------------------------------------------------------------------------
# Logic dùng chung cho cả HTTP và Serial
# ---------------------------------------------------------------------------

def verify_access(device_id, rfid_uid, source="http"):
    """
    Verify và ghi audit log. Trả về (result, reason, http_status).
    result: "granted" | "denied"
    """
    raw_device_id = str(device_id or "").strip()
    # HARDcode device_id STM32: firmware chưa sửa được nên bỏ qua id MCU gửi lên
    device_id = STM32_DEVICE_ID
    rfid_uid = str(rfid_uid or "").strip()

    if not device_id or not rfid_uid:
        result, reason, status = "denied", "missing_fields", 400
    else:
        try:
            granted, reason = db.check_access(device_id, rfid_uid)
        except Exception as exc:  # fail-closed: lỗi DB -> deny
            result, reason, status = "denied", "db_error", 500
            print(f"[{source.upper()}] lỗi DB: {exc}")
        else:
            result = "granted" if granted else "denied"
            status = 200 if granted else 401

    print(f"[{source.upper()}] device={device_id}"
          f"{' (MCU gửi: ' + raw_device_id + ')' if raw_device_id and raw_device_id != device_id else ''} "
          f"uid={rfid_uid or '-'} "
          f"-> {result} ({reason})")

    if device_id or rfid_uid:
        try:
            db.log_access(device_id, rfid_uid, result=result, reason=reason)
        except Exception as exc:
            print(f"[{source.upper()}] lỗi ghi audit log: {exc}")

    # Cloud: ngay sau khi xử lý decision xong, gửi access event lên K6
    # (RFID + servo status + green/red LEDs status + buzzer status).
    cloud_bridge.on_decision(device_id, rfid_uid, result, reason)

    return result, reason, status


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

@app.route("/api/devices/<device_id>/verify", methods=["POST"])
def verify_device(device_id):
    """Endpoint chính. device_id lấy từ path = ID của STM32."""
    data = request.get_json(silent=True) or {}
    result, reason, status = verify_access(device_id, data.get("rfid_uid"), source="http")
    return jsonify({"result": result, "reason": reason}), status


@app.route("/api/verify", methods=["POST"])
def verify_legacy():
    """Endpoint cũ, giữ lại để tương thích ngược - device_id nằm trong body."""
    data = request.get_json(silent=True) or {}
    result, reason, status = verify_access(
        data.get("device_id"), data.get("rfid_uid"), source="http"
    )
    return jsonify({"result": result, "reason": reason}), status


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Serial protocol (STM32 <-> edge qua USB-UART: CP2102/CH340)
# ---------------------------------------------------------------------------
#
#   STM32 -> edge : RFID:<device_id>,<uid>      (giống hệt gateway ESP8266 gửi lên)
#                   RFID:<uid>                  (dùng device_id mặc định của server)
#                   ID:<device_id>              (đặt device_id mặc định cho session)
#                   PING / HELLO
#   edge -> STM32 : GRANT | DENY | OK | PONG | DEVICE:<device_id>
#
# Dùng dấu PHẨY giữa device_id và uid vì device_id là MAC, bản thân nó đã chứa
# dấu ':' (VD: AA:BB:CC:DD:EE:01) -> tách bằng ':' sẽ nhập nhằng.
#
# Chỉ gửi GRANT/DENY cho STM32; reason chi tiết nằm trong log của edge.

VERIFY_TAGS = ("RFID", "VERIFY", "ACCESS")


def parse_serial_line(line, default_device_id=None):
    """
    Parse 1 dòng lệnh verify từ STM32 (cùng format với gateway ESP8266):

        RFID:<device_id>,<uid>   -> (device_id, uid)
        RFID:<uid>               -> (device_id mặc định, uid)

    Trả về (device_id, rfid_uid) hoặc None nếu không phải lệnh verify.
    Không tự đổi hoa/thường của uid - phải khớp đúng giá trị lưu trong DB.
    """
    raw = str(line or "").strip()
    if not raw:
        return None

    tag, _, payload = raw.partition(":")
    if tag.strip().upper() not in VERIFY_TAGS:
        return None

    default_id = str(default_device_id or "").strip()
    payload = payload.strip()

    if "," in payload:
        device_id, _, uid = payload.partition(",")
        # device_id rỗng -> dùng mặc định của server
        return device_id.strip() or default_id, uid.strip()

    # Không có dấu phẩy -> cả payload là uid
    return default_id, payload


def handle_serial_line(line, device_id=None):
    """
    Xử lý 1 dòng từ STM32.
    Trả về (reply, device_id): reply là chuỗi cần gửi lại (None = không gửi),
    device_id là giá trị có thể đã đổi (lệnh ID:<device_id>).
    """
    raw = str(line or "").strip()
    if not raw:
        return None, device_id

    tag, _, payload = raw.partition(":")
    command = tag.strip().upper()

    if command == "PING":
        return "PONG", device_id

    if command in ("HELLO", "WHOAMI"):
        return f"DEVICE:{device_id or 'unset'}", device_id

    if command in ("ID", "DEV", "SETID"):
        new_id = payload.strip()
        if not new_id:
            return "ERROR:empty_device_id", device_id
        print(f"[SERIAL] đặt device_id mặc định = {new_id}")
        return "OK", new_id

    parsed = parse_serial_line(raw, device_id)
    if parsed is None:
        print(f"[SERIAL] bỏ qua dòng không hợp lệ: {raw!r}")
        return None, device_id

    parsed_device_id, rfid_uid = parsed
    result, _reason, _status = verify_access(parsed_device_id, rfid_uid, source="serial")
    return ("GRANT" if result == "granted" else "DENY"), device_id


def _serial_loop(ser, device_id):
    """Vòng đọc/ghi trên 1 kết nối serial đã mở. Thoát khi mất kết nối."""
    while True:
        try:
            raw = ser.readline()
        except Exception as exc:
            print(f"[SERIAL] mất kết nối: {exc}")
            return

        if not raw:
            continue  # timeout, không có dòng nào

        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        print(f"[SERIAL] <- {line}")

        reply, device_id = handle_serial_line(line, device_id)
        if not reply:
            continue

        try:
            ser.write((reply + "\n").encode("utf-8"))
            ser.flush()
        except Exception as exc:
            print(f"[SERIAL] không ghi được: {exc}")
            return
        print(f"[SERIAL] -> {reply}")


def _serial_ports():
    from serial.tools import list_ports

    return list(list_ports.comports())


def _port_text(port):
    """Gộp mọi mô tả của 1 cổng thành 1 chuỗi để so khớp."""
    return " ".join(
        str(x) for x in (port.description, port.manufacturer, port.product, port.hwid) if x
    ).upper()


def _is_usb_uart(port):
    return any(hint in _port_text(port) for hint in USB_UART_HINTS)


def _describe_port(device_name):
    """Trả về mô tả của cổng (VD 'Silicon Labs CP210x USB to UART Bridge')."""
    try:
        for port in _serial_ports():
            if port.device.upper() == str(device_name).upper():
                return port.description or ""
    except Exception:
        pass
    return ""


def _autodetect_port():
    """Trả về cổng USB-UART (CP2102/CH340...) đầu tiên, fallback cổng COM đầu tiên."""
    ports = _serial_ports()
    for port in ports:
        if _is_usb_uart(port):
            return port.device
    return ports[0].device if ports else None


def _print_ports():
    try:
        ports = _serial_ports()
    except ImportError:
        print("Thiếu pyserial -> pip install pyserial")
        return
    if not ports:
        print("Không thấy cổng COM nào.")
        return
    print("Cổng COM đang có:")
    for port in ports:
        mark = "  <-- USB-UART (dùng cổng này)" if _is_usb_uart(port) else ""
        print(f"  {port.device:<12} {port.description}{mark}")
    print("\nLưu ý: chỉ cổng USB-UART (CP2102/CH340...) mới nối tới STM32.")
    print("       Arduino/Bluetooth/com0com mở được nhưng sẽ KHÔNG nhận dữ liệu từ STM32.")


def _serial_worker(port, baud, device_id, auto=False):
    """Thread nền: mở cổng COM, tự mở lại khi lỗi/rút cáp."""
    try:
        import serial
    except ImportError:
        print("[SERIAL] thiếu pyserial -> pip install pyserial")
        return

    current_port = port
    while True:
        if auto:
            resolved = _autodetect_port()
            if not resolved:
                print(f"[SERIAL] chưa thấy cổng COM nào, thử lại sau {SERIAL_RECONNECT_DELAY:.0f}s")
                time.sleep(SERIAL_RECONNECT_DELAY)
                continue
            current_port = resolved

        try:
            with serial.Serial(current_port, baud, timeout=SERIAL_READ_TIMEOUT) as ser:
                desc = _describe_port(ser.port)
                print(f"[SERIAL] mở {ser.port} @ {baud} 8N1 | {desc or 'không rõ thiết bị'} | "
                      f"device_id mặc định: "
                      f"{device_id or '(chưa set - STM32 gửi RFID:<device_id>,<uid> hoặc ID:<device_id>)'}")
                if desc and not any(h in desc.upper() for h in USB_UART_HINTS):
                    print(f"[SERIAL] CẢNH BÁO: {ser.port} là '{desc}', KHÔNG phải USB-UART "
                          f"(CP2102/CH340...). Chạy `python server.py --list-ports` "
                          f"rồi chọn đúng cổng (hoặc --serial-port auto).")
                # Serial vừa nối lại -> đăng ký fleet với K6 (nếu chưa) + báo online.
                cloud_bridge.set_link(True, device_id)
                _serial_loop(ser, device_id)
        except Exception as exc:
            print(f"[SERIAL] lỗi trên {current_port}: {exc}")

        cloud_bridge.set_link(False)
        time.sleep(SERIAL_RECONNECT_DELAY)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Edge server cho access control: HTTP API + Serial (CP2102/CH340)"
    )
    parser.add_argument("--http-host", default=_env("EDGE_HOST", DEFAULT_HTTP_HOST),
                        help="địa chỉ bind HTTP (mặc định 0.0.0.0)")
    parser.add_argument("--http-port", type=int, default=int(_env("EDGE_PORT", DEFAULT_HTTP_PORT)),
                        help="port HTTP (mặc định 5000)")
    parser.add_argument("--serial-port", default=_env("EDGE_SERIAL_PORT"),
                        help="cổng COM của USB-UART (CP2102/CH340): COM8 | /dev/ttyUSB0 | auto")
    parser.add_argument("--baud", type=int, default=int(_env("EDGE_SERIAL_BAUD", DEFAULT_SERIAL_BAUD)),
                        help=f"baudrate serial (mặc định {DEFAULT_SERIAL_BAUD}, phải khớp firmware STM32)")
    parser.add_argument("--device-id", default=_env("EDGE_DEVICE_ID", STM32_DEVICE_ID),
                        help=f"device_id mặc định cho kênh serial (hardcode = {STM32_DEVICE_ID})")
    parser.add_argument("--list-ports", action="store_true",
                        help="liệt kê cổng COM đang có rồi thoát")
    parser.add_argument("--no-cloud", action="store_true",
                        help="tắt đẩy dữ liệu lên K6 cloud")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.list_ports:
        _print_ports()
        return 0

    db.init_db()
    cloud_bridge.start(disabled=args.no_cloud)

    if args.serial_port:
        auto = args.serial_port.strip().lower() == "auto"
        threading.Thread(
            target=_serial_worker,
            args=(args.serial_port, args.baud, args.device_id, auto),
            name="serial-worker",
            daemon=True,
        ).start()
    else:
        print("[SERIAL] không bật (thêm --serial-port COMx / --serial-port auto để bật)")

    print(f"[HTTP] nghe tại http://{args.http_host}:{args.http_port}  (Ctrl+C để dừng)")
    # use_reloader=False: tránh tiến trình con mở lại cổng COM lần thứ hai
    app.run(host=args.http_host, port=args.http_port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
