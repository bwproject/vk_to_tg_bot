import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
import asyncio
import threading
import os
import logging
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

# Храним выбранных собеседников
selected_friends = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню"""
    keyboard = [
        [InlineKeyboardButton("📩 Последние сообщения", callback_data="latest_messages")],
        [InlineKeyboardButton("👥 Список друзей", callback_data="friends_page_0")]
    ]
    await update.message.reply_text("📌 Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_latest_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит последние 5 сообщений"""
    query = update.callback_query
    await query.answer()

    try:
        messages = vk.messages.getConversations(count=5)["items"]

        if not messages:
            await query.edit_message_text("❌ Нет новых сообщений.")
            return

        text = "📩 Последние сообщения:\n"
        keyboard = []

        for msg in messages:
            last_message = msg["last_message"]
            user_id = last_message["from_id"]

            # Определяем, это пользователь или сообщество
            sender_name = "Неизвестно"
            if user_id > 0:
                try:
                    user_info = vk.users.get(user_ids=user_id, fields="first_name,last_name")[0]
                    sender_name = f"{user_info['first_name']} {user_info['last_name']} ({user_id})"
                except Exception:
                    pass
            else:
                try:
                    group_info = vk.groups.getById(group_id=abs(user_id))[0]
                    sender_name = f"{group_info['name']} ({user_id})"
                except Exception:
                    pass

            # Текст сообщения
            text_message = last_message.get("text", "Без текста")
            if 'attachments' in last_message:
                text_message += "\n\n📎 Вложения: " + ", ".join([attachment['type'] for attachment in last_message['attachments']])

            # Статус ответа
            reply_status = "✅ Ответили" if last_message.get("reply_message") else "❌ Не отвечено"
            reply_text = f"\nОтвет: {last_message['reply_message']['text']}" if last_message.get("reply_message") else "Вы не ответили"
            if 'attachments' in last_message.get("reply_message", {}):
                reply_text += "\n📎 Вложения: " + ", ".join([attachment['type'] for attachment in last_message['reply_message']['attachments']])

            text += f"\n👤 От: {sender_name}\nТекст: {text_message}\n{reply_status}\n{reply_text}\n==========="
            keyboard.append([InlineKeyboardButton(f"{sender_name}", callback_data=f"open_dialog_{user_id}")])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.error(f"Ошибка при получении сообщений: {e}")
        await query.edit_message_text("❌ Не удалось получить сообщения.")

async def open_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открывает диалог с выбранным собеседником"""
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    vk_user_id = int(query.data.split("_")[-1])
    selected_friends[user_id] = vk_user_id

    await query.edit_message_text(f"✅ Теперь вы общаетесь с ID {vk_user_id}. Можете отправлять сообщения.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет сообщение выбранному собеседнику в VK"""
    user_id = str(update.effective_user.id)
    vk_user_id = selected_friends.get(user_id)

    if not vk_user_id:
        await update.message.reply_text("⚠ Сначала выберите собеседника через /start.")
        return

    message_text = f"{update.message.text}\n\n{MESSAGE_SIGNATURE}"

    # Отправка сообщения в VK
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

                    asyncio.run_coroutine_threadsafe(
                        application.bot.send_message(
                            os.getenv("TELEGRAM_CHAT_ID"),
                            text=f"У Вас новое сообщение из ВК\nОт: {user_id}\n{text}"
                        ),
                        loop
                    )

        except Exception as e:
            logger.error(f"Ошибка VK listener: {e}")

def main():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(show_latest_messages, pattern="^latest_messages$"))
    application.add_handler(CallbackQueryHandler(open_dialog, pattern="^open_dialog_"))
    application.add_handler(MessageHandler(filters.TEXT, handle_message))

    loop = asyncio.get_event_loop()
    threading.Thread(target=vk_listener, args=(loop,), daemon=True).start()

    logger.info("🤖 Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()