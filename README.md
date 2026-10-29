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
python -m ibhics.manager --driver fake --state-file ./.ibhics/cluster.json
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

## รูปแบบการติดตั้ง

| รูปแบบ | เครื่อง | HA |
| --- | --- | --- |
| Standalone | 1 node | ไม่มี |
| 2 node + witness | 2 compute + witness 1 เครื่อง (mini PC หรือ VM ที่อื่น) | ทน node ล่มได้ 1 เครื่อง |
| Cluster | 3 compute ขึ้นไป | มี |

Manager รันบน compute node แรก ส่วนเครื่องอื่นรัน Agent ทุกเครื่องใช้ token เดียวกัน:

```bash
export IBHICS_CLUSTER_TOKEN="$(openssl rand -hex 32)"   # ใช้ค่าเดียวกันทุกเครื่อง

# node1 (manager + compute)
sudo -E python -m ibhics.manager --driver libvirt --node-name node1 --host 0.0.0.0

# node2 (compute)
sudo -E python -m ibhics.agent --role compute --name node2

# witness
python -m ibhics.agent --role witness --name witness
```

จากนั้นเพิ่ม node ในหน้า Web Console (ส่วน Nodes) หรือผ่าน API:

```bash
curl -X POST http://node1:8006/api/v1/cluster/nodes -H 'content-type: application/json' \
  -d '{"name": "node2", "role": "compute", "address": "http://10.0.0.12:8007"}'
```

ถ้าสมาชิกที่ online เหลือไม่ถึงครึ่ง (เช่น 2 node + witness แต่ล่มไป 2 เครื่อง) ระบบจะเสีย quorum และไม่ยอมให้สร้าง เปิด ปิด หรือลบ VM เพื่อกัน split-brain แต่ยังดูสถานะได้ตามปกติ

## Storage

แต่ละ node มี storage pool ของตัวเอง เพิ่มได้จากส่วน Storage ในหน้า Web Console หรือผ่าน API

| ชนิด | ใช้กับ | ดิสก์ VM |
| --- | --- | --- |
| `dir` | ดิสก์ในเครื่อง (ค่าเริ่มต้น `/var/lib/ibhics/pools/<ชื่อ>`) | qcow2 |
| `nfs` | NAS / NFS server | qcow2 |
| `iscsi` | iSCSI target: login แล้วสร้าง LVM บน LUN ที่เลือก | raw (LVM) |
| `lvm` | FC LUN ผ่าน multipath (`/dev/mapper/mpathX`) หรือดิสก์ในเครื่อง | raw (LVM) |

`iscsi` และ `lvm` จะลบข้อมูลบน device จึงต้องส่ง `"confirm_wipe": true` ส่วน FC ต้องตั้งค่า multipath บนเครื่องให้เห็น device ก่อน ถ้าใช้ storage ร่วมกันหลาย node (เช่น NFS ตัวเดียวกัน) ให้เพิ่ม pool ชื่อเดียวกันในทุก node ที่ต้องเข้าถึง LVM บน LUN เดียวกันจากหลาย node พร้อมกันยังไม่รองรับในรอบนี้ (ต้องใช้ lvmlockd)

```bash
curl -X POST http://node1:8006/api/v1/nodes/node1/pools -H 'content-type: application/json' \
  -d '{"name": "nas01", "type": "nfs", "host": "10.0.0.50", "export": "/vol/vms"}'
```

## Backup

backup ทำบน node ที่ VM อยู่ แล้วเก็บไว้ที่ปลายทางที่เลือก

| ชนิดปลายทาง | ใช้กับ |
| --- | --- |
| `local` | ดิสก์ใน node (ค่าเริ่มต้น `/var/lib/ibhics/backup/<ชื่อ>`) แต่ละ node เก็บของตัวเอง |
| `nfs` | NAS / NFS server |
| `smb` | Windows share หรือ NAS ผ่าน SMB/CIFS (รหัสผ่านส่งผ่านไฟล์ ไม่โผล่ใน process list) |
| `usb` | USB disk ที่ format แล้ว: mount ตอนเริ่มงาน และ unmount เมื่อเสร็จ จึงถอดเก็บนอกสถานที่ได้ |
| `s3` | AWS S3, MinIO, Ceph RGW (ต้องติดตั้ง `pip install 'ibhics[s3]'`) |

- **Backup ทั้ง VM ขณะเปิดอยู่ได้**: ใช้ push-mode backup ของ QEMU ได้ภาพดิสก์ ณ จุดเวลาเดียวกันทุกดิสก์ ส่วน VM ที่ปิดอยู่จะ copy volume ตรง
- **ใช้พื้นที่แบบ incremental**: แบ่งดิสก์เป็นก้อน 4 MB เก็บเฉพาะก้อนที่ยังไม่มี (dedup) และไม่เก็บก้อนที่เป็นศูนย์ ทุกชุดจึง restore ได้เต็ม VM แต่ชุดถัดไปใช้พื้นที่เท่าข้อมูลที่เปลี่ยน
- **ตั้งเวลา**: ทุกวันตามเวลาที่กำหนด หรือทุก N ชั่วโมง เลือก VM หรือทุก VM และเก็บไว้ N ชุดล่าสุด ชุดเก่าจะถูกลบ และคืนพื้นที่ที่ไม่มีชุดไหนใช้แล้ว
- **Restore**: กลับเป็น VM เดิม (ถ้าลบไปแล้ว) หรือเป็นสำเนาชื่อใหม่ เลือก node และ pool ปลายทางได้
- ตรวจ checksum ทุกก้อนตอน restore ค่าตั้งปลายทางพร้อมรหัสผ่านเก็บใน `/var/lib/ibhics/backup.json` (อ่านได้เฉพาะ root) และ API แสดงรหัสผ่านเป็น `********`

```bash
curl -X POST http://node1:8006/api/v1/backup/targets -H 'content-type: application/json' \
  -d '{"name": "nas", "type": "nfs", "host": "10.0.0.50", "export": "/backup"}'
curl -X POST http://node1:8006/api/v1/backup/policies -H 'content-type: application/json' \
  -d '{"name": "nightly", "target": "nas", "at": "02:00", "keep_last": 14}'
```

## REST API

| Method | Path | ทำอะไร |
| --- | --- | --- |
| GET | `/api/v1/health` | ตรวจสถานะ |
| GET | `/api/v1/cluster` | รูปแบบ cluster, quorum, สมาชิก |
| POST | `/api/v1/cluster/nodes` | เพิ่ม node (compute หรือ witness) |
| DELETE | `/api/v1/cluster/nodes/{name}` | นำ node ออก |
| GET | `/api/v1/nodes/{name}/host` | ข้อมูลฮาร์ดแวร์ของ node |
| GET | `/api/v1/pools` | storage pool ทุก node |
| POST | `/api/v1/nodes/{name}/pools` | เพิ่ม pool (`dir`, `nfs`, `iscsi`, `lvm`) |
| DELETE | `/api/v1/nodes/{name}/pools/{pool}` | นำ pool ออก (ข้อมูลบน storage ยังอยู่) |
| GET | `/api/v1/vms` | รายการ VM ทุก node |
| POST | `/api/v1/vms` | สร้าง VM (`node` ว่าง = เลือก node ให้อัตโนมัติ) |
| GET | `/api/v1/vms/{uuid}` | ข้อมูล VM |
| POST | `/api/v1/vms/{uuid}/start` | เปิด VM |
| POST | `/api/v1/vms/{uuid}/stop` | ปิด VM (`{"force": true}` = power off) |
| DELETE | `/api/v1/vms/{uuid}` | ลบ VM และดิสก์ |
| POST | `/api/v1/vms/{uuid}/backup` | backup ทันที (`{"target": "nas"}`) |
| GET/POST | `/api/v1/backup/targets` | ปลายทาง backup |
| DELETE | `/api/v1/backup/targets/{name}` | นำปลายทางออก (ไฟล์ backup ยังอยู่) |
| GET | `/api/v1/backup/targets/{name}/backups` | backup ที่อยู่บนปลายทาง |
| POST | `/api/v1/backup/targets/{name}/backups/{id}/restore` | restore (`name`, `node`, `pool`, `source_node`) |
| DELETE | `/api/v1/backup/targets/{name}/backups/{id}` | ลบ backup (`?node=` สำหรับปลายทาง local) |
| GET/POST | `/api/v1/backup/policies` | ตารางเวลา backup |
| POST | `/api/v1/backup/policies/{name}/run` | รัน policy ทันที |
| GET | `/api/v1/backup/jobs` | งาน backup/restore ล่าสุดและผลลัพธ์ |

## พัฒนา

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

## License

ibHiCS ใช้ license [GNU AGPLv3](LICENSE) ใช้งาน ติดตั้ง และแก้ไขได้ฟรี แต่ถ้านำไปแก้แล้วแจกจ่ายหรือเปิดเป็นบริการผ่านเครือข่าย ต้องเปิดซอร์สส่วนที่แก้ภายใต้ AGPLv3 ด้วย

ผู้ร่วมพัฒนาต้องยอมรับ [Contributor License Agreement](CLA.md) ก่อน PR แรกจะถูก merge ดูขั้นตอนใน [CONTRIBUTING.md](CONTRIBUTING.md)
