
import json
import logging
import os
import re
import sys
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Windows konsoli ko'pincha cp1251/cp866 kodlashda ishlaydi va emoji/o'zbek
# belgilarini chop etishda UnicodeEncodeError bilan qulab tushadi — UTF-8 ga
# majburlaymiz.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

load_dotenv()

# ============================================================
#  1) SOZLAMALAR
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

TIMEZONE = ZoneInfo("Asia/Tashkent")
REMINDER_TIME = dtime(10, 0, tzinfo=TIMEZONE)

BASE_DIR = Path(__file__).resolve().parent
MEETINGS_FILE = BASE_DIR / "meetings.json"
GROUPS_FILE = BASE_DIR / "groups.json"

DAYS = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]
DAY_LABELS = {
    "dushanba": "Dushanba", "seshanba": "Seshanba", "chorshanba": "Chorshanba",
    "payshanba": "Payshanba", "juma": "Juma", "shanba": "Shanba", "yakshanba": "Yakshanba",
}

# PTB JobQueue.run_daily() dagi "days" 0 = dushanba (Monday), 6 = yakshanba (Sunday).
# Python datetime.weekday() ham xuddi shunday: 0 = dushanba.
PTB_WEEKDAY = {
    "dushanba": 0, "seshanba": 1, "chorshanba": 2,
    "payshanba": 3, "juma": 4, "shanba": 5, "yakshanba": 6,
}

FIELD_LABELS = {
    "day": "📅 Kun",
    "time": "🕒 Vaqt",
    "type": "📌 Turi",
    "participants": "👥 Ishtirokchilar",
    "responsible": "👤 Asosiy mas'ul",
    "assistant": "🧑‍💼 Javobgar",
}

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("meeting_bot")


# ============================================================
#  2) SAQLASH (meetings.json / groups.json)
# ============================================================

def _seed_if_missing():
    if not MEETINGS_FILE.exists():
        data = {"next_id": 1, "meetings": []}
        MEETINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    if not GROUPS_FILE.exists():
        GROUPS_FILE.write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_meetings():
    _seed_if_missing()
    return json.loads(MEETINGS_FILE.read_text(encoding="utf-8"))


def save_meetings(data):
    MEETINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_groups():
    _seed_if_missing()
    return json.loads(GROUPS_FILE.read_text(encoding="utf-8"))


def save_groups(groups):
    GROUPS_FILE.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")


def get_meeting(data, meeting_id):
    for m in data["meetings"]:
        if m["id"] == meeting_id:
            return m
    return None


def known_projects():
    """meetings.json va groups.json dagi barcha loyiha nomlarini birlashtiradi."""
    data = load_meetings()
    groups = load_groups()
    names = {m["project"] for m in data["meetings"]} | set(groups.keys())
    return sorted(names)


# ============================================================
#  3) ESLATMA YUBORISH VA REJALASHTIRISH
# ============================================================

def build_message(m):
    return (
        "📢 Bugun majlis bor!\n\n"
        f"🗓 Loyiha/bo'lim: {m['project']}\n"
        f"📌 Turi: {m['type']}\n"
        f"🕒 Vaqt: {m['time']}\n"
        f"👥 Ishtirokchilar: {m['participants']}\n"
        f"👤 Asosiy mas'ul: {m['responsible']}\n"
        f"🧑‍💼 Javobgar: {m['assistant']}"
    )


async def send_meeting_reminder(bot, m):
    """Majlis eslatmasini guruhga yuboradi. (muvaffaqiyat, xabar) qaytaradi."""
    group = load_groups().get(m["project"])
    if not group or group.get("chat_id") is None:
        return False, f"'{m['project']}' uchun guruh sozlanmagan."
    kwargs = {"chat_id": group["chat_id"], "text": build_message(m)}
    if group.get("topic_id"):
        kwargs["message_thread_id"] = group["topic_id"]
    try:
        await bot.send_message(**kwargs)
        return True, "Yuborildi."
    except Exception as e:
        return False, str(e)


async def send_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    m = context.job.data
    ok, msg = await send_meeting_reminder(context.bot, m)
    if ok:
        logger.info("Eslatma yuborildi: %s (%s)", m["project"], m["time"])
        return

    logger.warning("Eslatma yuborilmadi (%s): %s", m["project"], msg)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"⚠️ '{m['project']}' uchun eslatma guruhga YUBORILMADI!\n"
                    f"Sabab: {msg}\n\n"
                    "Guruh sozlamalarini tekshiring: /start → ⚙️ Guruh sozlamalari, "
                    "yoki o'sha guruhda /register buyrug'ini qayta yozing."
                ),
            )
        except Exception:
            pass


def reschedule_all(app: Application):
    for job in app.job_queue.jobs():
        if job.name and job.name.startswith("reminder-"):
            job.schedule_removal()

    data = load_meetings()
    count = 0
    for m in data["meetings"]:
        if not TIME_RE.match(m["time"]) or m["day"] not in DAYS:
            logger.warning("Noto'g'ri jadval yozuvi o'tkazib yuborildi: %s", m)
            continue
        weekday_idx = PTB_WEEKDAY[m["day"]]
        app.job_queue.run_daily(
            send_reminder_job, time=REMINDER_TIME, days=(weekday_idx,), data=m, name=f"reminder-{m['id']}"
        )
        count += 1
    logger.info("Jadval yangilandi: %d ta eslatma rejalashtirildi.", count)


async def post_init(app: Application):
    reschedule_all(app)


# ============================================================
#  4) RUXSAT TEKSHIRUVI
# ============================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


async def deny(update: Update):
    if update.callback_query:
        await update.callback_query.answer("Sizda ruxsat yo'q.", show_alert=True)
    elif update.message:
        await update.message.reply_text(
            "Sizda ruxsat yo'q. Administratorga /myid buyrug'i natijasini yuboring."
        )


# ============================================================
#  5) TUGMALI MENYULAR
# ============================================================

def kb(rows):
    return InlineKeyboardMarkup(rows)


def main_menu_kb():
    return kb([
        [InlineKeyboardButton("📋 Majlislarni tahrirlash", callback_data="list")],
        [InlineKeyboardButton("➕ Yangi majlis qo'shish", callback_data="add")],
        [InlineKeyboardButton("⚙️ Guruh sozlamalari", callback_data="groups")],
    ])


def back_button(callback_data="menu"):
    return InlineKeyboardButton("⬅️ Bosh menyu", callback_data=callback_data)


def projects_list_kb(prefix):
    rows = [[InlineKeyboardButton(p, callback_data=f"{prefix}:{p}")] for p in known_projects()]
    rows.append([back_button()])
    return kb(rows)


def add_projects_kb():
    rows = [[InlineKeyboardButton(p, callback_data=f"addproj:{p}")] for p in known_projects()]
    rows.append([InlineKeyboardButton("➕ Yangi loyiha", callback_data="addproj:new")])
    rows.append([back_button()])
    return kb(rows)


def meetings_kb(data, project):
    rows = []
    for m in data["meetings"]:
        if m["project"] == project:
            label = f"{DAY_LABELS[m['day']]} {m['time']} — {m['type']}"
            rows.append([InlineKeyboardButton(label, callback_data=f"meet:{m['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Loyihalar", callback_data="list")])
    return kb(rows)


def field_menu_kb(meeting_id):
    rows = [
        [InlineKeyboardButton(FIELD_LABELS["day"], callback_data=f"fld:{meeting_id}:day"),
         InlineKeyboardButton(FIELD_LABELS["time"], callback_data=f"fld:{meeting_id}:time")],
        [InlineKeyboardButton(FIELD_LABELS["type"], callback_data=f"fld:{meeting_id}:type"),
         InlineKeyboardButton(FIELD_LABELS["participants"], callback_data=f"fld:{meeting_id}:participants")],
        [InlineKeyboardButton(FIELD_LABELS["responsible"], callback_data=f"fld:{meeting_id}:responsible"),
         InlineKeyboardButton(FIELD_LABELS["assistant"], callback_data=f"fld:{meeting_id}:assistant")],
        [InlineKeyboardButton("🗑 O'chirish", callback_data=f"fld:{meeting_id}:delete")],
        [InlineKeyboardButton("📨 Sinov xabari yuborish", callback_data=f"test:{meeting_id}")],
        [InlineKeyboardButton("⬅️ Loyihalar", callback_data="list")],
    ]
    return kb(rows)


def day_kb(action_prefix, extra=""):
    rows, row = [], []
    for d in DAYS:
        cb = f"{action_prefix}:{extra}:{d}" if extra else f"{action_prefix}:{d}"
        row.append(InlineKeyboardButton(DAY_LABELS[d], callback_data=cb))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([back_button()])
    return kb(rows)


def confirm_kb(yes_data, no_data):
    return kb([[InlineKeyboardButton("✅ Ha", callback_data=yes_data),
                InlineKeyboardButton("❌ Yo'q", callback_data=no_data)]])


def meeting_summary(m):
    return (
        f"🗓 {m['project']}\n"
        f"📅 Kun: {DAY_LABELS[m['day']]}\n"
        f"🕒 Vaqt: {m['time']}\n"
        f"📌 Turi: {m['type']}\n"
        f"👥 Ishtirokchilar: {m['participants']}\n"
        f"👤 Asosiy mas'ul: {m['responsible']}\n"
        f"🧑‍💼 Javobgar: {m['assistant']}"
    )


def group_summary(name, group):
    chat_id = group.get("chat_id")
    topic_id = group.get("topic_id")
    return (
        f"⚙️ {name}\n"
        f"Chat ID: {chat_id if chat_id is not None else 'sozlanmagan'}\n"
        f"Topic ID: {topic_id if topic_id is not None else 'umumiy mavzu'}"
    )


def group_detail_kb(name):
    return kb([
        [InlineKeyboardButton("✏️ Chat ID", callback_data=f"gfld:{name}:chatid"),
         InlineKeyboardButton("✏️ Topic ID", callback_data=f"gfld:{name}:topicid")],
        [InlineKeyboardButton("⬅️ Guruhlar", callback_data="groups")],
    ])


async def render(update: Update, text, markup=None):
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


# ============================================================
#  6) BUYRUQLAR
# ============================================================

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    thread_id = update.message.message_thread_id
    text = (
        f"Sizning Telegram ID: {update.effective_user.id}\n"
        f"Joriy chat ID: {chat.id}\n"
        f"Chat turi: {chat.type}\n"
    )
    if thread_id:
        text += f"Joriy mavzu (topic) ID: {thread_id}\n"
    text += (
        "\nAgar bu guruh bo'lsa, 'Joriy chat ID' — guruhning ID si. "
        "Agar bu xabar biror mavzu (topic) ichida yozilgan bo'lsa, "
        "yuqoridagi 'Joriy mavzu ID' — aynan o'sha mavzuning ID si "
        "(guruh sozlamalarida Topic ID sifatida shuni kiriting)."
    )
    await update.message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await deny(update)
        return
    context.user_data.clear()
    await render(update, "Salom! Nima qilmoqchisiz?", main_menu_kb())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await deny(update)
        return
    context.user_data.clear()
    await update.message.reply_text("Bekor qilindi.")
    await update.message.reply_text("Bosh menyu:", reply_markup=main_menu_kb())


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await deny(update)
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "Bu buyruqni shaxsiy chatda emas, balki loyihaga tegishli haqiqiy "
            "guruhda (agar mavzular yoqilgan bo'lsa — aynan kerakli mavzu ichida) yozing."
        )
        return
    context.chat_data["register_thread_id"] = update.message.message_thread_id
    rows = [[InlineKeyboardButton(p, callback_data=f"reg:{p}")] for p in known_projects()]
    await update.message.reply_text(
        "Shu chat (va mavzu, agar bo'lsa) qaysi loyihaga bog'lansin?",
        reply_markup=kb(rows),
    )


# ============================================================
#  7) TUGMA BOSILGANDA (CALLBACK)
# ============================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await deny(update)
        return
    await query.answer()
    data = query.data
    ud = context.user_data

    if data == "menu":
        ud.clear()
        await render(update, "Bosh menyu:", main_menu_kb())
        return

    if data == "list":
        ud.clear()
        await render(update, "Qaysi loyiha majlisini tahrirlaysiz?", projects_list_kb("proj"))
        return

    if data.startswith("proj:"):
        project = data.split(":", 1)[1]
        mdata = load_meetings()
        matches = [m for m in mdata["meetings"] if m["project"] == project]
        if not matches:
            await render(update, "Bu loyihada hozircha majlis yo'q.", projects_list_kb("proj"))
        elif len(matches) == 1:
            m = matches[0]
            await render(update, meeting_summary(m), field_menu_kb(m["id"]))
        else:
            await render(update, f"{project} — qaysi majlis?", meetings_kb(mdata, project))
        return

    if data.startswith("meet:"):
        meeting_id = int(data.split(":", 1)[1])
        mdata = load_meetings()
        m = get_meeting(mdata, meeting_id)
        if not m:
            await render(update, "Bu majlis topilmadi (o'chirilgan bo'lishi mumkin).", main_menu_kb())
            return
        await render(update, meeting_summary(m), field_menu_kb(meeting_id))
        return

    if data.startswith("fld:"):
        _, meeting_id, field = data.split(":", 2)
        meeting_id = int(meeting_id)
        mdata = load_meetings()
        m = get_meeting(mdata, meeting_id)
        if not m:
            await render(update, "Bu majlis topilmadi.", main_menu_kb())
            return
        if field == "day":
            await render(update, "Yangi kunni tanlang:", day_kb("day", str(meeting_id)))
        elif field == "delete":
            await render(update, f"{meeting_summary(m)}\n\nRostdan o'chirasizmi?",
                          confirm_kb(f"delyes:{meeting_id}", f"delno:{meeting_id}"))
        elif field in FIELD_LABELS:
            ud["awaiting"] = ("edit", meeting_id, field)
            await render(update, f"{FIELD_LABELS[field]} uchun yangi qiymatni yozing:")
        return

    if data.startswith("test:"):
        meeting_id = int(data.split(":", 1)[1])
        mdata = load_meetings()
        m = get_meeting(mdata, meeting_id)
        if not m:
            await render(update, "Bu majlis topilmadi.", main_menu_kb())
            return
        ok, msg = await send_meeting_reminder(context.bot, m)
        status = "✅ Sinov xabari guruhga yuborildi!" if ok else f"❌ Yuborilmadi: {msg}"
        await render(update, f"{status}\n\n{meeting_summary(m)}", field_menu_kb(meeting_id))
        return

    if data.startswith("reg:"):
        name = data.split(":", 1)[1]
        chat = update.effective_chat
        thread_id = context.chat_data.pop("register_thread_id", None)
        groups = load_groups()
        groups[name] = {"chat_id": chat.id, "topic_id": thread_id}
        save_groups(groups)
        await query.edit_message_text(
            f"✅ '{name}' shu chatga bog'landi.\n"
            f"Chat ID: {chat.id}\n"
            f"Topic ID: {thread_id if thread_id else 'umumiy mavzu'}"
        )
        return

    if data.startswith("day:"):
        _, meeting_id, day = data.split(":", 2)
        meeting_id = int(meeting_id)
        mdata = load_meetings()
        m = get_meeting(mdata, meeting_id)
        if m:
            m["day"] = day
            save_meetings(mdata)
            reschedule_all(context.application)
            await render(update, f"✅ Kun yangilandi.\n\n{meeting_summary(m)}", field_menu_kb(meeting_id))
        return

    if data.startswith("delyes:"):
        meeting_id = int(data.split(":", 1)[1])
        mdata = load_meetings()
        mdata["meetings"] = [m for m in mdata["meetings"] if m["id"] != meeting_id]
        save_meetings(mdata)
        reschedule_all(context.application)
        await render(update, "🗑 Majlis o'chirildi.", projects_list_kb("proj"))
        return

    if data.startswith("delno:"):
        meeting_id = int(data.split(":", 1)[1])
        mdata = load_meetings()
        m = get_meeting(mdata, meeting_id)
        if m:
            await render(update, meeting_summary(m), field_menu_kb(meeting_id))
        return

    if data == "groups":
        ud.clear()
        await render(update, "Qaysi loyiha guruhini sozlaysiz?", projects_list_kb("gproj"))
        return

    if data.startswith("gproj:"):
        name = data.split(":", 1)[1]
        group = load_groups().get(name, {"chat_id": None, "topic_id": None})
        await render(update, group_summary(name, group), group_detail_kb(name))
        return

    if data.startswith("gfld:"):
        _, name, field = data.split(":", 2)
        if field == "chatid":
            ud["awaiting"] = ("group_chatid", name)
            await render(update, "Guruh chat_id sini yozing (masalan -1001234567890):")
        elif field == "topicid":
            ud["awaiting"] = ("group_topicid", name)
            await render(
                update,
                "Topic ID ni yozing, yoki umumiy mavzuga qaytarish uchun tugmani bosing:",
                kb([[InlineKeyboardButton("Umumiy mavzu (tozalash)", callback_data=f"gcleartopic:{name}")]]),
            )
        return

    if data.startswith("gcleartopic:"):
        name = data.split(":", 1)[1]
        groups = load_groups()
        groups.setdefault(name, {"chat_id": None, "topic_id": None})
        groups[name]["topic_id"] = None
        save_groups(groups)
        await render(update, group_summary(name, groups[name]), group_detail_kb(name))
        return

    if data == "add":
        ud.clear()
        ud["draft"] = {}
        await render(update, "Qaysi loyiha uchun majlis qo'shasiz?", add_projects_kb())
        return

    if data.startswith("addproj:"):
        name = data.split(":", 1)[1]
        if name == "new":
            ud["awaiting"] = ("add_new_project_name",)
            await render(update, "Yangi loyiha nomini yozing:")
            return
        ud.setdefault("draft", {})["project"] = name
        await render(update, "Qaysi kun?", day_kb("addday"))
        return

    if data.startswith("addday:"):
        day = data.split(":", 1)[1]
        ud.setdefault("draft", {})["day"] = day
        ud["awaiting"] = ("add_time",)
        await render(update, "Soat nechida boshlanadi? (HH:MM, masalan 09:00):")
        return

    if data == "skiptopic":
        ud.setdefault("draft_group", {})["topic_id"] = None
        await _finish_new_group_and_continue(update, context)
        return

    if data == "addsave":
        draft = ud.get("draft", {})
        mdata = load_meetings()
        new_id = mdata["next_id"]
        mdata["next_id"] += 1
        meeting = {"id": new_id, **draft}
        mdata["meetings"].append(meeting)
        save_meetings(mdata)
        reschedule_all(context.application)
        ud.clear()
        await render(update, f"✅ Majlis qo'shildi!\n\n{meeting_summary(meeting)}", main_menu_kb())
        return

    if data == "addcancel":
        ud.clear()
        await render(update, "Bekor qilindi.", main_menu_kb())
        return


# ============================================================
#  8) MATN YOZILGANDA (yangi qiymat kiritish)
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        await deny(update)
        return

    ud = context.user_data
    awaiting = ud.get("awaiting")
    if not awaiting:
        await update.message.reply_text(
            "Buyruq tugmalar orqali beriladi. Bosh menyu uchun /start yozing."
        )
        return

    text = update.message.text.strip()
    kind = awaiting[0]

    if kind == "edit":
        _, meeting_id, field = awaiting
        if field == "time" and not TIME_RE.match(text):
            await update.message.reply_text("Noto'g'ri format. HH:MM ko'rinishida yozing (masalan 14:30):")
            return
        mdata = load_meetings()
        m = get_meeting(mdata, meeting_id)
        if not m:
            ud.pop("awaiting", None)
            await update.message.reply_text("Bu majlis topilmadi.", reply_markup=main_menu_kb())
            return
        m[field] = text
        save_meetings(mdata)
        reschedule_all(context.application)
        ud.pop("awaiting", None)
        await update.message.reply_text(
            f"✅ Yangilandi.\n\n{meeting_summary(m)}", reply_markup=field_menu_kb(meeting_id)
        )
        return

    if kind == "group_chatid":
        _, name = awaiting
        if not re.match(r"^-?\d+$", text):
            await update.message.reply_text("chat_id faqat raqamlardan iborat bo'lishi kerak. Qayta yozing:")
            return
        groups = load_groups()
        groups.setdefault(name, {"chat_id": None, "topic_id": None})
        groups[name]["chat_id"] = int(text)
        save_groups(groups)
        ud.pop("awaiting", None)
        await update.message.reply_text(
            f"✅ Yangilandi.\n\n{group_summary(name, groups[name])}", reply_markup=group_detail_kb(name)
        )
        return

    if kind == "group_topicid":
        _, name = awaiting
        if not re.match(r"^\d+$", text):
            await update.message.reply_text("topic_id faqat musbat raqam bo'lishi kerak. Qayta yozing:")
            return
        groups = load_groups()
        groups.setdefault(name, {"chat_id": None, "topic_id": None})
        groups[name]["topic_id"] = int(text)
        save_groups(groups)
        ud.pop("awaiting", None)
        await update.message.reply_text(
            f"✅ Yangilandi.\n\n{group_summary(name, groups[name])}", reply_markup=group_detail_kb(name)
        )
        return

    if kind == "add_new_project_name":
        ud.setdefault("draft", {})["project"] = text
        ud["draft_group"] = {"chat_id": None, "topic_id": None}
        ud["awaiting"] = ("add_new_group_chatid",)
        await update.message.reply_text(
            "Bu yangi loyiha uchun Telegram guruh chat_id sini yozing (masalan -1001234567890):"
        )
        return

    if kind == "add_new_group_chatid":
        if not re.match(r"^-?\d+$", text):
            await update.message.reply_text("chat_id faqat raqamlardan iborat bo'lishi kerak. Qayta yozing:")
            return
        ud.setdefault("draft_group", {})["chat_id"] = int(text)
        ud["awaiting"] = ("add_new_group_topicid",)
        await update.message.reply_text(
            "Agar bu guruhda 'Mavzular' (Topics) yoqilgan bo'lsa, topic_id yozing. "
            "Aks holda pastdagi tugmani bosing.",
            reply_markup=kb([[InlineKeyboardButton("Umumiy mavzu (topic yo'q)", callback_data="skiptopic")]]),
        )
        return

    if kind == "add_new_group_topicid":
        if not re.match(r"^\d+$", text):
            await update.message.reply_text("topic_id faqat musbat raqam bo'lishi kerak. Qayta yozing:")
            return
        ud.setdefault("draft_group", {})["topic_id"] = int(text)
        await _finish_new_group_and_continue(update, context)
        return

    if kind == "add_time":
        if not TIME_RE.match(text):
            await update.message.reply_text("Noto'g'ri format. HH:MM ko'rinishida yozing (masalan 09:00):")
            return
        ud["draft"]["time"] = text
        ud["awaiting"] = ("add_type",)
        await update.message.reply_text("Majlis turi? (masalan Meeting, Tahlil, Sales Tahlil):")
        return

    if kind == "add_type":
        ud["draft"]["type"] = text
        ud["awaiting"] = ("add_participants",)
        await update.message.reply_text("Ishtirokchilar kim?")
        return

    if kind == "add_participants":
        ud["draft"]["participants"] = text
        ud["awaiting"] = ("add_responsible",)
        await update.message.reply_text("Asosiy mas'ul kim?")
        return

    if kind == "add_responsible":
        ud["draft"]["responsible"] = text
        ud["awaiting"] = ("add_assistant",)
        await update.message.reply_text("Javobgar kim?")
        return

    if kind == "add_assistant":
        ud["draft"]["assistant"] = text
        ud.pop("awaiting", None)
        await update.message.reply_text(
            f"Tekshirib ko'ring:\n\n{meeting_summary(ud['draft'])}",
            reply_markup=confirm_kb("addsave", "addcancel"),
        )
        return


async def _finish_new_group_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    name = ud["draft"]["project"]
    group = ud.pop("draft_group", {"chat_id": None, "topic_id": None})
    groups = load_groups()
    groups[name] = group
    save_groups(groups)
    ud.pop("awaiting", None)
    await render(update, f"✅ '{name}' guruhi sozlandi.\n\nQaysi kun?", day_kb("addday"))


# ============================================================
#  9) ISHGA TUSHIRISH
# ============================================================

def validate_config():
    problems = []
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN topilmadi (.env faylida BOT_TOKEN=... yozing).")
    if not ADMIN_IDS:
        problems.append(
            "ADMIN_IDS bo'sh — hech kim botni boshqara olmaydi. .env faylida "
            "ADMIN_IDS=111111111,222222222 kabi yozing (har bir admin botga "
            "/myid yozib o'z ID sini bilib oladi)."
        )
    if problems:
        print("Sozlamalarda xatolik:\n")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)


def main():
    validate_config()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bekor", cmd_cancel))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot ishga tushmoqda...")
    app.run_polling()


if __name__ == "__main__":
    main()
