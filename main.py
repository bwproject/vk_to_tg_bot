import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import asyncio
import threading
from dotenv import load_dotenv
import os
import logging
import requests
import json
import time
from collections import OrderedDict

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs.txt"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

load_dotenv()

# Конфигурация
VK_USER_TOKEN = os.getenv("VK_USER_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AUTHORIZED_TELEGRAM_USER_ID = os.getenv("AUTHORIZED_TELEGRAM_USER_ID")
MESSAGE_SIGNATURE = os.getenv("MESSAGE_SIGNATURE", "\n\n(отправлено с помощью tg бота)")
MAX_DIALOGS = 10

if not all([VK_USER_TOKEN, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, AUTHORIZED_TELEGRAM_USER_ID]):
    raise ValueError("Не все обязательные переменные окружения заданы в .env!")

# Инициализация VK
vk_session = vk_api.VkApi(token=VK_USER_TOKEN)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

# Утилиты
def get_user_info(user_id):
    try:
        response = vk.users.get(user_ids=user_id, fields="first_name,last_name")
        return response[0] if response else {}
    except Exception as e:
        logger.error(f"Ошибка получения информации о пользователе: {e}")
        return {}

def download_file(url):
    try:
        response = requests.get(url, stream=True, timeout=10)
        if response.status_code == 200:
            filename = os.path.join("/tmp", os.path.basename(url))
            with open(filename, "wb") as f:
                for chunk in response.iter_content(1024):
                    f.write(chunk)
            return filename
    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}")
    return None

class DialogManager:
    def __init__(self):
        self.dialogs = OrderedDict()
        self.selected_dialogs = {}

    def update_dialog(self, user_id, message, attachments=None):
        if user_id in self.dialogs:
            self.dialogs.move_to_end(user_id)
        else:
            if len(self.dialogs) >= MAX_DIALOGS:
                self.dialogs.popitem(last=False)
            self.dialogs[user_id] = {
                'info': get_user_info(user_id),
                'last_msg': (message[:50] + '...') if len(message) > 50 else message,
                'attachments': attachments or [],
                'time': time.time()
            }

    def get_dialogs(self):
        return list(self.dialogs.items())

    def select_dialog(self, telegram_user_id, vk_user_id):
        self.selected_dialogs[telegram_user_id] = vk_user_id

    def get_selected(self, telegram_user_id):
        return self.selected_dialogs.get(telegram_user_id)

dialog_manager = DialogManager()

# Обработчики VK
def vk_listener(loop):
    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                    user_id = event.user_id
                    dialog_manager.update_dialog(user_id, event.text, event.attachments)
                    asyncio.run_coroutine_threadsafe(
                        forward_to_telegram(user_id, event.text, event.attachments),
                        loop
                    )
        except Exception as e:
            logger.error(f"Ошибка в VK listener: {e}")
            time.sleep(5)

async def forward_to_telegram(user_id, text, attachments):
    try:
        user_info = get_user_info(user_id)
        dialog_info = f"📨 От {user_info.get('first_name', 'Неизвестный')} {user_info.get('last_name', '')}"

        await application.bot.send_message(TELEGRAM_CHAT_ID, text=f"{dialog_info}:\n{text}")

        if isinstance(attachments, str):
            attachments = attachments.split(",")

        for attach in attachments:
            try:
                logger.debug(f"Обрабатываем вложение: {attach}")

                if isinstance(attach, str):
                    await application.bot.send_message(TELEGRAM_CHAT_ID, text=f"🔗 Вложение: https://vk.com/{attach}")
                    continue

                if attach['type'] == 'photo':
                    photo = max(attach['photo']['sizes'], key=lambda x: x['width'])
                    await application.bot.send_photo(TELEGRAM_CHAT_ID, photo=photo['url'])

                elif attach['type'] == 'doc':
                    await application.bot.send_document(TELEGRAM_CHAT_ID, document=attach['doc']['url'])

                elif attach['type'] == 'audio_message':
                    audio_url = attach['audio_message']['link_ogg']
                    filepath = download_file(audio_url)
                    if filepath:
                        with open(filepath, 'rb') as audio_file:
                            await application.bot.send_voice(TELEGRAM_CHAT_ID, voice=audio_file)

                else:
                    logger.warning(f"Неизвестный тип вложения: {attach['type']}")

            except Exception as e:
                logger.error(f"Ошибка обработки вложения: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"Ошибка пересылки в Telegram: {e}", exc_info=True)

# Обработчики Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != AUTHORIZED_TELEGRAM_USER_ID:
        await update.message.reply_text("⛔ Доступ запрещен")
        return
    await show_dialogs(update, context)

async def show_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dialogs = dialog_manager.get_dialogs()
    if not dialogs:
        await update.message.reply_text("🤷 Нет активных диалогов")
        return

    keyboard = [
        [InlineKeyboardButton(f"{dialog['info'].get('first_name', '?')} {dialog['info'].get('last_name', '?')}", callback_data=f"select_{user_id}")]
        for user_id, dialog in dialogs
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📋 Выберите диалог:", reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("select_"):
        return

    selected_vk_id = int(query.data.split("_")[1])
    dialog_manager.select_dialog(str(query.from_user.id), selected_vk_id)
    user_info = get_user_info(selected_vk_id)
    await query.edit_message_text(f"✅ Выбран диалог с {user_info.get('first_name', 'Неизвестный')} {user_info.get('last_name', 'Пользователь')}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != AUTHORIZED_TELEGRAM_USER_ID:
        return

    selected_vk_id = dialog_manager.get_selected(user_id)
    if not selected_vk_id:
        await update.message.reply_text("⚠ Сначала выберите диалог /dialogs")
        return

    vk.messages.send(user_id=selected_vk_id, message=update.message.text + MESSAGE_SIGNATURE, random_id=0)
    await update.message.reply_text("✅ Сообщение отправлено")

def main():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("dialogs", show_dialogs))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT, handle_message))

    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()

    application.run_polling()

if __name__ == "__main__":
    main()