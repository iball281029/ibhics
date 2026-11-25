# แผนพัฒนา ibHiCS

## อัปเดต 25 พฤศจิกายน 2026 หลังเปิดใช้ Alert และแก้ Container ซ้ำ

- Alert ภาษาอังกฤษเปิดใช้ทั้งสองโหนด: Email HTML/ข้อความธรรมดา, LINE Flex, ข้อมูล Compression/Deduplication และสรุปรายวัน ผู้ใช้ยืนยันได้รับทั้ง LINE และอีเมล รวมถึงทดสอบอีเมลที่ระบุทั้งกลุ่มและส่วนตัวแล้ว
- Container ซ้ำ: เปิดใช้สองโหนดและตรวจ API จริงเหลือ `ubututest` หนึ่งรายการ Running บน ibhics-node2; บล็อกการลบขณะทำงานและตรวจสถานะซ้ำหลังได้ task lock ชุดทดสอบผ่าน 23 รายการ
- Backup: เพิ่ม Cancel, รายละเอียดความคืบหน้า และ full pull เมื่อ CBT เดิมเสียแล้ว; npm-vm #23 สำเร็จ แต่ PNetLab #24 ล้มเหลวด้วย JSONDecodeError และความเร็วงานใหญ่ยังไม่ผ่านเกณฑ์ จึงยังไม่ปิดงาน Backup
- งานค้าง: Backup ความเร็ว/JSONDecodeError, เอา Network Advanced ออก, ตรวจ Active Manager บน API/หน้า Cluster และหาสาเหตุ VM ค้าง
- Container Backup ยังไม่เริ่ม รอผู้ใช้สั่งโดยเฉพาะ; Test Failover — Zabbix และ Cutover ผู้ใช้รับไปทำเอง ไม่นับเป็นงานค้างของเรา

รายละเอียดด้านล่างเป็นประวัติการตรวจรับก่อนอัปเดตนี้

แผนตั้งต้นอ้างอิงประวัติถึง b301bda ผลตรวจล่าสุดด้านล่างอ้างอิงงานถึง d2edba0 วันที่ 24 พฤศจิกายน 2026 ก่อนเริ่มแต่ละชุดให้เทียบกับ main บน GitHub และ PR ที่ยังเปิดอยู่

## ผลที่ยืนยันแล้ว ณ วันที่ 24 พฤศจิกายน 2026

| ส่วนงาน | หลักฐานล่าสุด | เกณฑ์ที่ยังต้องผ่าน |
|---|---|---|
| Backup | งานตามเวลาเมื่อวันที่ 23 พฤศจิกายนสำเร็จ งาน #9 ของวันที่ 24 พฤศจิกายนถูกยกเลิกตามคำสั่งผู้ใช้และล้าง retry แล้ว VM ยังทำงานอยู่ | หาสาเหตุความช้า กู้จาก restore point จริงเทียบ checksum และทดสอบ NFS cleanup หลัง commit |
| Sync Now | Deploy ตัวจับคู่ชื่อ Source บน .103/.104 แล้ว ทดสอบจริง Zabbix ผ่าน 2 รอบ: CBT ส่ง 16 MiB และรอบติดกันส่ง 0 bytes ใน 16.53 วินาที ไม่มี fallback ไป Full; API idle/error null | Cutover และการบูต generation หลัง Sync รอบใหม่นี้ยังไม่ทดสอบ |
| Test Failover — Zabbix | ผู้ใช้รับไปทดสอบเอง ตัดออกจากรายการงานค้างของเรา ตามคำสั่งล่าสุด | ไม่รันทดสอบต่อเอง; รอผู้ใช้แจ้งหากพบปัญหา |
| Cutover | ผู้ใช้รับไปทดสอบเอง ตัดออกจากรายการงานค้างของเรา ตามคำสั่งล่าสุด | ไม่เริ่ม Cutover เอง; รอผู้ใช้แจ้งหากพบปัญหา |
| พื้นที่ root | ปิดการตรวจรับ Migration จริง 300 GiB ผ่านแอป 25 พ.ย.: แก้ socket path ยาวโดยใช้ temp ชื่อสั้นบน storage, บูตถึง AlmaLinux login แบบไม่มี NIC และล้างชุดทดสอบครบ; root เปลี่ยนเพียง 16 KiB หลัง cleanup; แพตช์ลงทั้ง .103/.104 แล้ว | หลักฐานใน docs/migration-root-acceptance-20261125.json; Test Failover/Cutover ผู้ใช้รับไปทดสอบเอง ไม่อยู่ในงานค้างของเรา |
| ความเร็ว Test Failover | แพตช์ sparse copy ลง .103/.104 แล้ว รอบจริง 24 พ.ย. เข้าขั้น virt-v2v ภายในประมาณครึ่งนาทีจากการตรวจ process; รวมแปลง raw และ qcow2 จนเปิด VM ประมาณ 19 นาที | คอขวดถัดไปคือการแปลงดิสก์สองช่วง; ต้องวัดก่อนปรับ ห้ามเรียกขั้นคัดลอกนี้ว่า incremental sync |
| LINE | เพิ่ม config ฝั่ง server, token ที่ปิดบังใน API, ผู้รับ, การเลือก event และ endpoint ทดสอบสำหรับ admin เทสต์แจ้งเตือนผ่าน 30 รายการ | ตรวจรับ Deploy และทดสอบส่งจริงด้วย channel token และผู้รับที่กำหนด |
| ธีมสว่าง | ปิดงานตามขอบเขตที่แจ้ง: ตรวจ 18 หน้า/34 dialog, ข้อความพื้นสีทึบ 1,473 จุดไม่พบ contrast ต่ำกว่าเกณฑ์ของ audit; ตรวจภาพส่วนที่แก้และยืนยันธีมมืดไม่เปลี่ยน ลง CSS ทั้ง .103/.104 แล้วและไฟล์ที่เว็บจริงส่งตรงกับชุดทดสอบ | ไม่มีงานค้างในชุดธีมนี้; หลักฐานและข้อจำกัดการตรวจอยู่ใน docs/light-theme-audit-20261124.json |
| Active Manager | โค้ดแสดงโหนดที่ได้รับเลือกอยู่ใน PR #82 | ยืนยัน API และหน้า Cluster หลัง deploy |
| VM ค้าง | ประวัติระบุว่ากลับมาใช้งานได้หลังปิดเปิด | ยืนยันสาเหตุและวิธีแก้ถาวร |
| อีเมลกลุ่ม | SMTP ใช้ Microsoft 365 ผู้ส่งเป็นสมาชิกที่รายงานว่าไม่ได้รับสำเนาผ่านกลุ่ม | ยืนยันชนิดกลุ่มและตรวจการรับสำเนาของผู้ส่ง |

CI ของ 3cad3e8 ผ่านครบ 8 งาน รอบแก้ล่าสุด d2edba0 ยังต้องตรวจผล CI ของ commit นั้นเพิ่ม ผลทดสอบในเครื่องของแพตช์แก้ resize ล่าสุดผ่าน 67 รายการและข้าม 1 รายการ การตรวจรับจาก Astra ยังติดข้อจำกัดเครดิต workspace

1. **Backup** — ต่อยอด #74 (NFS scratch cleanup) และ #75 (retry, catch-up, RPO guard) ตรวจผล scheduled backup ล่าสุด เวลา restore point ล่าสุด และเหตุที่ retry จากประวัติงาน ทดสอบ schedule ที่พลาดหนึ่งรอบและ NFS cleanup ล้มหลัง commit บนชุดทดสอบ เกณฑ์ผ่านคือไม่มี restore point ซ้ำ งานที่ commit สำเร็จยังแสดงว่าสำเร็จ และกู้ไฟล์ตัวอย่างเทียบ checksum ได้
2. **Replication** — เก็บ manager journal ของ Zabbix ช่วง 14:00–14:02 ของวันที่เกิดเหตุ เทียบ filesystem ที่เต็ม โหนดที่ทำงาน snapshot และสถานะ CBT ก่อนสรุปสาเหตุ เพิ่มข้อความผิดพลาดที่บอกประเภทปัญหาและ job ID โดยกรอง credential แล้วทดสอบ incremental ทั้งกรณีข้อมูลเปลี่ยนและไม่เปลี่ยน ตรวจ generation ก่อนและหลัง failure แยกงาน migration cache ที่แก้อยู่ใน dev เข้าชุดทดสอบ importer และ release ของมันเอง
3. **LINE** — เพิ่มช่องทาง LINE Messaging API ในระบบแจ้งเตือนเดิม เลือกเหตุการณ์ backup/replication ได้ เก็บ token ฝั่ง server และให้ผู้ดูแลกำหนดผู้รับ ทดสอบส่งข้อความด้วยผู้รับทดสอบ รวมกรณี token ผิดและ rate limit เกณฑ์ผ่านคือส่งถึงผู้รับที่ตั้งค่าไว้ และความล้มเหลวของการส่งไม่เปลี่ยนผล backup หรือ replication
4. **ธีมสว่าง** — ทดสอบสลับมืด → สว่าง → มืดในหน้า Backup, Mirror, Migration และหน้าที่มีปัญหา โดยเปิดเมนูและ dialog ค้างขณะสลับ ใช้แนวคิดสลับคู่สีของตัวพื้นกับข้อความหรือไอคอนผ่าน CSS variables ตรวจสี hardcode ของแต่ละโมดูล รักษาความหมายของสีแจ้งเตือนและภาพต้นฉบับ เทียบภาพก่อนและหลังที่ viewport เดียวกัน ตรวจข้อความ ขอบ focus และตำแหน่งองค์ประกอบ เพื่อแยกอาการสีจมหายจาก layout เคลื่อนจริง ดูงานเมนูใน PR #80 ก่อนแก้ส่วนที่ทับกัน

## แก้การใช้พื้นที่ root ของ Migration และ Replication แบบถาวร

ให้เขียนข้อมูล VM และไฟล์ชั่วคราวของ Migration และ Replication ลง storage ที่งานนั้นใช้ (WSO) รวมถึง cache, scratch, snapshot export และ NBD backup หากขั้นตอนใดต้องเขียนบนโฮสต์ต้นทาง ให้ใช้พื้นที่บน storage ที่จัดไว้และเข้าถึงได้จากโฮสต์นั้น ตรวจว่า path อยู่บน filesystem ที่คาดไว้และพื้นที่พอก่อนเริ่ม หาก mount หายหรือพื้นที่ไม่พอ ให้หยุดพร้อมระบุ storage และสาเหตุ ห้าม fallback ไปเขียนบน root แม้ path ใต้ mount point ยังมีอยู่

- **Migration** — เหตุที่ยืนยันแล้วคือ root ของ node2 เต็มและ virt-v2v ใช้ cache เริ่มต้นของ libguestfs ที่ `/var/tmp` จึงย้าย VM ไม่สำเร็จ ตามบันทึกส่งต่องาน โหนด .103 และ .104 ใช้ hotpatch ตั้ง `LIBGUESTFS_CACHEDIR` ไปที่ `<out_dir>/.libguestfs-cache` แล้ว ให้นำการแก้เข้า source และแพ็กเกจ release ตรวจว่า cache และ scratch อยู่บน filesystem ปลายทางที่จัดไว้ และทดสอบย้าย VM ขนาดใหญ่พร้อมวัดพื้นที่ root ระหว่างงาน
- **Replication** — โค้ดก่อนแก้ใช้ `/var/tmp` สำหรับ export snapshot ผ่าน SSH และ NBD bitmap backup บนโฮสต์ต้นทาง แพตช์ใน repo ใช้ scratch ที่กำหนดไว้และตรวจ mount, filesystem กับพื้นที่ว่างก่อนเริ่มแล้ว ให้ยืนยันผลหลัง deploy รวมถึงกรณี storage หายระหว่างใช้งาน สาเหตุ SourceNotFound ของ Zabbix ยืนยันจาก journal แล้วตามตารางผลตรวจ
- **เกณฑ์ผ่าน** — ทดสอบทั้งงานสำเร็จและงานล้ม ตรวจว่า cache, snapshot export และ NBD scratch ไม่ถูกเขียนลง root ตรวจการล้างไฟล์ชั่วคราว และยืนยันว่าการอัปเกรดแพ็กเกจยังคงใช้ตำแหน่งที่ตั้งค่าไว้

## ความเร็วและการกู้คืนของ Replication

สำหรับต้นทาง KVM/Proxmox ให้ตั้ง `IBHICS_REPLICATION_SOURCE_SCRATCH` ในบริการ ibHiCS ที่รัน replication worker เป็น absolute path ของจุด mount บนโฮสต์ต้นทาง แต่ละต้นทางที่ worker ติดต่อจะต้องเตรียม storage ที่ path นี้ งาน snapshot export และ NBD จะหยุดหากไม่ได้ตั้งค่า จุด mount ใช้ไม่ได้ อยู่บน root หรือมีพื้นที่ไม่พอ ควรใช้ path สั้นเพื่อให้ชื่อ Unix socket ของ NBD อยู่ในขนาดที่ระบบรองรับ

- **Full ครั้งแรก** — สร้าง replica ตั้งต้นบน storage ของงาน แล้วบันทึก generation ที่ตรวจสอบได้พร้อม token สำหรับติดตามบล็อกที่เปลี่ยน
- **Sync Now และรอบตามเวลา** — ใช้ CBT หลัง Full เพื่ออ่านและส่งเฉพาะบล็อกที่เปลี่ยน รอบที่ไม่มีการเปลี่ยนแปลงต้องไม่ส่งข้อมูลดิสก์ ถ้า CBT ใช้ไม่ได้ให้แจ้งสาเหตุและรักษา generation ล่าสุดที่ใช้ได้ ให้ผู้ดูแลเลือก reseed เองเมื่อจำเป็น วัดเวลารวมและจำนวนไบต์ที่อ่านและส่งในรอบว่างและรอบเปลี่ยนข้อมูลเล็กน้อย
- **Test Failover** — ทดสอบเปิด VM แยกเครือข่ายจาก generation ล่าสุดที่ใช้ได้ ตรวจกรณี Sync รอบใหม่ล้มเหลวแต่ replica เดิมยังสมบูรณ์ และยืนยันว่าปิดและลบ VM ทดสอบแล้ว replica เดิมยังใช้ได้
- **Cutover** — ปิด VM ต้นทางและยืนยันสถานะก่อนทำ incremental รอบสุดท้าย จากนั้นเปิด VM ปลายทางเมื่อยืนยันว่า replica พร้อมใช้งาน ทดสอบบน VM ชุดทดสอบและเก็บ job ID, ขั้นตอนที่ล้ม, ข้อความผิดพลาด และ generation ของแต่ละปุ่ม

แยก PR ตามชุดงาน บันทึกผลทดสอบที่รันจริงและสิ่งที่ยังไม่ได้ตรวจไว้ใน PR ของแต่ละชุด

### หลักฐาน Test Failover รอบ 24 พ.ย. 2026

- Job `dd2632fcd9ab4e639e82ca3ca732e252` (Zabbix), ปลายทาง `ibhics-node2`, storage `virtualstorage`, ดิสก์ 300 GiB ตามที่ผู้ใช้ให้ดำเนินการต่อ
- เริ่มผ่าน API ด้วย `networks: []`; VM ทดสอบ UUID `3ef04ff2-0cc1-4303-9e5a-466dca458331` บูตถึง `appliance login:` ของ AlmaLinux 8.8; `virsh domiflist` ไม่มี interface
- ภาพ console ในเครื่อง: `/private/tmp/ibhics-zabbix-test-screen.png` (ไฟล์ชั่วคราว)
- หลัง cleanup: API คืน `paused`, `operation=null`, `test_vm=null`, `error=null`; ไม่พบ VM ทดสอบ ดิสก์ qcow2 หรือ workspace `test`; VM จริงสองเครื่องยัง running
- Manifest SHA256 ก่อน/หลัง cleanup ตรงกัน: `657cd26f0c36da6760540daa0659784d4bef81b31fd2ce55558882f08aa3c7cc`; ไฟล์ raw เดิมทุกไฟล์ยังมี size/inode/mtime เดิม
- Production ใช้แพตช์เฉพาะ `replication/store.py`, `importer/resize.py` และ method Test Failover ของ manager; ยังไม่ได้ deploy ชุด protection ทั้งหมด. มีไฟล์สำรองชื่อ `*.before-failover-20261124-*` และ manager backup บน .104
- ข้อจำกัด: Guest Agent ไม่เชื่อมต่อ จึงยืนยันการบูตจาก console; ไม่ได้ทดสอบบริการ Zabbix หรือ Cutover และไม่ควรสรุปว่าผ่านทั้งหมด

- 24 พ.ย. 2026 เวลา 16:52: แพตช์ประวัติ Test Failover ลง .103/.104 แล้ว; API ยืนยันรอบ 16:40 เป็น `done`, error null ขณะที่ Sync เดิมยัง `failed` พร้อม error เดิม; regression tests `tests/test_replication.py` ผ่าน 37 รายการ. แก้ประวัติย้อนหลังเฉพาะรอบที่ยืนยันบูตและ cleanup ด้วยหลักฐานจริง โดยสำรอง state เดิมก่อนแก้

### Sync Now evidence - 2026-11-24

- Production source resolution patched on .103/.104: job `esx01` resolves uniquely to `ESX01`; source configuration and credentials unchanged. Backup: `replications.py.before-source-fix-20261124-*`.
- First sync completed at 16:56:15: CBT, 16,777,216 bytes transferred (16 MiB), no fallback.
- Repeat started 16:57:40.438 and completed 16:57:56.966 (16.53 seconds): CBT, zero bytes transferred, no fallback. Final API state: idle, operation null, error null.
- These runs did not resend the full 300 GiB disk. This does not certify every provider or failure scenario. Source resolution tests were included in the previously passing 37-test replication suite.
- Original 720-minute schedule retained. Cutover and boot validation of the newly synced generation remain outstanding.

### Persistent bulk-data storage configuration

- Both managers now use `IBHICS_UPLOAD_DIR=/mnt/ibhics/virtualstorage/.ibhics-uploads` in `/etc/systemd/system/ibhics-manager.service.d/40-storage-paths.conf`. The active process environment was verified after restart. Legacy upload directories were empty; no uploaded data was discarded.
- Upload and P2V reject root-backed paths before directory creation or request consumption, and recheck the opened file descriptor before writing. Upload space preflight uses Content-Length when available. ENOSPC removes partial uploads and retains the previous file; replica staging failure retains the previous generation.
- Migration scratch rejects root, missing filesystem mounts, symlink scratch roots and insufficient space. Explicit scratch overrides also pass the root guard. Guestfs cache, TMPDIR, LIBGUESTFS_TMPDIR, resizing and NTFS helper mounts stay in the selected storage workspace.
- Replication validates destination storage and nested replica paths. KVM/Proxmox exports no longer default to `/var/tmp` on the source: set `IBHICS_REPLICATION_SOURCE_SCRATCH` on the execution agent to a mounted data-storage path that exists on the source host. Missing/invalid/offline/root-backed source storage refuses export before taking the snapshot.
- Production payload tests: 119 passed, 1 skipped. Final dedicated storage tests: 11 passed. Live placement/cleanup checks and root-space measurements are recorded in `root-storage-audit-20261124.json`.
- The live 300 GiB check used a sparse probe and real guestfs inspection, not a full OS migration. Authenticated UI acceptance is pending login; existing production VMs were not stopped or production mounts detached for fault injection.

### Migration acceptance closed - 2026-11-25

- User excluded Zabbix Test Failover and Cutover from our remaining work. No such operation was run in this acceptance round.
- The first migration attempt exposed an overlong qemu-nbd UNIX socket path. Fixed by keeping guestfs cache in the conversion workspace and placing runtime temp in a short per-workspace `.gt-*` directory on the same data storage. Cleanup removes the short directory on successful and failed conversions.
- Regression tests: conversion/storage suite 72 passed; portable real UNIX-socket binding and failed-conversion cleanup checks 2 passed. The patch was deployed on both nodes with backups on virtualstorage.
- User authorized installation/restart while PNetLab backup #10 was running. Its QEMU backup was aborted, VM remained running, persisted job state set cancelled and retry_at cleared before restart.
- A new independent sparse copy of the 300 GiB replica was imported through the authenticated Migration API as job 1, RootStorageAccept-e08f6467, on ibhics-node2. The copy held no ongoing replica lock and did not alter the original.
- Job finished done/error null in 18m24s. VM started only after NIC removal; console reached AlmaLinux 8.8 appliance login. VM, destination disk, source test copy, workspace and short temp directory were removed afterward.
- Root free before the run: 11,930,640,384 bytes; after cleanup: 11,930,624,000 bytes (16 KiB difference, no bulk image growth on root). Original two VMs remained running and services active.
- Existing limitations about production mount fault injection and unconfigured KVM/Proxmox source scratch still apply. This closes the migration storage-placement acceptance, not every provider or guest-application test.

### LINE delivery and notification-state recovery - 2026-11-25

- Deployed the four LINE-only files (`notifications.py`, `settings_api.py`, `settings.js`, `index.html`) to both nodes, retaining backups under `/mnt/ibhics/virtualstorage/.ibhics-maintenance/line-notifications/{103,104}/`. Notification tests: 30 passed.
- The user configured a free LINE OA and confirmed receipt of the test message in the intended personal chat. API acceptance alone was not treated as recipient delivery. Current selected event is `backup.succeeded`; acceptance of an actual backup event is pending the user's manual Run now test.
- Investigated missing SMTP settings: .103 retained the configured SMTP settings, while the active .104 manager had empty SMTP fields. Recovered only SMTP through the authenticated API, preserving LINE token, recipient and event selection. Verified again after the manager restarts: SMTP host/sender/password and one email recipient configured; LINE enabled with saved token and backup.succeeded selected. No credentials are recorded here.
- Added notifications.json to manager state replication/takeover. Seeded the shared copy from the active manager after recovery, patched both nodes, and restarted managers one at a time with no active backup/import/replication jobs. Backups: `/mnt/ibhics/virtualstorage/.ibhics-maintenance/line-state/`.
- Regression validates SMTP and LINE settings both survive the actual replicate/restore helpers and keep private permissions. `tests/test_leader.py tests/test_notifications.py`: 38 passed; Ruff and diff checks passed.
- User requested removal of Network Advanced (isolated gateway/NAT/DHCP) as a separate follow-up; it has not yet been removed.
