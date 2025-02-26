import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
import asyncio
import threading
import os
import requests
import logging
import time
from datetime import datetime
import pytz
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

VK_USER_TOKEN = os.getenv("VK_USER_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MESSAGE_SIGNATURE = os.getenv("MESSAGE_SIGNATURE", "Отправлено из Telegram")

if not all([VK_USER_TOKEN, TELEGRAM_TOKEN]):
    raise ValueError("❌ Не все переменные окружения заданы!")

# Инициализация VK API
vk_session = vk_api.VkApi(token=VK_USER_TOKEN)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

# Храним выбранных друзей
selected_friends = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит главное меню"""
    keyboard = [
        [InlineKeyboardButton("📩 Последние сообщения", callback_data="latest_messages")],
        [InlineKeyboardButton("👥 Открыть список друзей", callback_data="friends_page_0")]
    ]
    await update.message.reply_text("📌 Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_latest_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает последние 5 сообщений с кнопками"""
    query = update.callback_query
    await query.answer()

    messages = vk.messages.getConversations(count=5)
    msg_list = messages.get("items", [])

    if not msg_list:
        await query.edit_message_text("❌ Нет новых сообщений.")
        return

    text = "📩 Последние сообщения:\n"
    keyboard = []

    for msg in msg_list:
        last_message = msg["last_message"]
        user_id = last_message["from_id"]
        user_info = vk.users.get(user_ids=user_id, fields="first_name,last_name")[0]
        sender_name = f"{user_info['first_name']} {user_info['last_name']}"
        
        recipient_id = msg['conversation']['peer']['id']
        recipient_info = vk.users.get(user_ids=recipient_id, fields="first_name,last_name")[0]
        recipient_name = f"{recipient_info['first_name']} {recipient_info['last_name']}"
        
        replied = last_message.get("reply_message", None)
        reply_text = f"Ответ: {replied['text']}" if replied else "Нет ответа"
        
        text += f"\n👤 От: {sender_name}\nТекст: {last_message['text'][:50]}...\nОтвечено: {reply_text}"
        keyboard.append([InlineKeyboardButton(f"Перейти в диалог с {recipient_name}", callback_data=f"open_dialog_{recipient_id}")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_friends(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит список друзей с пагинацией"""
    query = update.callback_query
    await query.answer()

    page = int(query.data.split("_")[-1])
    friends = vk.friends.get(order="hints", fields="first_name,last_name")
    friends_list = friends.get("items", [])

    if not friends_list:
        await query.edit_message_text("❌ У вас нет друзей в VK.")
        return

    per_page = 5
    start = page * per_page
    end = start + per_page
    friends_page = friends_list[start:end]

    keyboard = [[InlineKeyboardButton(f"{f['first_name']} {f['last_name']}", callback_data=f"open_dialog_{f['id']}")] for f in friends_page]

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅ Назад", callback_data=f"friends_page_{page-1}"))
    if end < len(friends_list):
        nav_buttons.append(InlineKeyboardButton("➡ Вперед", callback_data=f"friends_page_{page+1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    await query.edit_message_text("👥 Выберите друга:", reply_markup=InlineKeyboardMarkup(keyboard))

async def open_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открывает диалог с выбранным пользователем"""
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    vk_user_id = int(query.data.split("_")[-1])
    selected_friends[user_id] = vk_user_id

    await query.edit_message_text(f"✅ Вы выбрали собеседника ID {vk_user_id}. Теперь можно писать ему сообщения.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет сообщение выбранному собеседнику в VK с подписью"""
    user_id = str(update.effective_user.id)
    vk_user_id = selected_friends.get(user_id)

    if not vk_user_id:
        await update.message.reply_text("⚠ Сначала выберите собеседника через /start.")
        return

    message_text = f"{update.message.text}\n\n{MESSAGE_SIGNATURE}"
    
    if update.message.photo:
        # Отправка фото
        photo_file = update.message.photo[-1].file_id
        file = await application.bot.get_file(photo_file)
        file_path = file.file_path
        vk.messages.send(user_id=vk_user_id, attachment=file_path, random_id=0)
    elif update.message.document:
        # Отправка документа
        document_file = update.message.document.file_id
        file = await application.bot.get_file(document_file)
        file_path = file.file_path
        vk.messages.send(user_id=vk_user_id, attachment=file_path, random_id=0)
    else:
        vk.messages.send(user_id=vk_user_id, message=message_text, random_id=0)

    await update.message.reply_text("✅ Сообщение отправлено.")

def vk_listener(loop):
    """Слушает новые сообщения из VK"""
    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                    user_id = event.user_id
                    message_data = vk.messages.getHistory(user_id=user_id, count=1)['items'][0]

                    text = message_data.get('text', '')
                    user_info = vk.users.get(user_ids=user_id, fields="first_name,last_name")[0]
                    sender_name = f"{user_info['first_name']} {user_info['last_name']}"

                    # Добавление текста "У вас новое сообщение из VK"
                    full_text = f"📩 У вас новое сообщение из ВК\nОт: {sender_name}\n{text}"

                    asyncio.run_coroutine_threadsafe(
                        application.bot.send_message(os.getenv("TELEGRAM_CHAT_ID"), text=full_text),
                        loop
                    )

        except Exception as e:
            logger.error(f"Ошибка VK listener: {e}")
            time.sleep(5)

def main():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(show_latest_messages, pattern="^latest_messages$"))
    application.add_handler(CallbackQueryHandler(show_friends, pattern="^friends_page_"))
    application.add_handler(CallbackQueryHandler(open_dialog, pattern="^open_dialog_"))
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.DOCUMENT, handle_message))

    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()

    logger.info("🤖 Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()