# ibHiCS

Open-source hyper-converged infrastructure platform.

ibHiCS รวม compute, storage และ network ไว้ในระบบเดียว ติดตั้งบนเซิร์ฟเวอร์ x86 ทั่วไป (Dell, HPE, Lenovo, Supermicro) และจัดการทั้งหมดผ่าน Web Console กับ REST API แนวคิดคล้าย Proxmox VE แต่เน้นประสบการณ์ใช้งานแบบ appliance

> สถานะ: **เฟส 1 (MVP) กำลังพัฒนา** ยังไม่พร้อมใช้งาน production

## สถาปัตยกรรม

| ชั้น | เทคโนโลยี |
| --- | --- |
| Hypervisor | KVM + QEMU ผ่าน libvirt |
| Storage | Ceph RBD (3 node ขึ้นไป), LINSTOR/DRBD (2 node) |
| Network | Open vSwitch + OVN |
| Cluster state | etcd |
| Manager + API | Python (FastAPI) |
| Web Console | HTML/JS (เฟส 1), SPA ภายหลัง |

รายละเอียดอยู่ใน [docs/architecture.md](docs/architecture.md)

## ลองใช้งาน (โหมดพัฒนา ไม่ต้องมี KVM)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m ibhics.manager --driver fake
```

เปิด http://127.0.0.1:8006 เพื่อใช้ Web Console หรือ http://127.0.0.1:8006/docs เพื่อดู API

## ใช้งานกับ KVM จริง

บนเครื่อง Debian 12/13 ที่เปิด VT-x/AMD-V:

```bash
sudo apt install qemu-system-x86 libvirt-daemon-system libvirt-dev pkg-config ovmf
pip install -e ".[libvirt]"
sudo python -m ibhics.manager --driver libvirt --host 0.0.0.0
```

ต้องมี libvirt storage pool ชื่อ `default` และ network ชื่อ `default` (หรือระบุชื่ออื่นตอนสร้าง VM)

## REST API

| Method | Path | ทำอะไร |
| --- | --- | --- |
| GET | `/api/v1/health` | ตรวจสถานะ |
| GET | `/api/v1/host` | ข้อมูล node |
| GET | `/api/v1/vms` | รายการ VM |
| POST | `/api/v1/vms` | สร้าง VM |
| GET | `/api/v1/vms/{uuid}` | ข้อมูล VM |
| POST | `/api/v1/vms/{uuid}/start` | เปิด VM |
| POST | `/api/v1/vms/{uuid}/stop` | ปิด VM (`{"force": true}` = power off) |
| DELETE | `/api/v1/vms/{uuid}` | ลบ VM และดิสก์ |

## พัฒนา

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

## License

ibHiCS ใช้ license [GNU AGPLv3](LICENSE) ใช้งาน ติดตั้ง และแก้ไขได้ฟรี แต่ถ้านำไปแก้แล้วแจกจ่ายหรือเปิดเป็นบริการผ่านเครือข่าย ต้องเปิดซอร์สส่วนที่แก้ภายใต้ AGPLv3 ด้วย

ผู้ร่วมพัฒนาต้องยอมรับ [Contributor License Agreement](CLA.md) ก่อน PR แรกจะถูก merge ดูขั้นตอนใน [CONTRIBUTING.md](CONTRIBUTING.md)
