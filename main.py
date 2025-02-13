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
vk_session = vk_api.VkApi(token=VK_USER_TOKEN, scope='photos,messages,docs,audio')
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
        response = requests.head(url, timeout=5)
        return response.status_code == 200
    except Exception as e:
        logger.warning(f"URL недоступен: {url}, ошибка: {e}")
        return False

def parse_vk_attachment(attachment) -> dict:
    """Парсит вложения ВК разных форматов"""
    try:
        # Если вложение уже словарь
        if isinstance(attachment, dict):
            return attachment
            
        # Если вложение - строка
        if isinstance(attachment, str):
            parts = attachment.split('_')
            if len(parts) < 2:
                return None

            # Обработка формата attach1_type123_456
            if parts[0].startswith('attach1'):
                parts = parts[0].split('-')[-1:] + parts[1:]

            attach_type = parts[0]
            owner_id = parts[1]
            media_id = parts[2] if len(parts) > 2 else None

            return {
                'type': attach_type,
                'owner_id': owner_id,
                'id': media_id,
                'url': f"https://vk.com/{attach_type}{owner_id}_{media_id}"
            }
        return None
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

def download_file(url):
    """Скачивает файл и возвращает временный путь"""
    try:
        response = requests.get(url, stream=True, timeout=10)
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
                    user_id = event.user_id
                    logger.debug(f"Новое сообщение: {event.raw}")
                    dialog_manager.update_dialog(user_id, event.text, event.attachments)
                    asyncio.run_coroutine_threadsafe(
                        forward_to_telegram(user_id, event.text, event.attachments),
                        loop
                    )
        except Exception as e:
            logger.error(f"Ошибка в VK listener: {e}")
            time.sleep(5)

async def send_media(media_type, url, caption):
    """Отправляет медиафайл в Telegram"""
    try:
        logger.info(f"Попытка отправить {media_type}: {url}")
        
        filepath = download_file(url)
        if not filepath:
            logger.error("Не удалось скачать файл")
            return

        with open(filepath, 'rb') as file:
            if media_type == 'photo':
                await application.bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=file,
                    caption=caption
                )
            elif media_type == 'doc':
                await application.bot.send_document(
                    chat_id=TELEGRAM_CHAT_ID,
                    document=file,
                    caption=caption
                )
            elif media_type == 'audio':
                await application.bot.send_audio(
                    chat_id=TELEGRAM_CHAT_ID,
                    audio=file,
                    caption=caption
                )
        
        os.remove(filepath)
        logger.info(f"Успешно отправлено: {media_type}")

    except Exception as e:
        logger.error(f"Ошибка отправки медиа: {e}")

async def forward_to_telegram(user_id, text, attachments):
    """Отправляет сообщение и вложения из ВК в Telegram"""
    try:
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
                logger.debug(f"Сырые данные вложения: {attach}")
                
                parsed = parse_vk_attachment(attach)
                if not parsed:
                    logger.warning(f"Не удалось распарсить вложение: {attach}")
                    continue

                attach_type = parsed.get('type')
                media_url = None

                # Обработка фото
                if attach_type == 'photo':
                    if 'sizes' in parsed:
                        sizes = parsed['sizes']
                        photo = max(sizes, key=lambda x: x.get('width', 0))
                        media_url = photo.get('url')
                    else:
                        media_url = parsed.get('url')

                # Обработка документов
                elif attach_type == 'doc':
                    media_url = parsed.get('url')

                # Обработка аудио
                elif attach_type == 'audio':
                    media_url = parsed.get('url')

                if media_url and is_url_accessible(media_url):
                    await send_media(attach_type, media_url, dialog_info)
                else:
                    logger.warning(f"Недействительный URL для {attach_type}: {media_url}")

            except Exception as e:
                logger.error(f"Ошибка обработки вложения: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"Ошибка пересылки: {e}", exc_info=True)

# ... (остальные функции остаются без изменений: start, show_dialogs, handle_callback, handle_message, update_status_task, main)

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
    """Обрабатывает текстовые сообщения"""
    user_id = str(update.effective_user.id)
    if user_id != AUTHORIZED_TELEGRAM_USER_ID:
        return

    selected_vk_id = dialog_manager.get_selected(user_id)
    if not selected_vk_id:
        await update.message.reply_text(DIALOG_NOT_SELECTED_MESSAGE)
        return

    try:
        signature = MESSAGE_SIGNATURE
        
        if update.message.text:
            vk.messages.send(
                user_id=selected_vk_id,
                message=update.message.text + signature,
                random_id=0
            )
            await update.message.reply_text("✅ Сообщение отправлено")

        elif update.message.photo:
            photo = await update.message.photo[-1].get_file()
            filepath = download_file(photo.file_path)
            if filepath:
                upload = vk_api.VkUpload(vk_session)
                photo_data = upload.photo_messages(filepath)[0]
                vk.messages.send(
                    user_id=selected_vk_id,
                    attachment=f"photo{photo_data['owner_id']}_{photo_data['id']}",
                    message=signature.strip(),
                    random_id=0
                )
                await update.message.reply_text("✅ Фото отправлено")

        elif update.message.document:
            doc = await update.message.document.get_file()
            filepath = download_file(doc.file_path)
            if filepath:
                upload = vk_api.VkUpload(vk_session)
                doc_data = upload.document_message(filepath, selected_vk_id, "doc")
                vk.messages.send(
                    user_id=selected_vk_id,
                    attachment=f"doc{doc_data['owner_id']}_{doc_data['id']}",
                    message=signature.strip(),
                    random_id=0
                )
                await update.message.reply_text("✅ Документ отправлен")

        elif update.message.audio:
            audio = await update.message.audio.get_file()
            filepath = download_file(audio.file_path)
            if filepath:
                upload = vk_api.VkUpload(vk_session)
                audio_data = upload.audio(filepath, artist="Исполнитель", title="Трек из Telegram")
                vk.messages.send(
                    user_id=selected_vk_id,
                    attachment=f"audio{audio_data['owner_id']}_{audio_data['id']}",
                    message=signature.strip(),
                    random_id=0
                )
                await update.message.reply_text("✅ Аудио отправлено")

        elif update.message.voice:
            voice = await update.message.voice.get_file()
            filepath = download_file(voice.file_path)
            if filepath:
                upload = vk_api.VkUpload(vk_session)
                doc_data = upload.document_message(filepath, selected_vk_id, "audio_message")
                vk.messages.send(
                    user_id=selected_vk_id,
                    attachment=f"doc{doc_data['owner_id']}_{doc_data['id']}",
                    message=signature.strip(),
                    random_id=0
                )
                await update.message.reply_text("✅ Голосовое сообщение отправлено")

    except Exception as e:
        logger.error(f"Ошибка отправки: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def update_status_task(context: ContextTypes.DEFAULT_TYPE):
    """Обновляет статус ВК"""
    try:
        current_time = datetime.now(pytz.timezone(TIMEZONE))
        uptime = datetime.now(pytz.timezone(TIMEZONE)) - bot_stats.start_time
        days = uptime.days
        hours, rem = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(rem, 60)
        
        status_text = BOT_STATUS_TEMPLATE.format(
            uptime=f"{days}д {hours}ч {minutes}м",
            message_count=bot_stats.message_count,
            last_time=current_time.strftime('%H:%M')
        )
        
        vk.status.set(text=status_text)
        logger.info(f"Обновлен статус ВК: {status_text}")
            
    except Exception as e:
        logger.error(f"Ошибка обновления статуса: {e}")

def main():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("dialogs", show_dialogs))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL, handle_message))

    application.job_queue.run_repeating(update_status_task, interval=300, first=5)

    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()
    
    logger.info("🤖 Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()