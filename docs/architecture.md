# ibHiCS Architecture

เอกสารออกแบบฉบับเต็ม (สถาปัตยกรรม, เทียบโมดูล, roadmap, ibHiCS Migrate):
https://claude.ai/artifact/TsWFgSoWhKdC2X16JG7zdK

## ชั้นของระบบ

```
Web Console / REST API / CLI
            │
     ibHiCS Manager  ── etcd (cluster state)
            │
   ibHiCS Agent (ทุก node)
   ├── Compute : KVM + QEMU + libvirt
   ├── Storage : Ceph RBD / LINSTOR
   ├── Network : Open vSwitch + OVN
   └── Security: OVN ACL, Suricata, Wazuh
            │
     ibHiCS OS (Debian, A/B root)
            │
   Dell · HPE · Lenovo · Supermicro (Redfish/IPMI)
```

## โครงโค้ด

| Path | หน้าที่ |
| --- | --- |
| `ibhics/hypervisor/base.py` | interface `HypervisorDriver` และ data model (VMSpec, VMInfo) |
| `ibhics/hypervisor/libvirt_driver.py` | driver สำหรับ KVM ผ่าน libvirt |
| `ibhics/hypervisor/domain_xml.py` | แปลง VMSpec เป็น libvirt domain/volume XML |
| `ibhics/hypervisor/pool_xml.py` | แปลง storage pool (dir, NFS, iSCSI, LVM/FC) เป็น libvirt pool XML |
| `ibhics/hypervisor/fake.py` | driver จำลองในหน่วยความจำ ใช้พัฒนาและเทสต์ |
| `ibhics/cluster/topology.py` | รูปแบบ cluster (standalone, 2 node + witness, cluster) และกฎ quorum |
| `ibhics/cluster/registry.py` | สมาชิก cluster, heartbeat, เลือก node วาง VM |
| `ibhics/cluster/remote.py` | client คุยกับ Agent และ `RemoteDriver` |
| `ibhics/agent/app.py` | Agent บนแต่ละ node (role compute หรือ witness) |
| `ibhics/manager/app.py` | FastAPI app: REST API + Web Console |
| `ibhics/manager/static/` | Web Console |

Manager คุยกับ hypervisor ผ่าน `HypervisorDriver` เท่านั้น node ที่ manager รันอยู่ใช้ driver ตรง ส่วน node อื่นใช้ `RemoteDriver` ที่เรียก Agent ผ่าน HTTP (ยืนยันตัวด้วย cluster token)

## Quorum

ทุกสมาชิก (compute และ witness) มี 1 เสียง ต้องมีสมาชิก online เกินครึ่งจึงจะสั่งงานที่เปลี่ยนสถานะได้ 2 node + witness จึงทนเครื่องล่มได้ 1 เครื่อง ตอนนี้ quorum คำนวณจาก heartbeat ที่ manager เห็น ขั้นต่อไปจะย้ายข้อมูลสมาชิกไปเก็บใน etcd 3 สมาชิก (node1, node2, witness) และทำ manager ให้ย้ายไป node อื่นได้เมื่อ node1 ล่ม

## สิ่งที่ทำแล้วในเฟส 1

- [x] Driver interface + libvirt driver + fake driver
- [x] REST API จัดการ VM (สร้าง, เปิด, ปิด, ลบ)
- [x] Web Console เบื้องต้น
- [x] เทสต์และ CI
- [x] Agent แยก process + การลงทะเบียน node (compute, witness)
- [x] Standalone, 2 node + witness, cluster 3 node ขึ้นไป + quorum
- [x] Storage pool: local, NFS, iSCSI, FC (multipath + LVM)

## งานถัดไปในเฟส 1

- [ ] iSCSI CHAP, LVM ร่วมหลาย node (lvmlockd), DRBD สำหรับ 2 node
- [ ] Backup ในตัว + ส่งออก NFS, SMB, USB, S3
- [ ] etcd เป็นที่เก็บสมาชิก cluster + manager ย้าย node ได้
- [ ] Console VM ผ่าน noVNC
- [ ] Live migration
- [ ] ตั้งค่า Ceph อัตโนมัติ
- [ ] Installer ISO + wizard
- [ ] Authentication (login, RBAC)
