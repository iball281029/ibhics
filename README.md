# ibHiCS

Open-source hyper-converged infrastructure platform.

ibHiCS รวม compute, storage และ network ไว้ในระบบเดียว ติดตั้งบนเซิร์ฟเวอร์ x86 ทั่วไป (Dell, HPE, Lenovo, Supermicro) และจัดการทั้งหมดผ่าน Web Console กับ REST API แนวคิดคล้าย Proxmox VE แต่เน้นประสบการณ์ใช้งานแบบ appliance

> สถานะ: **ibHiCS Hyper-Converged Infrastructure beta version 0.1** (`0.1.0b1`) รุ่นทดลองใช้ ยังไม่แนะนำสำหรับ production

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
IBHICS_ADMIN_PASSWORD=change-me-now python -m ibhics.manager --driver fake \
  --state-file ./.ibhics/cluster.json --backup-state-file ./.ibhics/backup.json \
  --users-file ./.ibhics/users.json --audit-log ./.ibhics/audit.log
```

เปิด http://127.0.0.1:8888 แล้ว sign in ด้วย `admin` / `change-me-now` (ระบบจะให้เปลี่ยนรหัสก่อน) เพื่อใช้ Web Console หรือ http://127.0.0.1:8888/docs เพื่อดู API

## ติดตั้งจาก ISO

ISO ของ ibHiCS คือ Debian 13 ที่ติดตั้งอัตโนมัติ พร้อม KVM, libvirt, ตัว client ของ storage (NFS, SMB, iSCSI, multipath) และ ibHiCS เอง

1. **ดาวน์โหลด ISO** จาก [Releases](https://github.com/iball281029/ibhics/releases) หรือจาก artifact `ibhics-installer-iso` ของ workflow "Installer ISO" ในแท็บ Actions หรือสร้างเองด้วย `installer/build-iso.sh` (ต้องมี `xorriso` และ `docker`)
2. **เขียนลง USB** (`dd if=ibhics-0.1-beta-amd64.iso of=/dev/sdX bs=4M`, Rufus หรือ Ventoy) หรือเมานต์ผ่าน iDRAC, iLO หรือ XClarity แล้วบูต ใช้ได้ทั้ง BIOS และ UEFI
3. **เลือก "Install ibHiCS"** เมนูนี้**ลบข้อมูลทั้งหมดในดิสก์ลูกแรก**โดยไม่ถาม แล้วติดตั้งเองจนจบ ถ้าต้องการเลือกดิสก์เอง ให้เลือกเมนูที่สอง **ไม่ต้องต่อเน็ตและไม่ต้องมี DHCP** เพราะแพ็กเกจทั้งหมดอยู่บน ISO ตัวติดตั้งไม่ถามเรื่อง network เลย
4. **หลังรีบูต** หน้าจอ console จะถาม:
   - node นี้คืออะไร: node แรก (manager + compute), compute เพิ่มเติม หรือ witness
   - ชื่อ node, พอร์ตแลน (พอร์ตที่เสียบสายอยู่จะขึ้นก่อน), IP/prefix, gateway และ DNS (ถ้ามี DHCP จะเติมค่าแนะนำให้)
   - time zone, รหัสผ่าน root และ (เฉพาะ node แรก) รหัสผ่านผู้ใช้ `admin` ของหน้าเว็บ
   - token ของ cluster: node แรกจะสร้างให้และแสดงบนจอ node อื่นต้องกรอก token นี้

   จากนั้นระบบตั้งค่าให้ทั้งหมด:
   - bridge `br0` ให้ VM อยู่ใน LAN เดียวกับ node
   - storage pool `default` ที่ `/var/lib/ibhics`
   - ใบรับรอง HTTPS (self-signed)
   - เปิด service ของ ibHiCS
5. **ใช้งาน**: เปิด `https://<IP ของ node แรก>:8888` แล้วเพิ่ม node อื่นที่เมนู Nodes ด้วย `http://<IP>:8007`

ตั้งค่าใหม่ทีหลังได้ด้วย `ibhics-setup`

**ข้อจำกัดของรุ่น beta**:
- agent คุยกับ manager ผ่าน HTTP ในวง management (มี token ยืนยันตัวตน แต่ยังไม่เข้ารหัส)
- live migration ต้องตั้ง libvirt TLS หรือ SSH key ระหว่าง node เอง ดูหัวข้อ Live migration
- ยังไม่ได้ทดสอบบูตบนเครื่องจริงทุกรุ่น

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
curl -X POST http://node1:8888/api/v1/cluster/nodes -H 'content-type: application/json' \
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
curl -X POST http://node1:8888/api/v1/nodes/node1/pools -H 'content-type: application/json' \
  -d '{"name": "nas01", "type": "nfs", "host": "10.0.0.50", "export": "/vol/vms"}'
```

## Backup

backup ทำบน node ที่ VM อยู่ แล้วเก็บไว้ที่ปลายทางที่เลือก

| ชนิดปลายทาง | ใช้กับ |
| --- | --- |
| `local` | ดิสก์ใน node (ค่าเริ่มต้น `/var/lib/ibhics/backup/<ชื่อ>`) แต่ละ node เก็บของตัวเอง |
| `nfs` | NAS / NFS server |
| `smb` | Windows share หรือ NAS ผ่าน SMB/CIFS (รหัสผ่านส่งผ่านไฟล์ ไม่โผล่ใน process list) |
| `usb` | USB disk ที่ format แล้ว เสียบไว้ที่ node (ใช้ได้เฉพาะ node นั้น) ถอดเก็บนอกสถานที่ได้ |
| `iscsi` | LUN จาก SAN หรือ NAS ผ่าน iSCSI: login ตอนเริ่มงาน และ logout เมื่อเสร็จ |
| `fc` | LUN ผ่าน Fibre Channel (`/dev/mapper/mpathX`) หรือ block device อื่น |
| `s3` | AWS S3, MinIO, Ceph RGW (ต้องติดตั้ง `pip install 'ibhics[s3]'`) |

- **ต่อเฉพาะตอนรัน job**: NFS, SMB, USB, iSCSI และ FC ถูก mount ตอนเริ่ม job และถอดออกเมื่อจบ ไม่ว่าสำเร็จหรือล้มเหลว (iSCSI logout ด้วย เครื่องจึงมองไม่เห็น LUN ระหว่างไม่มีงาน) ถ้าเครื่องดับระหว่าง job ตอนเปิดเครื่อง agent จะถอดของที่ค้างให้ ส่วน FC ถูก unmount แต่ LUN ยังต่ออยู่ที่ HBA ถ้าต้องตัดขาดจริงให้ทำ LUN masking ที่ storage
- **LUN ใหม่**: ถ้า LUN ของ iSCSI/FC ยังไม่มี filesystem ระบบจะ format เป็น XFS ให้เมื่อส่ง `"confirm_wipe": true` และใช้ได้ทีละ node ระบบจึงจัดคิวงานบนปลายทางเหล่านี้ไม่ให้ซ้อนกันทั้ง cluster
- **Backup ทั้ง VM ขณะเปิดอยู่ได้**: ใช้ push-mode backup ของ QEMU ได้ภาพดิสก์ ณ จุดเวลาเดียวกันทุกดิสก์ ส่วน VM ที่ปิดอยู่จะ copy volume ตรง
- **ใช้พื้นที่แบบ incremental**: แบ่งดิสก์เป็นก้อน 4 MB เก็บเฉพาะก้อนที่ยังไม่มี (dedup) และไม่เก็บก้อนที่เป็นศูนย์ ทุกชุดจึง restore ได้เต็ม VM แต่ชุดถัดไปใช้พื้นที่เท่าข้อมูลที่เปลี่ยน
- **ตั้งเวลา**: ทุกวันตามเวลาที่กำหนด หรือทุก N ชั่วโมง เลือก VM หรือทุก VM และเก็บไว้ N ชุดล่าสุด ชุดเก่าจะถูกลบ และคืนพื้นที่ที่ไม่มีชุดไหนใช้แล้ว
- **Restore**: กลับเป็น VM เดิม (ถ้าลบไปแล้ว) หรือเป็นสำเนาชื่อใหม่ เลือก node และ pool ปลายทางได้
- ตรวจ checksum ทุกก้อนตอน restore ค่าตั้งปลายทางพร้อมรหัสผ่านเก็บใน `/var/lib/ibhics/backup.json` (อ่านได้เฉพาะ root) และ API แสดงรหัสผ่านเป็น `********`

```bash
curl -X POST http://node1:8888/api/v1/backup/targets -H 'content-type: application/json' \
  -d '{"name": "nas", "type": "nfs", "host": "10.0.0.50", "export": "/backup"}'
curl -X POST http://node1:8888/api/v1/backup/policies -H 'content-type: application/json' \
  -d '{"name": "nightly", "target": "nas", "at": "02:00", "keep_last": 14}'
```

## Web console

หน้าเว็บ (ภาษาอังกฤษ) มีเมนูแถบซ้ายแบบ UniFi: Dashboard, Virtual Machines, Storage, Nodes, Backup, Migrate และด้านล่างเป็น Activity, API reference, Settings มีทั้งธีมสว่างและมืด หน้า Dashboard แสดงสถานะรวมของ cluster, ภาพรวม node → storage → VM → backup และการใช้ CPU, หน่วยความจำ และ storage ปุ่มที่แก้ไขระบบจะแสดงตามสิทธิ์ของผู้ใช้ (viewer, operator, admin)

### Inventory ของ VM

หน้า Virtual Machines แสดงระบบปฏิบัติการ, IP address และ hostname ของแต่ละ VM ค้นหาได้ด้วยชื่อ, OS, hostname หรือ IP กดชื่อ VM หรือปุ่ม **Summary** เพื่อเปิดหน้ารายละเอียด (`#vm/<uuid>`) ซึ่งมีแท็บ Summary, Hardware, Backups (restore point ของ VM นี้จากทุก target) และ Activity

- **Operating system**: ป้ายที่ผู้ดูแลกรอกตอนสร้าง VM (เก็บใน `<metadata>` ของ domain XML) VM ที่ import ผ่าน Migrate จะได้ค่าจาก VMware/OVF ให้อัตโนมัติ
- **ข้อมูลจาก guest**: ถ้าติดตั้ง `qemu-guest-agent` ใน VM (Linux: แพ็กเกจ `qemu-guest-agent`, Windows: VirtIO guest tools) ibHiCS จะอ่านชื่อและเวอร์ชัน OS, kernel, hostname และ IP ของทุก interface มาแสดงเอง ถ้าไม่มี agent จะแสดง IP จาก DHCP lease ของ libvirt เท่าที่มี
- API: `GET /api/v1/vms/guests` (ทุก VM ที่เปิดอยู่) และ `GET /api/v1/vms/{uuid}/guest`

## Console ของ VM

กดปุ่ม **Console** ที่ VM ที่เปิดอยู่ จะเปิดหน้าจอของ VM ในเบราว์เซอร์ (noVNC) มีปุ่ม Ctrl+Alt+Del, วางข้อความ (พิมพ์ข้อความจาก clipboard ลงใน VM) และเต็มจอ

VNC ของทุก VM ฟังเฉพาะ `127.0.0.1` ของ node ตัวเอง เบราว์เซอร์คุยกับ manager ผ่าน WebSocket `/api/v1/vms/{uuid}/console` แล้ว manager ส่งต่อไปยัง VNC บน node ของตัวเอง หรือผ่าน agent ของ node ที่ VM อยู่ (ยืนยันด้วย cluster token) จึงไม่ต้องเปิดพอร์ต VNC ออกนอกเครื่อง โหมดพัฒนา (`--driver fake`) มี VNC จำลองแสดงภาพทดสอบ

## ผู้ใช้และสิทธิ์

ทุกหน้าของ web console และทุก API (ยกเว้น `/api/v1/health`) ต้อง sign in ก่อน

- **ครั้งแรก**: manager สร้างผู้ใช้ `admin` ให้ รหัสผ่านเริ่มต้นตั้งได้ด้วย `IBHICS_ADMIN_PASSWORD` ถ้าไม่ตั้ง ระบบจะสุ่มรหัสให้และเขียนไว้ที่ `/var/lib/ibhics/initial-admin-password` (อ่านได้เฉพาะ root ไม่แสดงใน log) sign in ครั้งแรกต้องเปลี่ยนรหัสก่อนใช้งานอย่างอื่น
- **สิทธิ์**:

| Role | ทำอะไรได้ |
| --- | --- |
| `viewer` | ดูได้ทุกอย่าง แก้ไขไม่ได้ |
| `operator` | จัดการ VM: สร้าง ลบ เปิด/ปิด console backup restore และ migrate |
| `admin` | ทำได้ทุกอย่าง รวมถึง node, storage, ปลายทาง/ตารางเวลา backup, evacuate, ผู้ใช้ และ audit log |

- **Session**: browser ได้ cookie แบบ HttpOnly + SameSite=Strict หมดอายุเมื่อไม่ใช้งาน 30 นาที หรือครบ 12 ชั่วโมง ส่วน script ใช้ token ที่ได้จาก login ส่งเป็น `Authorization: Bearer <token>` ถ้า restart manager ทุกคนต้อง sign in ใหม่
- **ป้องกันการเดารหัส**: ใส่รหัสผิด 5 ครั้งภายใน 5 นาที บัญชีนั้นถูกล็อก 5 นาที รหัสผ่านเก็บแบบ scrypt hash ใน `/var/lib/ibhics/users.json` (สิทธิ์ 600)
- **Audit log**: ทุกการ sign in และทุกการแก้ไข (ใคร ทำอะไร ผลเป็นอย่างไร มาจาก IP ไหน) บันทึกไว้ที่ `/var/log/ibhics/audit.log` และดูได้ที่ `GET /api/v1/audit`
- เมื่อใช้งานจริง ควรเปิด manager หลัง HTTPS (reverse proxy) เพื่อให้ cookie ส่งแบบ Secure

```bash
# script: sign in แล้วใช้ token
TOKEN=$(curl -s localhost:8888/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"..."}' | jq -r .token)
curl -H "Authorization: Bearer $TOKEN" localhost:8888/api/v1/vms
```

## Live migration

ย้าย VM ไปอีก node โดย VM ยังทำงานต่อ (ผู้ใช้แทบไม่รู้สึก) ใช้ live migration ของ libvirt แบบ peer-to-peer: libvirtd ของ node ต้นทางคุยกับ node ปลายทางโดยตรง เมื่อย้ายเสร็จ VM จะถูก define ที่ปลายทางและลบออกจากต้นทาง จึงไม่มีช่วงที่ VM อยู่สองที่

```bash
# ย้ายไป node2 (ไม่ใส่ node = เลือก node ที่หน่วยความจำว่างมากที่สุดให้)
curl -X POST localhost:8888/api/v1/vms/<uuid>/migrate -H 'Content-Type: application/json' \
  -d '{"node": "node2", "bandwidth_mib": 500}'
curl localhost:8888/api/v1/migrations          # ดูความคืบหน้า

# ก่อนปิด node ไปซ่อม: ย้าย VM ทุกตัวออกจาก node2
curl -X POST localhost:8888/api/v1/nodes/node2/evacuate
```

ก่อนย้าย manager ตรวจให้ ถ้าไม่ผ่านจะบอกเหตุผล:

- ต้องมี quorum ระหว่างนั้นต้องไม่มี backup ของ VM ตัวนี้ทำงานอยู่ และ VM ตัวนี้ต้องไม่ได้กำลังถูกย้ายอยู่แล้ว
- node ปลายทางต้องมี storage pool ชื่อเดียวกับที่ดิสก์ของ VM อยู่ และต้องมีหน่วยความจำว่างพอ
- **Storage ที่ใช้ร่วมกัน** (NFS, iSCSI, FC/LVM ที่ชี้ไปยังแหล่งเดียวกันทั้งสอง node) ย้ายเฉพาะหน่วยความจำ จึงเร็ว
- **ดิสก์ local** (pool แบบ `dir`) ระบบจะคัดลอกดิสก์ไป pool ชื่อเดียวกันที่ปลายทางขณะ VM ทำงาน แล้วลบของเดิม ใช้เวลานานตามขนาดดิสก์ ถ้าไม่ต้องการให้คัดลอก ส่ง `"copy_storage": false`
- VM ที่ปิดอยู่ย้ายได้เฉพาะเมื่อดิสก์อยู่บน storage ที่ใช้ร่วมกัน (ย้ายแค่ definition)
- `evacuate` ย้ายทุกตัวที่ย้ายได้ ตัวที่ย้ายไม่ได้จะขึ้นเป็น job ที่ `failed` พร้อมเหตุผล

ให้ libvirtd ของแต่ละ node เชื่อมกันได้ เลือกด้วย `--migration-transport` (หรือ `IBHICS_MIGRATION_TRANSPORT`):

| ค่า | ต้องเตรียม |
| --- | --- |
| `tls` (ค่าเริ่มต้น) | ใบรับรอง libvirt TLS ของ cluster บนทุก node และเปิด `libvirtd-tls.socket` (พอร์ต 16514) |
| `ssh` | กุญแจ SSH ของ root ระหว่าง node |
| `tcp` | ไม่เข้ารหัส ใช้ในแล็บเท่านั้น |

ชื่อ host ของ node ปลายทางมาจาก address ของ agent ส่วน node ที่รัน manager ใช้ `--migration-host` (ค่าเริ่มต้นคือ FQDN ของเครื่อง) ตอนนี้ VM ใช้ CPU model มาตรฐานของ QEMU จึงย้ายข้าม node ที่ใช้ CPU ต่างรุ่นกันได้

## ibHiCS Migrate (ย้าย VM เข้ามาจากระบบอื่น)

นำ VM จากระบบเดิมเข้ามาใน ibHiCS โดยไม่ต้องติดตั้ง agent ในเครื่องต้นทาง ใช้ [virt-v2v](https://libguestfs.org/virt-v2v.1.html) แปลงดิสก์และปรับระบบปฏิบัติการใน VM ให้บูตบน KVM ได้ ได้แก่ ติดตั้ง driver virtio ถอด VMware Tools หรือ Hyper-V integration services และแก้ boot loader

| แหล่ง (`type`) | ใช้กับ | ต้องกรอก |
| --- | --- | --- |
| `vmware` | ESXi หรือ vCenter | `host`, `username`, `password`, และ `path` เมื่อผ่าน vCenter (เช่น `DC1/host/Cluster1/esx1.local`) |
| `ova` | ไฟล์ OVA ที่ export จาก VMware, VirtualBox ฯลฯ | `path` ของโฟลเดอร์ |
| `disk` | ไฟล์ดิสก์ VMDK, VHDX/VHD (Hyper-V), QCOW2, RAW (1 ไฟล์ = 1 VM) | `path` ของโฟลเดอร์ |

โฟลเดอร์ต้องเป็น path ที่ node เห็น เช่น NFS หรือ SMB share ที่เมานต์ไว้ที่ path เดียวกันทุก node

```bash
# 1. เพิ่มแหล่ง (admin)
curl -X POST .../api/v1/migrate/sources -d '{"name":"esx1","type":"vmware","host":"10.0.0.50","username":"root","password":"..."}'
# 2. ดู VM ที่อยู่บนแหล่งนั้น
curl .../api/v1/migrate/sources/esx1/vms
# 3. สั่งย้าย (ปิด VM ต้นทางก่อนเพื่อให้ข้อมูลตรงกัน)
curl -X POST .../api/v1/migrate/jobs -d '{"source":"esx1","vm":"win2019-db","pool":"default","start":true}'
curl .../api/v1/migrate/jobs     # ความคืบหน้า
```

ระบบจะแปลงดิสก์ให้เสร็จก่อน แล้วจึงสร้าง VM ใน ibHiCS โดยใช้จำนวน CPU, RAM, firmware (BIOS/UEFI) และจำนวน NIC ตามต้นทาง (เปลี่ยนได้ด้วย `vcpus`, `memory_mb`, `name`) ถ้าล้มเหลวจะไม่มี VM ค้างอยู่ รหัสผ่านของแหล่งเก็บใน `/var/lib/ibhics/migrate.json` (อ่านได้เฉพาะ root) และ API แสดงเป็น `********`

**ข้อควรรู้**:
- Windows ต้องมี driver virtio-win บน node (`/usr/share/virtio-win`) ซึ่ง Debian ไม่มีให้ ต้องดาวน์โหลดจาก Fedora
- ระหว่างย้ายต้นทางควรปิดอยู่ ยังไม่รองรับการซิงก์ขณะเปิดแบบ Nutanix Move (warm migration)
- ยังไม่รองรับเครื่อง physical (P2V) และ Sangfor HCI โดยตรง ให้ export เป็น OVA หรือดิสก์ก่อน

## REST API

| Method | Path | ทำอะไร |
| --- | --- | --- |
| GET | `/api/v1/health` | ตรวจสถานะ (ไม่ต้อง sign in) |
| POST | `/api/v1/auth/login` | sign in (`username`, `password`) ได้ cookie และ token |
| POST | `/api/v1/auth/logout` | sign out |
| GET | `/api/v1/auth/me` | ผู้ใช้ปัจจุบัน |
| POST | `/api/v1/auth/password` | เปลี่ยนรหัสผ่านตัวเอง |
| GET/POST | `/api/v1/users` | ผู้ใช้ (admin) |
| PATCH/DELETE | `/api/v1/users/{name}` | เปลี่ยน role, ปิดบัญชี, ตั้งรหัสใหม่, ลบ (admin) |
| GET | `/api/v1/audit` | audit log (admin) |
| GET/POST | `/api/v1/migrate/sources` | แหล่งของ ibHiCS Migrate (เพิ่ม/ลบ: admin) |
| GET | `/api/v1/migrate/sources/{name}/vms` | VM ที่อยู่บนแหล่ง |
| GET/POST | `/api/v1/migrate/jobs` | สั่งย้าย VM เข้ามา และดูสถานะ |
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
| POST | `/api/v1/vms/{uuid}/migrate` | live migration (`node`, `copy_storage`, `bandwidth_mib`) |
| GET | `/api/v1/migrations` | งานย้าย VM และผลลัพธ์ |
| POST | `/api/v1/nodes/{name}/evacuate` | ย้าย VM ทุกตัวออกจาก node ก่อนซ่อมบำรุง |
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

Web console ใช้ [noVNC](https://github.com/novnc/noVNC) 1.6.0 (MPL-2.0) และ pako (MIT) ที่รวมไว้ใน `ibhics/manager/static/novnc/` พร้อมไฟล์ license ของแต่ละโครงการ

ผู้ร่วมพัฒนาต้องยอมรับ [Contributor License Agreement](CLA.md) ก่อน PR แรกจะถูก merge ดูขั้นตอนใน [CONTRIBUTING.md](CONTRIBUTING.md)
