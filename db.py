"""
db.py - Lớp truy xuất database cho Edge (SQLite)

Schema:
  device          (id: mac address PK, name unique, disabled)
  rfid            (uid PK, disabled)
  rfid_to_device  (device_id FK, rfid_uid FK)

Log schema
  {yyyy-MM-dd}.csv      (lịch sử các lần xác thực)

Điều kiện access hợp lệ:
  - device tồn tại và disabled = False
  - rfid tồn tại và disabled = False
  - rfid được liên kết với device đó trong rfid_to_device
"""

import csv
import sqlite3
import os
import re
import threading
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), 'data/db', "access_control.db")
AUDIT_LOG_ROOT = os.path.join(os.path.dirname(__file__), 'data', 'log')

# SQLite không tự tạo thư mục cha -> đảm bảo có sẵn trước khi mở file
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(AUDIT_LOG_ROOT, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
    id       TEXT PRIMARY KEY,      -- mac address
    name     TEXT UNIQUE NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rfid (
    uid      TEXT PRIMARY KEY,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rfid_to_device (
    device_id TEXT NOT NULL,
    rfid_uid  TEXT NOT NULL,
    PRIMARY KEY (device_id, rfid_uid),
    FOREIGN KEY (device_id) REFERENCES device(id) ON DELETE CASCADE,
    FOREIGN KEY (rfid_uid)  REFERENCES rfid(uid)  ON DELETE CASCADE
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def check_access(device_id: str, rfid_uid: str):
    """
    Trả về (granted: bool, reason: str)
    """
    with get_conn() as conn:
        device = conn.execute(
            "SELECT * FROM device WHERE id = ?", (device_id,)
        ).fetchone()
        if device is None:
            return False, "device_not_found"
        if device["disabled"]:
            return False, "device_disabled"

        rfid = conn.execute(
            "SELECT * FROM rfid WHERE uid = ?", (rfid_uid,)
        ).fetchone()
        if rfid is None:
            return False, "rfid_not_found"
        if rfid["disabled"]:
            return False, "rfid_disabled"

        link = conn.execute(
            "SELECT 1 FROM rfid_to_device WHERE device_id = ? AND rfid_uid = ?",
            (device_id, rfid_uid),
        ).fetchone()
        if link is None:
            return False, "rfid_not_linked_to_device"

        return True, "ok"


def _sanitize_device_id_for_path(device_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", str(device_id or ""))
    return cleaned or "unknown-device"


def _audit_log_path(device_id: str) -> str:
    safe_device_id = _sanitize_device_id_for_path(device_id)
    date_str = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(AUDIT_LOG_ROOT, safe_device_id, f"{date_str}.csv")


# HTTP thread (Flask) và serial worker cùng ghi file -> cần khoá để không đan xen
_AUDIT_LOG_LOCK = threading.Lock()


def log_access(device_id: str, rfid_uid: str, result: str, reason: str):
    timestamp = datetime.now().isoformat(timespec="seconds")

    log_path = _audit_log_path(device_id)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    with _AUDIT_LOG_LOCK:
        file_exists = os.path.exists(log_path)
        with open(log_path, "a", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            if not file_exists:
                writer.writerow(["time", "rfid", "result", "reason"])
            writer.writerow([timestamp, rfid_uid, result, reason])


# ---- Hàm tiện ích cho quản trị (thêm/sửa device, rfid, liên kết) ----

def add_device(device_id: str, name: str, disabled: bool = False):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO device (id, name, disabled) VALUES (?, ?, ?)",
            (device_id, name, int(disabled)),
        )


def add_rfid(uid: str, disabled: bool = False):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rfid (uid, disabled) VALUES (?, ?)",
            (uid, int(disabled)),
        )


def link_rfid_to_device(device_id: str, rfid_uid: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO rfid_to_device (device_id, rfid_uid) VALUES (?, ?)",
            (device_id, rfid_uid),
        )


def set_device_disabled(device_id: str, disabled: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE device SET disabled = ? WHERE id = ?", (int(disabled), device_id)
        )


def set_rfid_disabled(uid: str, disabled: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE rfid SET disabled = ? WHERE uid = ?", (int(disabled), uid)
        )