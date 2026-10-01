import csv
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import db


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="access_control_")
        self.original_db_path = db.DB_PATH
        self.original_audit_root = db.AUDIT_LOG_ROOT
        db.DB_PATH = os.path.join(self.temp_dir, "access_control.db")
        db.AUDIT_LOG_ROOT = os.path.join(self.temp_dir, "data", "log")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        db.AUDIT_LOG_ROOT = self.original_audit_root
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_csv_audit_log_created_for_device(self):
        device_id = "AA:BB:CC:DD:EE:FF"
        rfid = "A1B2C3D4"
        result = "granted"
        reason = "ok"

        db.log_access(device_id, rfid, result, reason)

        date_dir = datetime.utcnow().strftime("%Y-%m-%d")
        sanitized_device_id = "".join(ch for ch in device_id if ch.isalnum())
        log_path = Path(db.AUDIT_LOG_ROOT) / sanitized_device_id / f"{date_dir}.csv"

        self.assertTrue(log_path.exists(), f"Expected audit log at {log_path}")
        self.assertNotIn(":", sanitized_device_id)
        self.assertNotIn("_", sanitized_device_id)

        with log_path.open("r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        self.assertEqual(rows[0], ["time", "rfid", "result", "reason"])
        self.assertEqual(rows[1], [rows[1][0], rfid, result, reason])
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
