import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import asyncio
import threading
from dotenv import load_dotenv
import os
import requests
import logging
import json

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

if not all([VK_USER_TOKEN, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, AUTHORIZED_TELEGRAM_USER_ID]):
    raise ValueError("Не все обязательные переменные окружения заданы в .env!")

# Инициализация VK
vk_session = vk_api.VkApi(token=VK_USER_TOKEN)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

# Функция для получения информации о пользователе
def get_user_info(user_id):
    try:
        response = vk.users.get(user_ids=user_id, fields="first_name,last_name")
        return response[0] if response else {}
    except Exception as e:
        logger.error(f"Ошибка получения информации о пользователе: {e}")
        return {}

# Функция загрузки файлов
def download_file(url):
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

# Слушатель VK
def vk_listener(loop):
    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                    user_id = event.user_id
                    logger.info(f"📩 Новое сообщение от {user_id}: {event.text}")
                    logger.info(f"🖼 Вложения: {event.attachments}")

                    asyncio.run_coroutine_threadsafe(
                        forward_to_telegram(user_id, event.text, event.attachments),
                        loop
                    )
        except Exception as e:
            logger.error(f"Ошибка в VK listener: {e}")
            asyncio.sleep(5)

# Функция пересылки сообщений в Telegram
async def forward_to_telegram(user_id, text, attachments):
    try:
        user_info = get_user_info(user_id)
        dialog_info = f"📨 От {user_info.get('first_name', 'Неизвестный')} {user_info.get('last_name', '')}"

        await application.bot.send_message(TELEGRAM_CHAT_ID, text=f"{dialog_info}:\n{text}")

        if not attachments:
            return

        if isinstance(attachments, str):
            # Вложения пришли в виде ссылки, отправляем как текст
            await application.bot.send_message(TELEGRAM_CHAT_ID, text=f"🔗 Вложение: https://vk.com/{attachments}")
            return

        for attach in attachments:
            try:
                if isinstance(attach, str):
                    await application.bot.send_message(TELEGRAM_CHAT_ID, text=f"🔗 Вложение: https://vk.com/{attach}")
                    continue

                if attach.get('type') == 'photo':
                    photo = max(attach['photo']['sizes'], key=lambda x: x['width'])
                    await application.bot.send_photo(TELEGRAM_CHAT_ID, photo=photo['url'])

                elif attach.get('type') == 'doc':
                    await application.bot.send_document(TELEGRAM_CHAT_ID, document=attach['doc']['url'])

                elif attach.get('type') == 'audio_message':
                    audio_url = attach['audio_message']['link_ogg']
                    filepath = download_file(audio_url)
                    if filepath:
                        with open(filepath, 'rb') as audio_file:
                            await application.bot.send_voice(TELEGRAM_CHAT_ID, voice=audio_file)

                else:
                    logger.warning(f"Неизвестный тип вложения: {attach.get('type')}")

            except Exception as e:
                logger.error(f"Ошибка обработки вложения: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"Ошибка пересылки в Telegram: {e}", exc_info=True)

# Обработчик Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != AUTHORIZED_TELEGRAM_USER_ID:
        await update.message.reply_text("⛔ Доступ запрещен")
        return
    await update.message.reply_text("🤖 Бот запущен!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != AUTHORIZED_TELEGRAM_USER_ID:
        return

    text = update.message.text
    await update.message.reply_text(f"📨 Отправлено: {text}")

    # Отправка сообщения в VK
    vk.messages.send(
        user_id=AUTHORIZED_TELEGRAM_USER_ID,
        message=text,
        random_id=0
    )

def main():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ALL, handle_message))

    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()

    logger.info("🤖 Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()