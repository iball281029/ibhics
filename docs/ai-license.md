# บัตรผ่าน AI ใน License

เครื่องมือผู้จำหน่ายใช้ Python 3.11+, OpenSSL CLI และ Codex CLI ที่ติดตั้งไว้แล้ว
ใช้บัญชี ChatGPT รายเดือนผ่าน Codex login เท่านั้น ไม่ใช้ API key, relay หรือโดเมนของผู้จำหน่าย
เก็บ private signing key ไว้ฝั่งผู้จำหน่ายเสมอ ห้ามส่งให้ลูกค้า

## เตรียมบัญชีครั้งแรก

```sh
python3 tools/license-gen.py add-account vendor-1
python3 tools/license-gen.py accounts
```

คำสั่งแรกเปิดขั้นตอน `codex login` ให้ผู้ใช้อนุญาตบัญชีในเบราว์เซอร์
ชื่อบัญชีใช้เฉพาะ `a-z`, `0-9`, `-` เก็บที่ `~/.ibhics-license/accounts/<name>/`
ไดเรกทอรีบัญชีเป็น 0700 ใช้ `--accounts-dir PATH` **ก่อน subcommand** หากต้องการเปลี่ยนที่เก็บ
`accounts` เรียก `codex login status` แสดงสถานะและจำนวน License จาก `issued.jsonl`

## ออก License

```sh
python3 tools/license-gen.py gen --key PRIVATE.pem \
  --customer "Example Customer" --cluster-id IBHX-1234-ABCD --nodes 3 \
  --support-until 2027-12-31 --update-until 2027-12-31 \
  --ai-account vendor-1 --ai-daily-limit 20 -o customer-license.json
```

ไม่ใส่ `--ai-account` เพื่อเลือกบัญชีจากเมนูตัวเลขใน terminal ได้
ต้องระบุ Support และ/หรือ Update อย่างน้อยหนึ่งอย่าง; AI ใช้ได้เมื่อ Support ยัง Active
โควตาต่อคลัสเตอร์คือ 1–50 คำถาม/วัน ค่าเริ่มต้น 20 รีเซ็ต 00:00 UTC (07:00 ไทย)
รวมคำถามที่เรียกบริการแล้วล้มเหลวด้วย และยังอยู่ภายใต้โควตาของบัญชี ChatGPT
เมื่อบัญชีออก License ตั้งแต่ 5 ใบขึ้นไปจะเตือนและให้พิมพ์ `y` หรือระบุ `--force`
จำนวนเป็นประวัติการออกทั้งหมด ไม่ได้ติดตามว่า License ใดเลิกใช้งานแล้ว
ผลลัพธ์ไม่เขียนทับไฟล์เดิม ไฟล์ License และ `~/.ibhics-license/issued.jsonl` เป็น 0600
journal บันทึกเพียง customer, cluster_id, account, date ไม่มี token

## ฝั่งลูกค้า

อัปโหลด License ใน Settings ตามปกติ Manager จะตรวจลายเซ็น ผูกกับ cluster ID
และติดตั้ง auth แบบ atomic ที่ `<shared state dir>/ai-codex/auth.json`
ไดเรกทอรีเป็น 0700 ไฟล์เป็น 0600 ทั้ง Active/Standby ต้องใช้ shared state เดียวกัน
และติดตั้ง Codex CLI บนทุกโหนดที่เป็น Manager ได้
AI เปิดให้อัตโนมัติเมื่อมี Support Active และ runtime พร้อม หากแอดมินเคยปิดไว้จะเคารพค่านั้น
License เดิมไม่มี AI ยังใช้ gateway เดิมได้ การถอด License หรือแทนด้วย License ไม่มี AI
จะลบไดเรกทอรี `ai-codex` รวม auth และ usage ของบัตรผ่าน

ต้องออกอินเทอร์เน็ต HTTPS ไปยังบริการ ChatGPT/OpenAI (`chatgpt.com`, บริการใต้ `openai.com`
และปลายทางยืนยันตัวตนที่ Codex ใช้) ได้ การติดตั้ง runtime ต้องเข้าถึง Debian mirror
และ npm registry ได้ด้วย installer/upgrade พยายามติดตั้ง nodejs, npm และ `@openai/codex`
หากออฟไลน์จะเตือนและติดตั้ง ibHiCS ต่อ ผู้ดูแลสามารถติดตั้ง runtime ภายหลังด้วย:

```sh
apt-get install -y nodejs npm
npm install -g @openai/codex
```

เมื่อ Codex ปฏิเสธ auth ระบบจะแสดง “The AI pass in this license has expired.
Contact your vendor for a new license.” พร้อมข้อความไทยใน UI ภาษาไทย
ผู้จำหน่ายต้อง login บัญชีใหม่ ออก License ใหม่ แล้วให้ลูกค้าอัปโหลดอีกครั้ง
สถานะ API แสดงเพียงชื่อบัญชี โควตา และ state ของบัตร ไม่ส่ง auth หรือ CLI stderr กลับ

## ข้อจำกัด

AES-256-CTR + HMAC เป็นการทำให้แกะยาก คีย์/pepper อยู่ในซอฟต์แวร์ลูกค้า
ผู้ที่ควบคุมเครื่องและตั้งใจแกะสามารถดึงบัตรผ่านได้ ลายเซ็น Ed25519 เป็นตัวรับรองสิทธิ์จากผู้จำหน่าย
บัญชีเดียวแบ่ง 3–5 License ได้ตามการทดสอบที่ผู้ใช้แจ้ง แต่หากบัญชีถูกระงับ
ทุก License ของบัญชีนั้นจะหยุดใช้ AI ด้วย โควตาบัญชีใช้ร่วมกัน และสถานะ login ไม่ยืนยันสิทธิ์ Pro

การต่ออายุ/หมุน refresh token เมื่อหลายเครื่องแชร์บัญชียังต้องผ่าน soak test จริง
เทสต์ใน repository ใช้ auth จำลอง ไม่ login จริง ไม่ยืนยันการต่ออายุ token หรือการใช้งานหลายเครื่องจริง
บัตรที่ฝังใน License เป็น snapshot; Codex อาจต่ออายุ auth ใน shared state ได้ตาม runtime
แต่ snapshot ที่ออกไปแล้วจะไม่เปลี่ยนตาม ผู้จำหน่ายควรทดสอบอายุบัตรก่อนส่งมอบ
