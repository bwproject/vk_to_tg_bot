import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import asyncio
import threading
import os
import requests
import logging
from datetime import datetime
import pytz
from dotenv import load_dotenv
from collections import OrderedDict

# Настройка логирования
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()

# Конфигурация
VK_USER_TOKEN = os.getenv("VK_USER_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AUTHORIZED_TELEGRAM_USER_ID = os.getenv("AUTHORIZED_TELEGRAM_USER_ID")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# Инициализация VK API
vk_session = vk_api.VkApi(token=VK_USER_TOKEN)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

class DialogManager:
    def __init__(self):
        self.dialogs = OrderedDict()
        self.selected_dialogs = {}
        self.selected_friends = {}

    def select_dialog(self, telegram_user_id, vk_user_id):
        self.selected_dialogs[telegram_user_id] = vk_user_id

    def get_selected(self, telegram_user_id):
        return self.selected_dialogs.get(telegram_user_id)

dialog_manager = DialogManager()

async def send_to_telegram(text, attachments=None):
    """Отправляет сообщение и вложения в Telegram."""
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    
    if attachments:
        for attach in attachments:
            await application.bot.send_document(chat_id=TELEGRAM_CHAT_ID, document=attach)

def vk_listener(loop):
    """Слушает новые сообщения и посты из VK."""
    for event in longpoll.listen():
        if event.type == VkEventType.MESSAGE_NEW and event.to_me:
            asyncio.run_coroutine_threadsafe(
                send_to_telegram(f"📩 Сообщение от {event.user_id}:\n{event.text}"), loop
            )
        
        elif event.type == VkEventType.WALL_POST_NEW:
            post = event.raw['object']
            text = post.get("text", "")
            owner_id = post["owner_id"]
            post_url = f"https://vk.com/wall{owner_id}_{post['id']}"
            msg = f"📝 Новый пост:\n{text}\n\n🔗 {post_url}"
            asyncio.run_coroutine_threadsafe(send_to_telegram(msg), loop)

async def show_friends(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит список друзей и кнопки для выбора."""
    friends = vk.friends.get(order="hints", fields="first_name,last_name")
    friends_list = friends.get("items", [])

    if not friends_list:
        await update.message.reply_text("❌ У вас нет друзей в VK.")
        return

    keyboard = [
        [InlineKeyboardButton(f"{f['first_name']} {f['last_name']}", callback_data=f"friend_{f['id']}")]
        for f in friends_list[:10]
    ]

    await update.message.reply_text("👥 Выберите друга для начала диалога:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок (выбор друга)."""
    query = update.callback_query
    await query.answer()

    if query.data.startswith("friend_"):
        user_id = int(query.data.split("_")[1])
        dialog_manager.select_dialog(str(update.effective_user.id), user_id)
        await query.edit_message_text(f"✅ Выбран друг {user_id}. Теперь можно писать ему сообщения.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет сообщение выбранному другу."""
    user_id = str(update.effective_user.id)
    selected_vk_id = dialog_manager.get_selected(user_id)

    if not selected_vk_id:
        await update.message.reply_text("⚠ Сначала выберите друга командой /friends.")
        return

    vk.messages.send(user_id=selected_vk_id, message=update.message.text, random_id=0)
    await update.message.reply_text("✅ Сообщение отправлено.")

def main():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("friends", show_friends))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT, handle_message))

    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()

    application.run_polling()

if __name__ == "__main__":
    main()