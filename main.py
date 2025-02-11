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
import json
from datetime import datetime, timedelta
import pytz

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    headers=[
        logging.FileHandler("logs.txt"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Отключаем логи httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()

# Конфигурация
VK_USER_TOKEN = os.getenv("VK_USER_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AUTHORIZED_TELEGRAM_USER_ID = os.getenv("AUTHORIZED_TELEGRAM_USER_ID")
MESSAGE_SIGNATURE = os.getenv("MESSAGE_SIGNATURE", "\n\n(отправлено с помощью tg bota)")
MAX_DIALOGS = 10

# Проверка переменных окружения
if not all([VK_USER_TOKEN, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, AUTHORIZED_TELEGRAM_USER_ID]):
    raise ValueError("Не все обязательные переменные окружения заданы в .env!")

# Инициализация VK
vk_session = vk_api.VkApi(
    token=VK_USER_TOKEN,
    scope=65536  # status permission
)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

# Утилиты
class BotStats:
    def __init__(self):
        self.start_time = datetime.now()
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

def parse_vk_attachment(attach_str: str) -> dict:
    """Парсит строковое представление вложения ВК"""
    try:
        # Пример: "photo-123_456" или "attach1_wall-123_456"
        parts = attach_str.split('_')
        if len(parts) < 2:
            return None

        # Определяем тип вложения
        attach_type = parts[0].replace('attach1_', '')  # Обрабатываем attach1_photo случаи
        if '-' in attach_type:
            attach_type = attach_type.split('-')[0]

        # Извлекаем owner_id и media_id
        owner_part = parts[1].split('-')[-1]
        owner_id = owner_part.replace(' ', '')
        media_id = parts[2] if len(parts) > 2 else None

        # Формируем URL
        if attach_type == 'photo':
            url = f"https://vk.com/photo{owner_id}_{media_id}"
        else:
            url = f"https://vk.com/{attach_type}{owner_id}_{media_id}"

        return {
            'type': attach_type,
            'owner_id': owner_id,
            'id': media_id,
            'url': url
        }
    except Exception as e:
        logger.error(f"Ошибка парсинга вложения {attach_str}: {e}")
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
        response = requests.get(url, stream=True, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
        })
        
        if response.status_code == 200:
            filename = os.path.basename(url.split('?')[0])  # Убираем параметры запроса
            filepath = os.path.join("/tmp", filename)
            
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(1024*1024):  # 1MB chunks
                    f.write(chunk)
            return filepath
    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}")
    return None

# Менеджер диалогов
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

# Обработчики VK
def vk_listener(loop):
    """Слушает новые сообщения из VK"""
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

async def send_media_with_fallback(chat_id, media_type, url, caption):
    """Отправляет медиафайл с обработкой ошибок"""
    try:
        logger.info(f"Попытка отправить {media_type}: {url}")
        
        if media_type == 'photo':
            try:
                await application.bot.send_photo(
                    chat_id=chat_id,
                    photo=url,
                    caption=caption
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить по URL, пробую скачать: {e}")
                filepath = download_file(url)
                if filepath:
                    with open(filepath, 'rb') as media_file:
                        await application.bot.send_photo(
                            chat_id=chat_id,
                            photo=media_file,
                            caption=caption
                        )
                    os.remove(filepath)
    except Exception as e:
        logger.error(f"Критическая ошибка отправки медиа: {e}")

async def forward_to_telegram(user_id, text, attachments):
    """Отправляет сообщение в Telegram"""
    try:
        bot_stats.last_message_time = datetime.now()
        bot_stats.message_count += 1

        user_info = get_user_info(user_id)
        dialog_info = f"📨 От {user_info.get('first_name', 'Неизвестный')} {user_info.get('last_name', '')}"

        await application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"{dialog_info}:\n{text}"
        )

        for attach in attachments:
            try:
                logger.debug(f"Сырые данные вложения: {attach}")

                # Обработка разных форматов вложений
                if isinstance(attach, str):
                    parsed = parse_vk_attachment(attach)
                    if parsed:
                        attach = parsed
                        logger.info(f"Преобразованное вложение: {parsed}")
                    else:
                        continue

                if isinstance(attach, dict):
                    # Для словарных вложений из VK API
                    attach_type = attach.get('type')
                    if attach_type == 'photo':
                        # Получаем фото максимального размера
                        sizes = attach['photo']['sizes']
                        photo = max(sizes, key=lambda x: x['width'])
                        photo_url = photo['url']
                    elif 'url' in attach:
                        photo_url = attach['url']
                    else:
                        continue

                    await send_media_with_fallback(
                        chat_id=TELEGRAM_CHAT_ID,
                        media_type='photo',
                        url=photo_url,
                        caption=dialog_info
                    )

            except Exception as e:
                logger.error(f"Ошибка обработки вложения: {str(e)}", exc_info=True)

    except Exception as e:
        logger.error(f"Ошибка пересылки в Telegram: {str(e)}", exc_info=True)

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

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(message_text.strip(), reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("select_"):
        return

    user_id = str(query.from_user.id)
    if user_id != AUTHORIZED_TELEGRAM_USER_ID:
        await query.edit_message_text("⛔ Доступ запрещен")
        return

    selected_vk_id = int(query.data.split("_")[1])
    dialog_manager.select_dialog(user_id, selected_vk_id)
    
    user_info = get_user_info(selected_vk_id)
    await query.edit_message_text(
        f"✅ Выбран диалог с {user_info.get('first_name', 'Неизвестный')} "
        f"{user_info.get('last_name', 'Пользователь')}"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != AUTHORIZED_TELEGRAM_USER_ID:
        return

    selected_vk_id = dialog_manager.get_selected(user_id)
    if not selected_vk_id:
        await update.message.reply_text("⚠ Сначала выберите диалог /dialogs")
        return

    try:
        signature = MESSAGE_SIGNATURE
        
        if update.message.text:
            message_text = update.message.text + signature
            vk.messages.send(
                user_id=selected_vk_id,
                message=message_text,
                random_id=0
            )
            await update.message.reply_text("✅ Сообщение отправлено")

        elif update.message.photo:
            photo = await update.message.photo[-1].get_file()
            filepath = download_file(photo.file_path)
            if filepath:
                upload = vk_api.VkUpload(vk_session)
                photo_data = upload.photo_messages(filepath)[0]
                attachment = f"photo{photo_data['owner_id']}_{photo_data['id']}"
                vk.messages.send(
                    user_id=selected_vk_id,
                    attachment=attachment,
                    message=signature.strip(),
                    random_id=0
                )
                await update.message.reply_text("✅ Фото отправлено")

        elif update.message.voice:
            voice = await update.message.voice.get_file()
            filepath = download_file(voice.file_path)
            if filepath:
                upload = vk_api.VkUpload(vk_session)
                doc = upload.document_message(
                    filepath,
                    peer_id=selected_vk_id,
                    doc_type="audio_message"
                )
                attachment = f"doc{doc['owner_id']}_{doc['id']}"
                vk.messages.send(
                    user_id=selected_vk_id,
                    attachment=attachment,
                    message=signature.strip(),
                    random_id=0
                )
                await update.message.reply_text("✅ Голосовое сообщение отправлено")

    except Exception as e:
        logger.error(f"Ошибка отправки: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

def set_vk_status(text):
    try:
        vk.status.set(text=text)
        logger.info(f"Статус обновлен: {text}")
        return True
    except vk_api.exceptions.ApiError as e:
        logger.error(f"Ошибка установки статуса: {e}")
        return False

async def update_status_task(context: ContextTypes.DEFAULT_TYPE):
    try:
        current_time = datetime.now(pytz.timezone('Asia/Yekaterinburg'))
        uptime = datetime.now() - bot_stats.start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        
        status_text = (
            f"⌛ Бот работает: {days}д {hours}ч {minutes}м | "
            f"📨 Сообщений: {bot_stats.message_count} | "
            f"🕒 Последнее: {current_time.strftime('%H:%M')} | "
            f"@tgvktg_bot"
        )
        
        if set_vk_status(status_text):
            logger.info("Статус ВК успешно обновлен")
        else:
            logger.warning("Не удалось обновить статус ВК")
            
    except Exception as e:
        logger.error(f"Ошибка в задаче обновления статуса: {e}")

def main():
    global application
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .arbitrary_callback_data(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("dialogs", show_dialogs))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL, handle_message))

    if application.job_queue:
        application.job_queue.run_repeating(
            callback=update_status_task,
            interval=900,
            first=10
        )

    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()
    
    logger.info("🤖 Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()