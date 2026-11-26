"use strict";
const AI_TEXT = Object.freeze({
  name: "น้อง ib",
  welcome: "สวัสดีค่ะ น้อง ib พร้อมช่วยดูแล ibHiCS นะคะ 🐘\nสงสัยเรื่องเมนู การตั้งค่า หรือข้อความแจ้งเตือน ถามได้เลยค่ะ",
  mascotAlt: "น้อง ib พร้อมช่วยเหลือ",
  questions: ["ตรวจสถานะระบบ", "เพิ่ม Virtual switch (VLAN) 123", "ช่วยอธิบายข้อความแจ้งเตือน"],
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
    "Daily AI allowance reached. Contact Support.": "ครบโควตาคำถาม AI สำหรับวันนี้แล้วค่ะ รีเซ็ตเวลา 07:00 น. (เวลาไทย) ข้อความนี้ไม่ได้หมายความว่า Support License หมดอายุ",
    "A preview question is already running. Please wait.": "น้อง ib กำลังตอบคำถามอื่นอยู่ค่ะ รอสักครู่แล้วลองใหม่นะคะ",
    "AI allowance reached. Contact Support.": "ครบโควตาคำถาม AI สำหรับวันนี้แล้วค่ะ รีเซ็ตเวลา 07:00 น. (เวลาไทย) ข้อความนี้ไม่ได้หมายความว่า Support License หมดอายุ",
    "AI access was declined. Contact Support to check your subscription.": "ยังใช้บริการ AI ไม่ได้ค่ะ กรุณาติดต่อทีม Support เพื่อตรวจสอบสิทธิ์",
    "AI service is unavailable. Check connectivity or contact Support.": "ยังติดต่อบริการ AI ไม่ได้ค่ะ ลองตรวจสอบอินเทอร์เน็ตหรือติดต่อทีม Support นะคะ",
    "Contact Support to provision the AI service.": "กรุณาติดต่อทีม Support เพื่อตั้งค่าบริการ AI ให้ระบบนี้ค่ะ",
    "An active Support subscription is required.": "ต้องมีสิทธิ์บริการ Support ที่ยังไม่หมดอายุ จึงจะใช้ผู้ช่วย AI ได้ค่ะ",
    "Ask your administrator to enable AI Assistant.": "ให้ผู้ดูแลระบบเปิดใช้ผู้ช่วย AI ก่อนนะคะ",
    "Invalid chat message or history": "ข้อความหรือประวัติแชตยาวเกินไปค่ะ ลองเริ่มแชตใหม่หรือย่อคำถามนะคะ"
  },
  unavailable: "ตอนนี้น้อง ib ยังตอบไม่ได้ค่ะ กรุณาลองใหม่อีกครั้งนะคะ"
});

const AI_LOCALES = Object.freeze({
  th: AI_TEXT,
  en: {
    name: "Nong ib",
    welcome: "Hi, I'm Nong ib! Your little ibHiCS helper.\nAsk me about menus, settings, or alerts you need help understanding.",
    mascotAlt: "Nong ib, your elephant AI assistant",
    questions: ["Check system status", "Add virtual switch VLAN 123", "Explain this alert"],
    thinking: "Nong ib is thinking…",
    privacy: "Messages are processed by an external AI service. Optional context includes resource names and job status. Do not send passwords or secrets. Chat history is cleared on reload or New chat.",
    status: {
      no_support: "An active Support License is required to use AI chat.",
      not_provisioned: "Contact Support to configure the AI service for this installation.",
      disabled: "Ask your administrator to enable AI Assistant.",
      ready: "Ready. Answers are guidance; verify before changing your system."
    },
    usage: (used, limit) => ` Today: ${used} of ${limit} questions used.`,
    errors: {
      "Daily AI allowance reached. Contact Support.": "Daily AI question limit reached. Resets at 07:00 Thailand time (00:00 UTC). This does not mean your Support License has expired.",
      "A preview question is already running. Please wait.": "Nong ib is answering another question. Please wait.",
      "AI allowance reached. Contact Support.": "Daily AI question limit reached. Resets at 07:00 Thailand time (00:00 UTC). This does not mean your Support License has expired.",
      "AI access was declined. Contact Support to check your subscription.": "AI access was declined. Contact Support to check your license.",
      "AI service is unavailable. Check connectivity or contact Support.": "Unable to reach the AI service. Check connectivity or contact Support.",
      "Contact Support to provision the AI service.": "Contact Support to configure the AI service.",
      "An active Support subscription is required.": "An active Support License is required.",
      "Ask your administrator to enable AI Assistant.": "Ask your administrator to enable AI Assistant.",
      "Invalid chat message or history": "Your message or history is too long. Shorten it or start a new chat."
    },
    unavailable: "Nong ib is unavailable right now. Please try again later."
  }
});
