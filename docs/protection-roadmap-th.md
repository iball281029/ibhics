# แผนพัฒนา ibHiCS

แผนตั้งต้นอ้างอิงประวัติถึง b301bda ผลตรวจล่าสุดด้านล่างอ้างอิงงานถึง d2edba0 วันที่ 24 พฤศจิกายน 2026 ก่อนเริ่มแต่ละชุดให้เทียบกับ main บน GitHub และ PR ที่ยังเปิดอยู่

## ผลที่ยืนยันแล้ว ณ วันที่ 24 พฤศจิกายน 2026

| ส่วนงาน | หลักฐานล่าสุด | เกณฑ์ที่ยังต้องผ่าน |
|---|---|---|
| Backup | งานตามเวลาเมื่อวันที่ 23 พฤศจิกายนสำเร็จ งาน #9 ของวันที่ 24 พฤศจิกายนถูกยกเลิกตามคำสั่งผู้ใช้และล้าง retry แล้ว VM ยังทำงานอยู่ | หาสาเหตุความช้า กู้จาก restore point จริงเทียบ checksum และทดสอบ NFS cleanup หลัง commit |
| Sync Now | Journal ของ Zabbix ระบุ SourceNotFound พบ job อ้าง esx01 ขณะที่ config ที่แชร์ใช้ ESX01 และตรวจชนิดต้นทาง โฮสต์ path และ transport แล้วตรงกัน เพิ่มการจับคู่ชื่อแบบไม่แยกตัวพิมพ์เมื่อมีเพียงรายการเดียวในโค้ดแล้ว | Deploy และวัด incremental จริงทั้งรอบที่ข้อมูลเปลี่ยนและรอบว่าง |
| Test Failover | ทดสอบจริง Zabbix ผ่าน 24 พ.ย. 2026 เวลา 16:40–16:42 หลังตั้งปลายทาง 300 GiB: VM บูตถึงหน้า login AlmaLinux 8.8 ไม่มี NIC; cleanup ลบ VM ดิสก์และ workspace ครบ โดย checksum manifest และตัวตนไฟล์ replica เดิมไม่เปลี่ยน | เก็บข้อบกพร่องประวัติที่ยังใช้ state failed ของ Sync เดิมทั้งที่ test สำเร็จ; Guest Agent ไม่เชื่อมต่อและยังไม่ตรวจบริการ Zabbix ผ่านเครือข่าย |
| Cutover | มีแพตช์ให้ใช้ Source เดียวกับ Sync รอบสุดท้าย | ทดสอบบน VM ชุดทดสอบหลังแก้ Sync และขนาดดิสก์ |
| พื้นที่ root | แพตช์ใน repo ครอบคลุม cache และ temp ของ conversion กับ resize รวมถึงตัวตรวจ migration scratch และ replica storage | Deploy ครบและวัดพื้นที่จริงตลอดงาน รวมกรณี mount หายและพื้นที่ไม่พอ |
| ความเร็ว Test Failover | แพตช์ sparse copy ลง .103/.104 แล้ว รอบจริง 24 พ.ย. เข้าขั้น virt-v2v ภายในประมาณครึ่งนาทีจากการตรวจ process; รวมแปลง raw และ qcow2 จนเปิด VM ประมาณ 19 นาที | คอขวดถัดไปคือการแปลงดิสก์สองช่วง; ต้องวัดก่อนปรับ ห้ามเรียกขั้นคัดลอกนี้ว่า incremental sync |
| LINE | เพิ่ม config ฝั่ง server, token ที่ปิดบังใน API, ผู้รับ, การเลือก event และ endpoint ทดสอบสำหรับ admin เทสต์แจ้งเตือนผ่าน 30 รายการ | ตรวจรับ Deploy และทดสอบส่งจริงด้วย channel token และผู้รับที่กำหนด |
| ธีมสว่าง | แก้ palette ของ Backup, Migration และ storage dialogs ตรวจ browser fixture 13 องค์ประกอบผ่านการสลับมืด–สว่าง–มืด สีและตำแหน่งผ่านเกณฑ์ของชุดทดสอบ | Deploy และตรวจหน้าแอปจริงพร้อมเมนูและ dialog |
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
