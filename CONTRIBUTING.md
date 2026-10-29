# Contributing to ibHiCS

ยินดีต้อนรับครับ ขั้นตอนร่วมพัฒนามีดังนี้

1. Fork repo แล้วสร้าง branch ใหม่จาก `main`
2. ติดตั้งเครื่องมือ: `pip install -e ".[dev]"`
3. เขียนโค้ดพร้อมเทสต์ แล้วรัน `pytest` และ `ruff check . && ruff format --check .` ให้ผ่าน
4. เปิด Pull Request อธิบายว่าเปลี่ยนอะไรและเพราะอะไร
5. ใน PR แรก ให้คอมเมนต์ยอมรับ [CLA](CLA.md) ตามข้อความนี้:

   > I have read the ibHiCS CLA (version 1.0) and I agree to its terms.

PR จะ merge ได้เมื่อ CI ผ่าน และผู้ร่วมพัฒนาทุกคนใน PR ยอมรับ CLA แล้ว

## แนวทางการเขียนโค้ด

- Python 3.11 ขึ้นไป จัดรูปแบบด้วย ruff (บรรทัดยาวไม่เกิน 100 ตัวอักษร)
- Manager ต้องคุยกับ hypervisor ผ่าน `HypervisorDriver` เท่านั้น
- ฟีเจอร์ใหม่ใน REST API ต้องมีเทสต์ที่รันกับ `FakeDriver` ได้ ไม่ต้องใช้ KVM
- ห้ามนำโค้ด, ภาพ หรือเอกสารที่มีลิขสิทธิ์ของผู้อื่น (รวมถึงซอฟต์แวร์ HCI เชิงพาณิชย์) มาใส่ใน repo
