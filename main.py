import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from telegram import Update
from telegram.ext import Application, ContextTypes
import asyncio
import threading
import os
import requests
import logging
from datetime import datetime
import pytz
import time

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
from dotenv import load_dotenv
load_dotenv()

VK_USER_TOKEN = os.getenv("VK_USER_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AUTHORIZED_TELEGRAM_USER_ID = os.getenv("AUTHORIZED_TELEGRAM_USER_ID")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# Проверка переменных окружения
if not all([VK_USER_TOKEN, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, AUTHORIZED_TELEGRAM_USER_ID]):
    raise ValueError("Не все переменные окружения заданы!")

# Инициализация VK
vk_session = vk_api.VkApi(token=VK_USER_TOKEN)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

class BotStats:
    def __init__(self):
        self.start_time = datetime.now(pytz.timezone(TIMEZONE))
        self.message_count = 0
        self.post_count = 0

bot_stats = BotStats()

def download_file(url):
    """Скачивает файл и возвращает путь"""
    try:
        response = requests.get(url, stream=True, timeout=10)
        if response.status_code == 200:
            filename = os.path.basename(url.split('?')[0])
            filepath = os.path.join("/tmp", filename)
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(1024 * 1024):
                    f.write(chunk)
            return filepath
    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}")
    return None

async def send_media(media_type, url, caption):
    """Отправляет медиафайл в Telegram"""
    try:
        logger.info(f"Отправка {media_type}: {url}")
        filepath = download_file(url)
        if not filepath:
            logger.error("Не удалось скачать файл")
            return

        with open(filepath, 'rb') as file:
            if media_type == 'photo':
                await application.bot.send_photo(TELEGRAM_CHAT_ID, photo=file, caption=caption)
            elif media_type == 'doc':
                await application.bot.send_document(TELEGRAM_CHAT_ID, document=file, caption=caption)
            elif media_type == 'audio':
                await application.bot.send_audio(TELEGRAM_CHAT_ID, audio=file, caption=caption)
        
        os.remove(filepath)
        logger.info(f"Файл {media_type} отправлен")

    except Exception as e:
        logger.error(f"Ошибка отправки медиа: {e}")

async def forward_to_telegram(user_id, text, attachments):
    """Пересылает сообщение и вложения из ВК в Telegram"""
    try:
        bot_stats.message_count += 1

        user_info = vk.users.get(user_ids=user_id, fields="first_name,last_name")[0]
        sender_name = f"{user_info.get('first_name', 'Неизвестный')} {user_info.get('last_name', '')}"

        message_text = f"📨 {sender_name}:\n{text}"
        await application.bot.send_message(TELEGRAM_CHAT_ID, text=message_text)

        for attach in attachments:
            attach_type = attach['type']
            media = attach[attach_type]

            if attach_type == 'photo':
                sizes = media.get('sizes', [])
                media_url = max(sizes, key=lambda x: x.get('width', 0)).get('url', '')

            elif attach_type in ['doc', 'audio', 'video']:
                media_url = media.get('url', '')

            elif attach_type == 'audio_message':
                media_url = media.get('link_mp3', '')

            else:
                logger.warning(f"Неизвестный тип вложения: {attach_type}")
                continue

            if media_url:
                await send_media(attach_type, media_url, f"{sender_name} отправил {attach_type}")

    except Exception as e:
        logger.error(f"Ошибка пересылки: {e}", exc_info=True)

async def forward_post_to_telegram(post):
    """Пересылает новый пост из VK в Telegram"""
    try:
        bot_stats.post_count += 1

        owner_id = post['owner_id']
        post_id = post['id']
        post_link = f"https://vk.com/wall{owner_id}_{post_id}"
        text = post.get('text', '')

        message_text = f"📢 Новый пост:\n{text}\n🔗 [Ссылка на пост]({post_link})"
        await application.bot.send_message(TELEGRAM_CHAT_ID, text=message_text, parse_mode="Markdown")

        attachments = post.get('attachments', [])
        for attach in attachments:
            attach_type = attach['type']
            media = attach[attach_type]

            if attach_type == 'photo':
                sizes = media.get('sizes', [])
                media_url = max(sizes, key=lambda x: x.get('width', 0)).get('url', '')

            elif attach_type in ['doc', 'audio', 'video']:
                media_url = media.get('url', '')

            elif attach_type == 'link':
                media_url = media.get('url', '')
                await application.bot.send_message(TELEGRAM_CHAT_ID, text=f"🔗 [Ссылка]({media_url})", parse_mode="Markdown")
                continue

            else:
                logger.warning(f"Неизвестный тип вложения в посте: {attach_type}")
                continue

            if media_url:
                await send_media(attach_type, media_url, "Вложение из поста")

    except Exception as e:
        logger.error(f"Ошибка пересылки поста: {e}", exc_info=True)

def vk_listener(loop):
    """Слушает события из VK"""
    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                    user_id = event.user_id
                    message_data = vk.messages.getHistory(user_id=user_id, count=1)['items'][0]

                    text = message_data.get('text', '')
                    attachments = message_data.get('attachments', [])

                    asyncio.run_coroutine_threadsafe(
                        forward_to_telegram(user_id, text, attachments),
                        loop
                    )

                elif event.type == VkEventType.WALL_POST_NEW:
                    post_data = event.raw
                    asyncio.run_coroutine_threadsafe(
                        forward_post_to_telegram(post_data),
                        loop
                    )

        except Exception as e:
            logger.error(f"Ошибка в VK listener: {e}")
            time.sleep(5)

def main():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()
    
    logger.info("🤖 Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()