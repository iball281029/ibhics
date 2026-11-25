"use strict";
const AI_TEXT = Object.freeze({
  name: "น้อง ib",
  welcome: "สวัสดีค่ะ น้อง ib พร้อมช่วยดูแล ibHiCS นะคะ 🐘\nสงสัยเรื่องเมนู การตั้งค่า หรือข้อความแจ้งเตือน ถามได้เลยค่ะ",
  mascotAlt: "น้อง ib พร้อมช่วยเหลือ",
  questions: ["สำรองข้อมูลคอนเทนเนอร์อย่างไร", "ช่วยอธิบายข้อความแจ้งเตือน", "ควรตรวจสอบระบบตรงไหนก่อน"],
  thinking: "น้อง ib กำลังคิดให้นะคะ…",
  privacy: "ข้อความจะถูกประมวลผลด้วยบริการ AI ภายนอก หากแนบสรุประบบ จะมีชื่อทรัพยากรและสถานะงานด้วย กรุณาไม่ส่งรหัสผ่านหรือข้อมูลลับ ประวัติแชตจะหายเมื่อรีโหลดหรือเริ่มแชตใหม่ค่ะ",
  status: {
    no_support: "ต้องมีสิทธิ์บริการ Support ที่ยังไม่หมดอายุ จึงจะใช้ผู้ช่วย AI ได้ค่ะ",
    not_provisioned: "กรุณาติดต่อทีม Support เพื่อตั้งค่าบริการ AI ให้ระบบนี้ค่ะ",
    disabled: "ให้ผู้ดูแลระบบเปิดใช้ผู้ช่วย AI ก่อนนะคะ",
    ready: "พร้อมช่วยแล้วค่ะ คำตอบเป็นคำแนะนำ ควรตรวจสอบก่อนเปลี่ยนแปลงระบบนะคะ"
  },
  usage: (used, limit) => ` วันนี้ใช้ไป ${used} จาก ${limit} คำถาม`,
  errors: {
    "AI allowance reached. Contact Support.": "วันนี้ใช้สิทธิ์ AI ครบแล้วค่ะ กรุณาติดต่อทีม Support หากต้องการเพิ่มสิทธิ์",
    "AI access was declined. Contact Support to check your subscription.": "ยังใช้บริการ AI ไม่ได้ค่ะ กรุณาติดต่อทีม Support เพื่อตรวจสอบสิทธิ์",
    "AI service is unavailable. Check connectivity or contact Support.": "ยังติดต่อบริการ AI ไม่ได้ค่ะ ลองตรวจสอบอินเทอร์เน็ตหรือติดต่อทีม Support นะคะ",
    "Contact Support to provision the AI service.": "กรุณาติดต่อทีม Support เพื่อตั้งค่าบริการ AI ให้ระบบนี้ค่ะ",
    "An active Support subscription is required.": "ต้องมีสิทธิ์บริการ Support ที่ยังไม่หมดอายุ จึงจะใช้ผู้ช่วย AI ได้ค่ะ",
    "Ask your administrator to enable AI Assistant.": "ให้ผู้ดูแลระบบเปิดใช้ผู้ช่วย AI ก่อนนะคะ",
    "Invalid chat message or history": "ข้อความหรือประวัติแชตยาวเกินไปค่ะ ลองเริ่มแชตใหม่หรือย่อคำถามนะคะ"
  },
  unavailable: "ตอนนี้น้อง ib ยังตอบไม่ได้ค่ะ กรุณาลองใหม่อีกครั้งนะคะ"
});
