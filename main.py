import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import asyncio
import threading
from dotenv import load_dotenv
import os
from collections import OrderedDict
import time
import requests
import logging
from datetime import datetime, timedelta
import pytz

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot_debug.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()

# Конфигурация
VK_USER_TOKEN = os.getenv("VK_USER_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AUTHORIZED_TELEGRAM_USER_ID = os.getenv("AUTHORIZED_TELEGRAM_USER_ID")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# Сообщения
ACCESS_DENIED_MESSAGE = os.getenv("ACCESS_DENIED_MESSAGE", "⛔ Доступ запрещен")
DIALOG_NOT_SELECTED_MESSAGE = os.getenv("DIALOG_NOT_SELECTED_MESSAGE", "⚠ Сначала выберите диалог /dialogs")
MESSAGE_SIGNATURE = os.getenv("MESSAGE_SIGNATURE", "\n\n(отправлено с помощью tg bota)")
BOT_STATUS_TEMPLATE = os.getenv("BOT_STATUS_TEMPLATE", "⌛ Бот работает: {uptime} | 📨 Сообщений: {message_count} | 🕒 Последнее: {last_time}")

MAX_DIALOGS = 10

# Проверка переменных окружения
if not all([VK_USER_TOKEN, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, AUTHORIZED_TELEGRAM_USER_ID]):
    raise ValueError("Не все обязательные переменные окружения заданы в .env!")

# Инициализация VK
vk_session = vk_api.VkApi(token=VK_USER_TOKEN, scope='photos,messages,docs')
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

class BotStats:
    def __init__(self):
        self.start_time = datetime.now(pytz.timezone(TIMEZONE))
        self.last_message_time = None
        self.message_count = 0

bot_stats = BotStats()

def is_url_accessible(url):
    """Проверяет доступность URL"""
    try:
        response = requests.head(url, timeout=5, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
        })
        return response.status_code == 200
    except Exception as e:
        logger.warning(f"URL недоступен: {url} | Ошибка: {e}")
        return False

def parse_vk_attachment(attach_str: str) -> dict:
    """Парсит строковое представление вложения ВК"""
    try:
        logger.debug(f"Парсинг вложения: {attach_str}")
        parts = attach_str.split('_')
        if len(parts) < 2:
            return None

        attach_type = parts[0].replace('attach1_', '')
        if '-' in attach_type:
            attach_type = attach_type.split('-')[0]

        owner_id = parts[1].split('-')[-1].replace(' ', '')
        media_id = parts[2] if len(parts) > 2 else None

        return {
            'type': attach_type,
            'owner_id': owner_id,
            'id': media_id,
            'url': f"https://vk.com/{attach_type}{owner_id}_{media_id}"
        }
    except Exception as e:
        logger.error(f"Ошибка парсинга вложения: {e}")
        return None

def get_user_info(user_id):
    """Получает информацию о пользователе VK"""
    try:
        response = vk.users.get(user_ids=user_id, fields="first_name,last_name,photo_50")
        return response[0] if response else {}
    except Exception as e:
        logger.error(f"Ошибка получения информации о пользователе: {e}")
        return {}

def update_vk_status():
    """Обновляет статус бота в VK"""
    try:
        uptime = datetime.now(pytz.timezone(TIMEZONE)) - bot_stats.start_time
        uptime_str = str(uptime).split('.')[0]  # Убираем миллисекунды
        last_time_str = bot_stats.last_message_time.strftime("%H:%M:%S") if bot_stats.last_message_time else "N/A"
        
        status_text = BOT_STATUS_TEMPLATE.format(
            uptime=uptime_str,
            message_count=bot_stats.message_count,
            last_time=last_time_str
        )

        vk.status.set(text=status_text)
        logger.info(f"Обновлен статус ВКонтакте: {status_text}")
    except Exception as e:
        logger.error(f"Ошибка обновления статуса в ВК: {e}")

def download_file(url):
    """Скачивает файл и возвращает временный путь"""
    try:
        if not is_url_accessible(url):
            return None
            
        response = requests.get(url, stream=True, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
        })
        
        if response.status_code == 200:
            filename = os.path.basename(url.split('?')[0])
            filepath = os.path.join("/tmp", filename)
            
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(1024*1024):
                    f.write(chunk)
            return filepath
    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}")
    return None

class DialogManager:
    def __init__(self):
        self.dialogs = OrderedDict()
        self.selected_dialogs = {}
        self.lock = threading.Lock()

    def update_dialog(self, user_id, message, attachments=None):
        with self.lock:
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
        with self.lock:
            return list(self.dialogs.items())
    
    def select_dialog(self, telegram_user_id, vk_user_id):
        with self.lock:
            self.selected_dialogs[telegram_user_id] = vk_user_id

    def get_selected(self, telegram_user_id):
        with self.lock:
            return self.selected_dialogs.get(telegram_user_id)

dialog_manager = DialogManager()

def vk_listener(loop):
    """Слушает новые сообщения из VK"""
    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                    logger.debug(f"Новое сообщение: {event.raw}")
                    user_id = event.user_id
                    dialog_manager.update_dialog(user_id, event.text, event.attachments)
                    asyncio.run_coroutine_threadsafe(
                        forward_to_telegram(user_id, event.text, event.attachments),
                        loop
                    )
                    update_vk_status()  # Обновляем статус после каждого сообщения
        except Exception as e:
            logger.error(f"Ошибка в VK listener: {e}")
            time.sleep(5)

async def send_media(chat_id, media_type, url, caption):
    """Отправляет медиафайл в Telegram"""
    try:
        logger.info(f"Попытка отправить {media_type}: {url}")
        
        filepath = download_file(url)
        if filepath:
            with open(filepath, 'rb') as f:
                if media_type == 'photo':
                    await application.bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=caption
                    )
                elif media_type == 'document':
                    await application.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        caption=caption
                    )
                elif media_type == 'audio':
                    await application.bot.send_audio(
                        chat_id=chat_id,
                        audio=f,
                        caption=caption
                    )
                os.remove(filepath)
        else:
            logger.error(f"Не удалось скачать медиа: {url}")
    except Exception as e:
        logger.error(f"Критическая ошибка отправки медиа: {e}")

async def forward_to_telegram(user_id, text, attachments):
    """Отправляет сообщение и вложения в Telegram"""
    try:
        logger.info(f"Обработка сообщения от {user_id}")
        logger.debug(f"Сырые вложения: {attachments}")

        bot_stats.last_message_time = datetime.now(pytz.timezone(TIMEZONE))
        bot_stats.message_count += 1

        user_info = get_user_info(user_id)
        dialog_info = f"📨 От {user_info.get('first_name', 'Неизвестный')} {user_info.get('last_name', '')}"

        # Отправка текста сообщения
        await application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"{dialog_info}:\n{text}"
        )

        # Обработка вложений
        for attach in attachments:
            try:
                if isinstance(attach, str):
                    parsed = parse_vk_attachment(attach)
                    if not parsed:
                        continue
                    attach = parsed

                if not isinstance(attach, dict):
                    logger.warning(f"Неподдерживаемый формат вложения: {type(attach)}")
                    continue

                attach_type = attach.get('type')
                logger.debug(f"Обработка вложения типа: {attach_type}")

                if attach_type == 'photo':
                    photo_url = attach.get('url')
                    if photo_url and is_url_accessible(photo_url):
                        await send_media(
                            chat_id=TELEGRAM_CHAT_ID,
                            media_type='photo',
                            url=photo_url,
                            caption=dialog_info
                        )

                elif attach_type == 'doc':
                    document_url = attach.get('url')
                    if document_url and is_url_accessible(document_url):
                        await send_media(
                            chat_id=TELEGRAM_CHAT_ID,
                            media_type='document',
                            url=document_url,
                            caption=dialog_info
                        )

                elif attach_type == 'audio':
                    audio_url = attach.get('url')
                    if audio_url and is_url_accessible(audio_url):
                        await send_media(
                            chat_id=TELEGRAM_CHAT_ID,
                            media_type='audio',
                            url=audio_url,
                            caption=dialog_info
                        )

            except Exception as e:
                logger.error(f"Ошибка обработки вложения: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"Ошибка пересылки: {e}", exc_info=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /start"""
    if str(update.effective_user.id) != AUTHORIZED_TELEGRAM_USER_ID:
        await update.message.reply_text(ACCESS_DENIED_MESSAGE)
        return
    await show_dialogs(update, context)

async def show_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список диалогов"""
    dialogs = dialog_manager.get_dialogs()
    if not dialogs:
        await update.message.reply_text("🤷 Нет активных диалогов")
        return

    message_text = "📋 Последние диалоги:\n\n"
    for i, (user_id, dialog) in enumerate(dialogs, 1):
        user = dialog['info']
        message_text += (
            f"{i}. {user.get('first_name', '?')} {user.get('last_name', '?')}\n"
            f"   └ {dialog['last_msg']}\n\n"
        )

    keyboard = [
        [InlineKeyboardButton(
            f"{dialog['info'].get('first_name', '?')} {dialog['info'].get('last_name', '?')}", 
            callback_data=f"select_{user_id}"
        )]
        for user_id, dialog in dialogs
    ]

    await update.message.reply_text(
        message_text.strip(), 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок"""
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("select_"):
        return

    user_id = str(query.from_user.id)
    if user_id != AUTHORIZED_TELEGRAM_USER_ID:
        await query.edit_message_text(ACCESS_DENIED_MESSAGE)
        return

    selected_vk_id = int(query.data.split("_")[1])
    dialog_manager.select_dialog(user_id, selected_vk_id)
    
    user_info = get_user_info(selected_vk_id)
    await query.edit_message_text(
        f"✅ Выбран диалог с {user_info.get('first_name', 'Неизвестный')} "
        f"{user_info.get('last_name', 'Пользователь')}"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает входящие текстовые сообщения"""
    if str(update.effective_user.id) != AUTHORIZED_TELEGRAM_USER_ID:
        await update.message.reply_text(ACCESS_DENIED_MESSAGE)
        return

    selected_vk_id = dialog_manager.get_selected(update.effective_user.id)
    if not selected_vk_id:
        await update.message.reply_text(DIALOG_NOT_SELECTED_MESSAGE)
        return

    try:
        message_text = update.message.text.strip() + MESSAGE_SIGNATURE
        vk.messages.send(
            user_id=selected_vk_id,
            message=message_text,
            random_id=0
        )
        await update.message.reply_text("✅ Сообщение отправлено")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")
        await update.message.reply_text("❌ Ошибка отправки сообщения")

async def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("dialogs", show_dialogs))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT, handle_message))

    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()

    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(main())