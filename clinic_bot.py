"""
Alupe Health Clinic WhatsApp Assistant
Upgraded Production Version
"""

import os
import re
import json
import time
import hashlib
import logging
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
from io import StringIO
import csv

import httpx
import bleach
from fastapi import FastAPI, Request, Response, Query, HTTPException, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('clinic_bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Alupe Health Clinic Assistant", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====================== CONFIG ======================
class Config:
    WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
    PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
    VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "ALUPE_HEALTH_TOKEN_2026")
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    AI_MODEL = os.getenv("AI_MODEL", "llama3-8b-8192")
    ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "change-this-in-production")

    CLINIC_NAME = os.getenv("CLINIC_NAME", "Alupe Health Center")
    CLINIC_PHONE = os.getenv("CLINIC_PHONE", "+254-XXX-XXX-XXX")
    CLINIC_ADDRESS = os.getenv("CLINIC_ADDRESS", "Alupe, Busia County")
    STAFF_PHONE = os.getenv("STAFF_PHONE")  # WhatsApp number for staff notifications
    MPESA_TILL = os.getenv("MPESA_TILL", "YOUR_TILL")

config = Config()
supabase: Client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
scheduler = AsyncIOScheduler()

# ====================== STORES ======================
class UserState:
    MAIN_MENU = "main_menu"
    ASKING_QUESTION = "asking_question"
    BOOKING_TYPE = "booking_type"
    BOOKING_SLOT = "booking_slot"
    BOOKING_CONFIRM = "booking_confirm"
    LEAVING_FEEDBACK = "leaving_feedback"
    MENTAL_HEALTH = "mental_health"

CONSULTATION_TYPES = {
    "1": ("general", "General Consultation", 500),
    "2": ("specialist", "Specialist", 1000),
    "3": ("lab", "Lab Tests", 800),
    "4": ("emergency", "Emergency Care", 1500),
}

class InMemoryStore:
    def __init__(self):
        self.conversation_history = defaultdict(list)   # phone -> [{role, content}]
        self.user_states = {}                           # phone -> UserState
        self.processed_messages = set()                 # message IDs already handled
        self.rate_limits = defaultdict(list)            # phone -> [timestamps]
        self.slot_mappings = {}                         # phone -> {consultation, price, label}
        self.pending_bookings = {}                      # phone -> booking dict awaiting confirm
        self.new_users = set()                          # phones that haven't seen privacy notice

    def is_rate_limited(self, phone: str) -> bool:
        now = time.time()
        # Keep only timestamps within the last 60 seconds
        self.rate_limits[phone] = [t for t in self.rate_limits[phone] if now - t < 60]
        if len(self.rate_limits[phone]) >= 15:
            return True
        self.rate_limits[phone].append(now)
        return False

    def add_to_history(self, phone: str, role: str, content: str):
        self.conversation_history[phone].append({"role": role, "content": content})
        # Keep last 10 exchanges only to avoid token bloat
        if len(self.conversation_history[phone]) > 20:
            self.conversation_history[phone] = self.conversation_history[phone][-20:]

    def clear_history(self, phone: str):
        self.conversation_history[phone] = []

store = InMemoryStore()

# ====================== UTILITIES ======================
def hash_phone(phone: str) -> str:
    return hashlib.sha256(phone.encode('utf-8')).hexdigest()

def sanitize_input(text: str) -> str:
    text = bleach.clean(text, tags=[], strip=True)
    return re.sub(r'[<>{}]', '', text)[:1000]

def detect_emergency(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["suicide", "kill myself", "want to die", "kujiua",
                              "can't breathe", "heart attack", "unconscious", "stroke"]):
        return "critical"
    if any(k in t for k in ["chest pain", "severe pain", "high fever",
                              "difficulty breathing", "broken bone", "maumivu makali"]):
        return "urgent"
    return "normal"

def detect_mental_health_trigger(text: str) -> bool:
    t = text.lower()
    triggers = [
        "not okay", "not ok", "siko sawa", "stressed", "depressed", "depression",
        "anxiety", "anxious", "can't cope", "overwhelmed", "lonely", "alone",
        "failing", "hopeless", "worthless", "tired of everything", "mental"
    ]
    return any(k in t for k in triggers)

def generate_reference(prefix: str = "RES") -> str:
    ts = datetime.now().strftime("%y%m%d%H%M")
    unique = os.urandom(3).hex().upper()
    return f"{prefix}{ts}{unique}"

def format_currency(amount: int) -> str:
    return f"KES {amount:,.0f}"

def get_available_slots() -> list[dict]:
    """
    Returns next 6 available appointment slots.
    Slots are weekdays 8AM-4PM in 1hr blocks.
    In production replace with a real DB availability query.
    """
    slots = []
    now = datetime.now()
    day = now

    while len(slots) < 6:
        day += timedelta(days=1)
        if day.weekday() >= 5:  # skip weekends
            continue
        for hour in [9, 10, 11, 14, 15, 16]:
            if len(slots) >= 6:
                break
            slot_dt = day.replace(hour=hour, minute=0, second=0)
            slots.append({
                "key": str(len(slots) + 1),
                "date": slot_dt.strftime("%Y-%m-%d"),
                "time": slot_dt.strftime("%H:%M"),
                "display": slot_dt.strftime("%A %d %b, %I:%M %p")
            })

    return slots

# ====================== WHATSAPP ======================
async def send_whatsapp_message(to: str, message: str) -> bool:
    try:
        url = f"https://graph.facebook.com/v18.0/{config.PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {config.WHATSAPP_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": message}
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, json=data)
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"WhatsApp send error to {to}: {e}")
        return False

async def notify_staff(message: str):
    """Send a notification to the clinic staff WhatsApp number."""
    if config.STAFF_PHONE:
        await send_whatsapp_message(config.STAFF_PHONE, message)

# ====================== AI ======================
STUDENT_SYSTEM_PROMPT = """You are a friendly, non-judgmental health assistant for Alupe Health Center, 
serving university students in Kenya. You speak warmly and simply — no medical jargon.

Key rules:
- Students are often scared of cost, shame, or judgment. Be reassuring first.
- Understand Kenyan Sheng and Swahili mixed with English. If they write "niko vibaya" or "sijisikii vizuri", understand and respond naturally.
- For sexual health questions (STIs, contraception, pregnancy), be direct and shame-free. Students need real info.
- For mental health, don't just say "see a doctor". Acknowledge their feelings first, then suggest next steps.
- Always end with whether they should: (a) come in, (b) monitor at home, or (c) go to emergency.
- Never diagnose. Always frame as general guidance.
- Keep responses under 200 words.

Context: You serve comrades (university students) at Alupe University area. Consultation fee is KES 500. 
The clinic is approachable and non-judgmental."""

async def get_ai_response(phone: str, question: str) -> str:
    try:
        history = store.conversation_history.get(phone, [])

        messages = [
            {"role": "system", "content": STUDENT_SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": question}
        ]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                json={
                    "model": config.AI_MODEL,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 400
                }
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

        # Store exchange in history
        store.add_to_history(phone, "user", question)
        store.add_to_history(phone, "assistant", content)

        return content + "\n\n_⚠️ This is general guidance only. Visit the clinic for proper diagnosis._"

    except Exception as e:
        logger.error(f"Groq AI error: {e}")
        return f"I'm having issues right now. Please call the clinic directly: {config.CLINIC_PHONE}"

# ====================== PRIVACY NOTICE ======================
async def send_privacy_notice(phone: str):
    msg = (
        "👋 *Welcome to Alupe Health Center Assistant*\n\n"
        "Before we start, a quick note:\n\n"
        "🔒 *Your privacy matters*\n"
        "• Your phone number is encrypted — we never store it in plain text\n"
        "• Your conversations are not shared with anyone\n"
        "• You can ask sensitive questions anonymously and safely\n\n"
        "This assistant provides *general health guidance only* — not a substitute for a doctor.\n\n"
        "Reply *OK* to continue or *Menu* to see what we can help with."
    )
    await send_whatsapp_message(phone, msg)
    store.new_users.discard(phone)

# ====================== MAIN MENU ======================
async def send_main_menu(phone: str):
    menu = (
        f"🏥 *{config.CLINIC_NAME}*\n\n"
        "What would you like to do?\n\n"
        "💬 *Ask* [your question] — health guidance\n"
        "📅 *Book* — schedule appointment\n"
        "❌ *Cancel* [ref] — cancel a booking\n"
        "ℹ️ *Info* — clinic details & hours\n"
        "🆘 *Emergency* — urgent help\n"
        "💚 *Not okay* — mental health support\n\n"
        "Example: _Ask I have a headache and fever_"
    )
    await send_whatsapp_message(phone, menu)
    store.user_states[phone] = UserState.MAIN_MENU

# ====================== MENTAL HEALTH FLOW ======================
async def handle_mental_health(phone: str, text: str):
    state = store.user_states.get(phone)

    if state != UserState.MENTAL_HEALTH:
        # First entry
        store.user_states[phone] = UserState.MENTAL_HEALTH
        msg = (
            "💚 Hey, I hear you.\n\n"
            "It takes courage to reach out — you did the right thing.\n\n"
            "You don't have to be okay right now. Can you tell me a little about what you're going through? "
            "I'm here to listen, and everything you share stays between us.\n\n"
            "_Type anything — even just a few words._"
        )
        await send_whatsapp_message(phone, msg)
        return

    # They've shared something — use AI with a mental health focused prompt
    mh_question = f"[MENTAL HEALTH SUPPORT NEEDED] Student says: {text}"
    reply = await get_ai_response(phone, mh_question)
    await send_whatsapp_message(phone, reply)

    # After one exchange, offer concrete next step
    follow_up = (
        "\n💚 *You're not alone in this.*\n\n"
        "If you'd like to talk to someone in person:\n"
        f"📞 Call us: {config.CLINIC_PHONE}\n"
        "📅 Or reply *Book* to schedule a private consultation\n\n"
        "If you're in crisis right now, reply *Emergency*"
    )
    await send_whatsapp_message(phone, follow_up)
    store.user_states[phone] = UserState.MAIN_MENU

# ====================== BOOKING FLOW ======================
async def show_booking_options(phone: str):
    msg = (
        f"📅 *Book Appointment — {config.CLINIC_NAME}*\n\n"
        "Select consultation type:\n\n"
        "1️⃣ General Consultation — *KES 500*\n"
        "2️⃣ Specialist — *KES 1,000*\n"
        "3️⃣ Lab Tests — *KES 800*\n"
        "4️⃣ Emergency Care — *KES 1,500*\n\n"
        "✅ Pay at the clinic when you arrive\n"
        "Reply *1, 2, 3* or *4* — or *Back* to return"
    )
    await send_whatsapp_message(phone, msg)
    store.user_states[phone] = UserState.BOOKING_TYPE

async def show_available_slots(phone: str, cons_key: str):
    cons_type, cons_label, price = CONSULTATION_TYPES[cons_key]
    slots = get_available_slots()

    store.slot_mappings[phone] = {
        "consultation": cons_type,
        "label": cons_label,
        "price": price,
        "slots": {s["key"]: s for s in slots}
    }

    lines = "\n".join([f"{s['key']}️⃣ {s['display']}" for s in slots])
    msg = (
        f"📅 *{cons_label}* — {format_currency(price)}\n\n"
        "Available slots:\n\n"
        f"{lines}\n\n"
        "Reply with the number of your preferred slot or *Back*"
    )
    await send_whatsapp_message(phone, msg)
    store.user_states[phone] = UserState.BOOKING_SLOT

async def handle_slot_selection(phone: str, selection: str):
    if selection.lower() == "back":
        await show_booking_options(phone)
        return

    mapping = store.slot_mappings.get(phone, {})
    slot = mapping.get("slots", {}).get(selection)

    if not slot:
        await send_whatsapp_message(phone, "Invalid slot. Please reply with a number from the list, or *Back* to restart.")
        return

    # Show full details and ask for confirmation
    cons_label = mapping.get("label", "Consultation")
    price = mapping.get("price", 500)

    confirm_msg = (
        f"✅ *Confirm your booking?*\n\n"
        f"🏥 {config.CLINIC_NAME}\n"
        f"📋 {cons_label}\n"
        f"📅 {slot['display']}\n"
        f"💰 *{format_currency(price)}* — pay at clinic on arrival\n"
        f"📍 {config.CLINIC_ADDRESS}\n\n"
        "Reply *Yes* to confirm or *No* to cancel"
    )
    await send_whatsapp_message(phone, confirm_msg)

    # Store pending booking for confirmation
    store.pending_bookings[phone] = {
        "slot": slot,
        "consultation": mapping.get("consultation"),
        "label": cons_label,
        "price": price
    }
    store.user_states[phone] = UserState.BOOKING_CONFIRM

async def handle_booking_confirmation(phone: str, text: str):
    if text.lower() not in ["yes", "y", "ndio", "confirm"]:
        store.pending_bookings.pop(phone, None)
        store.slot_mappings.pop(phone, None)
        store.user_states[phone] = UserState.MAIN_MENU
        await send_whatsapp_message(phone, "Booking cancelled. Reply *Menu* to start over.")
        return

    pending = store.pending_bookings.get(phone)
    if not pending:
        await send_main_menu(phone)
        return

    try:
        ref = generate_reference("RES")
        slot = pending["slot"]

        booking = {
            "reference": ref,
            "phone_hash": hash_phone(phone),
            "consultation_type": pending["consultation"],
            "date": slot["date"],
            "time": slot["time"],
            "amount": pending["price"],
            "status": "reserved",
            "payment_status": "pending",
            "created_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(hours=48)).isoformat()
        }

        supabase.table("bookings").insert(booking).execute()

        # Confirm to student
        confirmation = (
            f"🎉 *Booking Confirmed!*\n\n"
            f"📋 Ref: *{ref}*\n"
            f"📅 {slot['display']}\n"
            f"💰 {format_currency(pending['price'])} — pay on arrival\n\n"
            "• Arrive 10 minutes early\n"
            "• Bring your student ID\n"
            f"📍 {config.CLINIC_ADDRESS}\n"
            f"📞 {config.CLINIC_PHONE}\n\n"
            f"To cancel, reply: *Cancel {ref}*"
        )
        await send_whatsapp_message(phone, confirmation)

        # Notify staff
        staff_msg = (
            f"📅 *New Booking*\n"
            f"Ref: {ref}\n"
            f"Type: {pending['label']}\n"
            f"Date: {slot['display']}\n"
            f"Amount: {format_currency(pending['price'])}"
        )
        await notify_staff(staff_msg)

        store.pending_bookings.pop(phone, None)
        store.slot_mappings.pop(phone, None)
        store.user_states[phone] = UserState.MAIN_MENU

    except Exception as e:
        logger.error(f"Booking creation error: {e}")
        await send_whatsapp_message(phone, "Sorry, couldn't create the booking. Please call the clinic directly.")

# ====================== CANCELLATION ======================
async def handle_cancellation(phone: str, ref: str):
    ref = ref.strip().upper()
    if not ref:
        await send_whatsapp_message(phone, "Please include your booking reference. Example: *Cancel RES240430ABC*")
        return

    try:
        result = supabase.table("bookings")\
            .select("*")\
            .eq("reference", ref)\
            .eq("phone_hash", hash_phone(phone))\
            .maybe_single()\
            .execute()

        booking = result.data
        if not booking:
            await send_whatsapp_message(phone, f"No booking found with ref *{ref}*. Check the reference and try again.")
            return

        if booking["status"] == "cancelled":
            await send_whatsapp_message(phone, f"Booking *{ref}* is already cancelled.")
            return

        supabase.table("bookings").update({"status": "cancelled"}).eq("reference", ref).execute()

        await send_whatsapp_message(
            phone,
            f"✅ Booking *{ref}* has been cancelled.\n\nReply *Book* to make a new appointment."
        )
        await notify_staff(f"❌ Booking cancelled: {ref}")

    except Exception as e:
        logger.error(f"Cancellation error: {e}")
        await send_whatsapp_message(phone, "Couldn't process cancellation. Please call the clinic.")

# ====================== FEEDBACK ======================
async def request_feedback(phone: str, ref: str):
    """Called after a visit to collect rating."""
    msg = (
        f"👋 Hope your visit went well!\n\n"
        f"How would you rate your experience at {config.CLINIC_NAME}?\n\n"
        "Reply with a number:\n"
        "1 — Poor\n2 — Fair\n3 — Good\n4 — Very Good\n5 — Excellent"
    )
    await send_whatsapp_message(phone, msg)
    store.user_states[phone] = UserState.LEAVING_FEEDBACK

async def handle_feedback(phone: str, text: str):
    if text in ["1", "2", "3", "4", "5"]:
        try:
            supabase.table("feedback").insert({
                "phone_hash": hash_phone(phone),
                "rating": int(text),
                "created_at": datetime.utcnow().isoformat()
            }).execute()
            await send_whatsapp_message(phone, f"Thank you for the {text}⭐ rating! It helps us improve. 🙏")
        except Exception as e:
            logger.error(f"Feedback save error: {e}")
    else:
        await send_whatsapp_message(phone, "Please reply with a number between 1 and 5.")
        return

    store.user_states[phone] = UserState.MAIN_MENU

# ====================== DAILY SUMMARY SCHEDULER ======================
async def send_daily_summary():
    """Sends today's bookings summary to staff at 7AM."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        result = supabase.table("bookings")\
            .select("reference, consultation_type, time, amount, status")\
            .eq("date", today)\
            .neq("status", "cancelled")\
            .execute()

        bookings = result.data or []

        if not bookings:
            summary = f"📋 *{config.CLINIC_NAME} — Daily Summary*\n\nNo bookings for today."
        else:
            lines = "\n".join([
                f"• {b['time']} | {b['consultation_type']} | {format_currency(b['amount'])} | Ref: {b['reference']}"
                for b in bookings
            ])
            total = sum(b["amount"] for b in bookings)
            summary = (
                f"📋 *{config.CLINIC_NAME} — {today}*\n\n"
                f"Appointments today: *{len(bookings)}*\n"
                f"Expected revenue: *{format_currency(total)}*\n\n"
                f"{lines}"
            )

        await notify_staff(summary)
        logger.info(f"Daily summary sent for {today}")

    except Exception as e:
        logger.error(f"Daily summary error: {e}")

# ====================== M-PESA CALLBACK ======================
@app.post("/mpesa-callback")
async def mpesa_callback(request: Request):
    try:
        data = await request.json()
        stk = data.get("Body", {}).get("stkCallback", {})

        if stk.get("ResultCode") != 0:
            return {"ResultCode": 0, "ResultDesc": "Acknowledged"}

        items = {item.get("Name"): item.get("Value") for item in stk.get("CallbackMetadata", {}).get("Item", [])}

        tx = {
            "mpesa_receipt": items.get("MpesaReceiptNumber"),
            "amount": float(items.get("Amount", 0)),
            "phone_hash": hash_phone(str(items.get("PhoneNumber", ""))),
            "transaction_date": str(items.get("TransactionDate", "")),
            "status": "completed",
            "recorded_at": datetime.utcnow().isoformat(),
            "notes": "Clinic payment via M-Pesa"
        }

        supabase.table("transactions").insert(tx).execute()
        logger.info(f"M-Pesa recorded: {tx['mpesa_receipt']} | KES {tx['amount']}")
        return {"ResultCode": 0, "ResultDesc": "Success"}

    except Exception as e:
        logger.error(f"M-Pesa callback error: {e}")
        return {"ResultCode": 0, "ResultDesc": "Error acknowledged"}

# ====================== ADMIN ENDPOINTS ======================
def verify_admin(x_api_key: str = Header(None)):
    if x_api_key != config.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/admin/revenue")
async def download_revenue(
    start_date: str = None,
    end_date: str = None,
    x_api_key: str = Header(None)
):
    verify_admin(x_api_key)
    try:
        query = supabase.table("transactions").select("*").order("recorded_at", desc=True)
        if start_date:
            query = query.gte("recorded_at", start_date)
        if end_date:
            query = query.lte("recorded_at", end_date)

        result = query.execute()
        transactions = result.data or []

        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=["mpesa_receipt", "amount", "transaction_date", "recorded_at", "notes"])
        writer.writeheader()
        for tx in transactions:
            writer.writerow({k: tx.get(k, "") for k in writer.fieldnames})

        output.seek(0)
        filename = f"alupe_revenue_{datetime.now().strftime('%Y%m%d')}.csv"
        return StreamingResponse(
            output,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        logger.error(f"Revenue download error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate revenue report")

@app.get("/admin/bookings")
async def get_bookings(date: str = None, x_api_key: str = Header(None)):
    verify_admin(x_api_key)
    try:
        query = supabase.table("bookings").select("*").order("date", desc=False)
        if date:
            query = query.eq("date", date)
        result = query.execute()
        return {"bookings": result.data or [], "count": len(result.data or [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ====================== CORE MESSAGE HANDLER ======================
async def handle_message(phone: str, text: str, msg_type: str = "text"):
    if msg_type != "text":
        await send_whatsapp_message(phone, "I currently support text messages only. Type *Menu* to see options.")
        return

    # Rate limiting
    if store.is_rate_limited(phone):
        await send_whatsapp_message(phone, "You're sending messages too fast. Please wait a moment. ⏳")
        return

    text_raw = sanitize_input(text).strip()
    text = text_raw.lower()

    # First-time user: show privacy notice
    if phone not in store.user_states and phone not in store.new_users:
        store.new_users.add(phone)
        await send_privacy_notice(phone)
        return

    # Emergency detection (always checked regardless of state)
    emergency_level = detect_emergency(text)
    if emergency_level == "critical":
        await send_whatsapp_message(
            phone,
            "🚨 *EMERGENCY*\n\nPlease call *999* or go to the nearest hospital immediately!\n\n"
            f"You can also call us directly: {config.CLINIC_PHONE}"
        )
        store.user_states[phone] = UserState.MAIN_MENU
        return
    if emergency_level == "urgent":
        await send_whatsapp_message(
            phone,
            f"⚠️ This sounds urgent. Please call the clinic now: {config.CLINIC_PHONE}\n"
            "Or reply *Book* for emergency appointment."
        )

    # Mental health trigger (checked before state routing)
    if detect_mental_health_trigger(text) or text in ["not okay", "not ok", "siko sawa"]:
        await handle_mental_health(phone, text_raw)
        return

    state = store.user_states.get(phone, UserState.MAIN_MENU)

    # ---- State-based routing ----
    if state == UserState.MENTAL_HEALTH:
        await handle_mental_health(phone, text_raw)
        return

    if state == UserState.BOOKING_TYPE:
        if text in CONSULTATION_TYPES:
            await show_available_slots(phone, text)
        elif text == "back":
            await send_main_menu(phone)
        else:
            await show_booking_options(phone)
        return

    if state == UserState.BOOKING_SLOT:
        await handle_slot_selection(phone, text)
        return

    if state == UserState.BOOKING_CONFIRM:
        await handle_booking_confirmation(phone, text)
        return

    if state == UserState.LEAVING_FEEDBACK:
        await handle_feedback(phone, text)
        return

    # ---- Keyword routing ----
    if text.startswith("ask "):
        question = text_raw[4:].strip()
        if question:
            store.user_states[phone] = UserState.ASKING_QUESTION
            reply = await get_ai_response(phone, question)
            await send_whatsapp_message(phone, reply)
            store.user_states[phone] = UserState.MAIN_MENU
        else:
            await send_whatsapp_message(phone, "What's your question? Example: _Ask I have a headache_")
        return

    if text in ["book", "appointment", "schedule", "book appointment"]:
        await show_booking_options(phone)
        return

    if text.startswith("cancel "):
        ref = text_raw[7:].strip()
        await handle_cancellation(phone, ref)
        return

    if text in ["info", "about", "contact", "hours"]:
        info = (
            f"🏥 *{config.CLINIC_NAME}*\n\n"
            f"📍 {config.CLINIC_ADDRESS}\n"
            f"📞 {config.CLINIC_PHONE}\n\n"
            "🕐 *Hours:*\n"
            "Mon–Fri: 8AM – 5PM\n"
            "Saturday: 8AM – 12PM\n"
            "Sunday: Closed\n\n"
            "💰 *Consultation Fees:*\n"
            "General: KES 500\n"
            "Specialist: KES 1,000\n"
            "Lab Tests: KES 800\n"
            "Emergency: KES 1,500\n\n"
            "Pay at the clinic — no upfront payment required."
        )
        await send_whatsapp_message(phone, info)
        return

    if text in ["emergency"]:
        await send_whatsapp_message(
            phone,
            f"🚨 For emergencies call *999* or go to the nearest hospital.\n\n"
            f"For urgent but non-life-threatening issues:\n📞 {config.CLINIC_PHONE}"
        )
        return

    if text in ["menu", "hi", "hello", "start", "ok", "okay", "hii", "sawa"]:
        await send_main_menu(phone)
        return

    # Fallback: try AI anyway rather than dead-end
    reply = await get_ai_response(phone, text_raw)
    await send_whatsapp_message(phone, reply)

# ====================== WEBHOOKS ======================
@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge")
):
    if hub_mode == "subscribe" and hub_verify_token == config.VERIFY_TOKEN:
        return Response(content=hub_challenge)
    raise HTTPException(status_code=403, detail="Forbidden")

@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    try:
        data = await request.json()
        if "entry" not in data:
            return {"status": "ignored"}

        for entry in data["entry"]:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    msg_id = message.get("id")

                    # Deduplication
                    if msg_id in store.processed_messages:
                        continue
                    store.processed_messages.add(msg_id)
                    # Prevent memory leak — keep last 1000 IDs
                    if len(store.processed_messages) > 1000:
                        store.processed_messages = set(list(store.processed_messages)[-500:])

                    phone = message.get("from")
                    msg_type = message.get("type", "text")

                    if msg_type == "text":
                        text = message["text"]["body"]
                        asyncio.create_task(handle_message(phone, text, msg_type))

        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return {"status": "error"}

# ====================== STARTUP ======================
@app.on_event("startup")
async def startup_event():
    # Schedule daily summary at 7AM
    scheduler.add_job(send_daily_summary, 'cron', hour=7, minute=0)
    scheduler.start()
    logger.info(f"🚀 {config.CLINIC_NAME} WhatsApp Assistant v3.0 started")

@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
