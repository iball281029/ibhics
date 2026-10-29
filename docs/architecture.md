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
| `ibhics/hypervisor/fake.py` | driver จำลองในหน่วยความจำ ใช้พัฒนาและเทสต์ |
| `ibhics/manager/app.py` | FastAPI app: REST API + Web Console |
| `ibhics/manager/static/` | Web Console |

Manager คุยกับ hypervisor ผ่าน `HypervisorDriver` เท่านั้น เฟสถัดไปจะแยก driver ไปอยู่ใน Agent บนแต่ละ node แล้วให้ Manager เรียกผ่าน API โดยไม่ต้องเปลี่ยนโค้ดฝั่ง REST

## สิ่งที่ทำแล้วในเฟส 1 (รอบนี้)

- [x] Driver interface + libvirt driver + fake driver
- [x] REST API จัดการ VM (สร้าง, เปิด, ปิด, ลบ)
- [x] Web Console เบื้องต้น
- [x] เทสต์และ CI

## งานถัดไปในเฟส 1

- [ ] Agent แยก process + การลงทะเบียน node
- [ ] Cluster 3 node ด้วย etcd
- [ ] Console VM ผ่าน noVNC
- [ ] Live migration
- [ ] ตั้งค่า Ceph อัตโนมัติ
- [ ] Installer ISO + wizard
- [ ] Authentication (login, RBAC)
