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

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs.txt"),  # Логи в файл
        logging.StreamHandler()           # Логи в консоль
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

# Конфигурация
VK_USER_TOKEN = os.getenv("VK_USER_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AUTHORIZED_TELEGRAM_USER_ID = os.getenv("AUTHORIZED_TELEGRAM_USER_ID")
MAX_DIALOGS = 10

# Проверка конфигурации
if not all([VK_USER_TOKEN, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, AUTHORIZED_TELEGRAM_USER_ID]):
    raise ValueError("Не все переменные окружения заданы в .env!")

# Инициализация VK
vk_session = vk_api.VkApi(token=VK_USER_TOKEN)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

# Утилиты
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
            filename = os.path.basename(url)
            filepath = os.path.join("/tmp", filename)
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(1024):
                    f.write(chunk)
            return filepath
    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}")
    return None

def save_last_dialog(telegram_user_id, vk_user_id):
    """Сохраняет ID последнего выбранного диалога в файл"""
    try:
        with open("dialog.txt", "w") as file:
            file.write(f"{telegram_user_id}:{vk_user_id}")
        logger.info(f"Сохранен последний диалог: {telegram_user_id} -> {vk_user_id}")
    except Exception as e:
        logger.error(f"Ошибка сохранения диалога: {e}")

def load_last_dialog():
    """Загружает ID последнего выбранного диалога из файла"""
    try:
        with open("dialog.txt", "r") as file:
            data = file.read().strip()
            if data:
                telegram_user_id, vk_user_id = data.split(":")
                return int(telegram_user_id), int(vk_user_id)
    except FileNotFoundError:
        logger.info("Файл dialog.txt не найден, создан новый.")
    except Exception as e:
        logger.error(f"Ошибка загрузки диалога: {e}")
    return None, None

# Менеджер диалогов
class DialogManager:
    def __init__(self):
        self.dialogs = OrderedDict()
        self.selected_dialogs = {}
        self.lock = threading.Lock()
        
        # Загрузка последнего диалога при инициализации
        telegram_user_id, vk_user_id = load_last_dialog()
        if telegram_user_id and vk_user_id:
            self.selected_dialogs[telegram_user_id] = vk_user_id

    def update_dialog(self, user_id, message, attachments=None):
        with self.lock:
            if user_id in self.dialogs:
                self.dialogs.move_to_end(user_id)
            else:
                if len(self.dialogs) >= MAX_DIALOGS:
                    self.dialogs.popitem(last=False)
                self.dialogs[user_id] = {
                    'info': get_user_info(user_id),
                    'last_msg': message,
                    'attachments': attachments or [],
                    'time': time.time()
                }

    def get_dialogs(self):
        with self.lock:
            return list(self.dialogs.items())
    
    def select_dialog(self, telegram_user_id, vk_user_id):
        with self.lock:
            self.selected_dialogs[telegram_user_id] = vk_user_id
            save_last_dialog(telegram_user_id, vk_user_id)

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

async def forward_to_telegram(user_id, text, attachments):
    """Отправляет сообщение в Telegram"""
    try:
        user_info = get_user_info(user_id)
        dialog_info = f"📨 От {user_info.get('first_name', 'Неизвестный')} {user_info.get('last_name', '')}"

        # Отправка текста
        await application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"{dialog_info}:\n{text}"
        )

        # Отправка вложений
        for attach in attachments:
            if attach['type'] == 'photo':
                photo_url = max(attach['photo']['sizes'], key=lambda x: x['width'])['url']
                await application.bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=photo_url,
                    caption=dialog_info
                )
            elif attach['type'] == 'doc':
                doc_url = attach['doc']['url']
                await application.bot.send_document(
                    chat_id=TELEGRAM_CHAT_ID,
                    document=doc_url,
                    caption=dialog_info
                )

    except Exception as e:
        logger.error(f"Ошибка пересылки в Telegram: {e}")

# Обработчики Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    if str(update.effective_user.id) != AUTHORIZED_TELEGRAM_USER_ID:
        await update.message.reply_text("⛔ Доступ запрещен")
        return
    await show_dialogs(update, context)

async def show_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список диалогов с инлайн-клавиатурой"""
    dialogs = dialog_manager.get_dialogs()
    if not dialogs:
        await update.message.reply_text("🤷 Нет активных диалогов")
        return

    keyboard = []
    for user_id, dialog in dialogs:
        user = dialog['info']
        btn_text = f"{user.get('first_name', '?')} {user.get('last_name', '?')}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"select_{user_id}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📋 Выберите диалог:", reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки"""
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
    """Обрабатывает все сообщения"""
    user_id = str(update.effective_user.id)
    if user_id != AUTHORIZED_TELEGRAM_USER_ID:
        return

    selected_vk_id = dialog_manager.get_selected(user_id)
    if not selected_vk_id:
        await update.message.reply_text("⚠ Сначала выберите диалог /dialogs")
        return

    try:
        # Добавляем подпись к тексту
        signature = "\n\n(отправлено с помощью tg bota)"
        
        # Отправка текста
        if update.message.text:
            message_text = update.message.text + signature
            vk.messages.send(
                user_id=selected_vk_id,
                message=message_text,
                random_id=0
            )
            await update.message.reply_text("✅ Сообщение отправлено")

        # Обработка медиа
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
                    message=signature.strip(),  # Подпись для фото
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
                    message=signature.strip(),  # Подпись для голосового
                    random_id=0
                )
                await update.message.reply_text("✅ Голосовое сообщение отправлено")

    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

def main():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("dialogs", show_dialogs))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL, handle_message))

    # Запуск VK listener
    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()

    # Запуск бота
    logger.info("🤖 Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()