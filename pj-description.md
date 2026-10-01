# dự án Access Control đơn giản:

## hardware:

- stm32f103c8t6 là main module kết nối các device
- stm32f103c8t6 giao tiếp uart với esp32-wroom-32 làm wifi module
- esp32-wroom-32 giao tiếp với edge (python) qua HTTP mạng local nội bộ

## edge

- python, verify uid (rfid) với db (sqlite) và trả lời hardware
- hỗ trợ 2 kênh vào dùng chung 1 logic verify + chung audit log:
  - HTTP qua mạng nội bộ (ESP32 gateway)
  - serial qua USB-CH340 nối trực tiếp STM32 <-> edge (không cần ESP32)
- ghi kết quả vào audit log có path: data/log/{device_id}/{yyyy-MM-dd}.csv, bao gồm các cột time (asc), rfid, result, reason

### db schema

table device {
id varchar pk (mac address)
name varchar unique
disabled boolean default false
}

table rfid {
uid varchar pk
disable
}

table rfid_to_device {
device_id varchar fk(device, id)
rfid_uid varchar fk(rfid, uid)
}

### thông tin thêm

điều kiện access:

- rfid có trong db, disabled = false, AND
- device id có trong db, disabled = false, AND
- rfid liên kết với device
- không nhất thiết phải dùng sqlite, có thể custom 1 class db đơn giản, dùng file txt để lưu trữ

về mạng:

- stm32 <-----uart-----> esp32 <-----wifi / mạng local-----> edge
- esp32 và edge nói chuyện với nhau không phụ thuộc vào internet
