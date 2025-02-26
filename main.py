import logging
import json
import vk_api
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Настройки VK
VK_TOKEN = "ВАШ_ТОКЕН_VK"
TELEGRAM_BOT_TOKEN = "ВАШ_ТОКЕН_TELEGRAM"
AUTHORIZED_TELEGRAM_USER_ID = "ВАШ_ID_В_TG"

# Настройка логирования
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Авторизация в VK
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()

# Запуск Telegram-бота
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /start"""
    await update.message.reply_text("👋 Привет! Я бот для пересылки сообщений между VK и Telegram.")

async def get_friends(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает список друзей из ВК и отправляет в Telegram"""
    try:
        user_id = update.effective_user.id
        if str(user_id) != AUTHORIZED_TELEGRAM_USER_ID:
            await update.message.reply_text("⛔ У вас нет доступа к этой команде!")
            return

        friends = vk.friends.get(order="hints", count=50, fields="first_name,last_name")["items"]

        if not friends:
            await update.message.reply_text("🤷 У вас нет друзей в VK.")
            return

        buttons = []
        for friend in friends:
            friend_id = friend["id"]
            name = f"{friend.get('first_name', 'Неизвестно')} {friend.get('last_name', '')}"
            buttons.append([InlineKeyboardButton(name, callback_data=f"write_{friend_id}")])

        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text("👥 Ваши друзья:", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Ошибка получения друзей: {e}", exc_info=True)
        await update.message.reply_text("❌ Ошибка при получении друзей.")

async def handle_friend_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор друга для начала диалога"""
    query = update.callback_query
    await query.answer()

    friend_id = query.data.replace("write_", "")
    context.user_data["selected_friend_id"] = friend_id

    await query.message.reply_text(f"📝 Теперь вы можете написать @id{friend_id} (VK). Отправьте сообщение:")

async def send_message_to_friend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет сообщение выбранному другу в VK"""
    user_id = update.effective_user.id
    if str(user_id) != AUTHORIZED_TELEGRAM_USER_ID:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде!")
        return

    friend_id = context.user_data.get("selected_friend_id")
    if not friend_id:
        await update.message.reply_text("⚠️ Сначала выберите друга командой /friends!")
        return

    text = update.message.text
    try:
        vk.messages.send(user_id=friend_id, message=text, random_id=0)
        await update.message.reply_text(f"✅ Сообщение отправлено @id{friend_id} (VK)")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}", exc_info=True)
        await update.message.reply_text("❌ Ошибка отправки сообщения.")

# Регистрация команд в Telegram-боте
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("friends", get_friends))
application.add_handler(CallbackQueryHandler(handle_friend_selection, pattern=r"write_\d+"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, send_message_to_friend))

# Запуск бота
if __name__ == "__main__":
    logger.info("Бот запущен!")
    application.run_polling()