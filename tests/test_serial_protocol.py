"""Test protocol serial của edge (không cần cổng COM thật).

Chạy: python -m unittest discover -s tests -v
"""

import os
import shutil
import tempfile
import unittest

import db
import server


class SerialProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="access_control_serial_")
        self.original_db_path = db.DB_PATH
        self.original_audit_root = db.AUDIT_LOG_ROOT
        db.DB_PATH = os.path.join(self.temp_dir, "access_control.db")
        db.AUDIT_LOG_ROOT = os.path.join(self.temp_dir, "data", "log")
        db.init_db()

        db.add_device(device_id="AABBCCDDEEFF", name="Barrier Gate 01")
        db.add_rfid(uid="A1D3E40A9C")
        db.add_rfid(uid="DEADBEEF00")
        db.link_rfid_to_device("AABBCCDDEEFF", "A1D3E40A9C")

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        db.AUDIT_LOG_ROOT = self.original_audit_root
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_parse_gateway_format_device_id_and_uid(self):
        self.assertEqual(
            server.parse_serial_line("RFID:AABBCCDDEEFF,A1D3E40A9C", "OTHER"),
            ("AABBCCDDEEFF", "A1D3E40A9C"),
        )

    def test_parse_keeps_mac_device_id_with_colons(self):
        self.assertEqual(
            server.parse_serial_line("RFID:AA:BB:CC:DD:EE:01,A1D3E40A9C", "OTHER"),
            ("AA:BB:CC:DD:EE:01", "A1D3E40A9C"),
        )

    def test_parse_uid_only_uses_default_device_id(self):
        self.assertEqual(
            server.parse_serial_line("RFID:A1D3E40A9C", "AABBCCDDEEFF"),
            ("AABBCCDDEEFF", "A1D3E40A9C"),
        )

    def test_parse_empty_device_id_falls_back_to_default(self):
        self.assertEqual(
            server.parse_serial_line("RFID: ,A1D3E40A9C", "AABBCCDDEEFF"),
            ("AABBCCDDEEFF", "A1D3E40A9C"),
        )

    def test_parse_tolerates_whitespace_and_crlf(self):
        self.assertEqual(
            server.parse_serial_line("  rfid:A1D3E40A9C \r\n", "AABBCCDDEEFF"),
            ("AABBCCDDEEFF", "A1D3E40A9C"),
        )

    def test_parse_rejects_unknown_command(self):
        self.assertIsNone(server.parse_serial_line("HELLO", "AABBCCDDEEFF"))
        self.assertIsNone(server.parse_serial_line("", "AABBCCDDEEFF"))

    def test_verify_command_without_uid_is_denied_not_ignored(self):
        self.assertEqual(server.parse_serial_line("RFID:", "AABBCCDDEEFF"),
                         ("AABBCCDDEEFF", ""))
        self.assertEqual(server.parse_serial_line("RFID:AABBCCDDEEFF,", None),
                         ("AABBCCDDEEFF", ""))
        self.assertEqual(server.handle_serial_line("RFID:", "AABBCCDDEEFF")[0], "DENY")

    def test_gateway_style_line_end_to_end(self):
        reply, device_id = server.handle_serial_line("RFID:AABBCCDDEEFF,A1D3E40A9C")
        self.assertEqual((reply, device_id), ("GRANT", None))

        reply, _ = server.handle_serial_line("RFID:AABBCCDDEEFF,")
        self.assertEqual(reply, "DENY")

    def test_grant_for_linked_rfid(self):
        reply, device_id = server.handle_serial_line("RFID:A1D3E40A9C", "AABBCCDDEEFF")
        self.assertEqual(reply, "GRANT")
        self.assertEqual(device_id, "AABBCCDDEEFF")

    def test_deny_for_unlinked_rfid(self):
        reply, _ = server.handle_serial_line("RFID:DEADBEEF00", "AABBCCDDEEFF")
        self.assertEqual(reply, "DENY")

    def test_deny_when_device_id_missing(self):
        reply, _ = server.handle_serial_line("RFID:A1D3E40A9C", None)
        self.assertEqual(reply, "DENY")

    def test_ping_and_hello(self):
        self.assertEqual(server.handle_serial_line("PING")[0], "PONG")
        self.assertEqual(server.handle_serial_line("HELLO", "AABBCCDDEEFF")[0],
                         "DEVICE:AABBCCDDEEFF")

    def test_id_command_overrides_default_device_id(self):
        reply, device_id = server.handle_serial_line("ID:AABBCCDDEEFF", None)
        self.assertEqual((reply, device_id), ("OK", "AABBCCDDEEFF"))

        reply, device_id = server.handle_serial_line("RFID:A1D3E40A9C", device_id)
        self.assertEqual((reply, device_id), ("GRANT", "AABBCCDDEEFF"))

    def test_serial_verify_writes_audit_log(self):
        server.handle_serial_line("RFID:A1D3E40A9C", "AABBCCDDEEFF")

        log_dir = os.path.join(db.AUDIT_LOG_ROOT, "AABBCCDDEEFF")
        files = os.listdir(log_dir)
        self.assertEqual(len(files), 1)

        with open(os.path.join(log_dir, files[0]), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("A1D3E40A9C,granted,ok", content)


if __name__ == "__main__":
    unittest.main()
