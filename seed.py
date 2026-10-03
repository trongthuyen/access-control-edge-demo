"""
seed.py - Thêm dữ liệu mẫu để test nhanh.

Chạy: python seed.py
"""

import db

if __name__ == "__main__":
    db.init_db()

    # device_id nên là MAC address thật của STM32 (in.println(WiFi.macAddress())
    # để lấy khi STM32 boot lên, xem log Serial)
    db.add_device(device_id="421108000003", name="Barrier Gate 01")

    db.add_rfid(uid="A1D3E40A9C")
    db.add_rfid(uid="3149970AE5")

    db.link_rfid_to_device(device_id="421108000003", rfid_uid="A1D3E40A9C")

    print("Đã thêm dữ liệu mẫu.")
