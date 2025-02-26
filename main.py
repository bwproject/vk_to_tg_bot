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
MESSAGE_SIGNATURE = os.getenv("MESSAGE_SIGNATURE", "Отправлено из Telegram")  # Подпись

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

    try:
        messages = vk.messages.getConversations(count=5)
        msg_list = messages.get("items", [])

        if not msg_list:
            await query.edit_message_text("❌ Нет новых сообщений.")
            return

        text = "📩 Последние сообщения:\n"
        keyboard = []

        for msg in msg_list:
            last_message = msg["last_message"]

            # Получаем имя отправителя
            user_id = last_message["from_id"]
            user_info = vk.users.get(user_ids=user_id, fields="first_name,last_name")[0]
            sender_name = f"{user_info.get('first_name', 'Неизвестно')} {user_info.get('last_name', 'Неизвестно')}"

            # Получаем имя получателя
            recipient_name = "Неизвестно"
            peer_id = msg["peer_id"]
            if peer_id <= 2e9:  # Это личное сообщение, получатель — человек
                try:
                    recipient_info = vk.users.get(user_ids=peer_id, fields="first_name,last_name")[0]
                    recipient_name = f"{recipient_info.get('first_name', 'Неизвестно')} {recipient_info.get('last_name', 'Неизвестно')}"
                except Exception as e:
                    logger.error(f"Ошибка при получении информации о получателе: {e}")

            reply_status = "✅ Ответили" if last_message.get("reply_message") else "❌ Не отвечено"
            reply_text = f"Ответ: {last_message['reply_message']['text']}" if last_message.get("reply_message") else ""

            text += f"\n👤 От: {recipient_name}\n{last_message['text'][:50]}...\n{reply_status}\n{reply_text}"
            keyboard.append([InlineKeyboardButton(sender_name, callback_data=f"open_dialog_{user_id}")])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.error(f"Ошибка при получении сообщений: {e}")
        await query.edit_message_text("❌ Не удалось получить сообщения.")

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
    
    # Проверяем, есть ли вложения и передаем их
    if update.message.photo:
        photo = update.message.photo[-1].file_id
        vk.messages.send(user_id=vk_user_id, message=message_text, random_id=0, attachment=photo)
    elif update.message.document:
        document = update.message.document.file_id
        vk.messages.send(user_id=vk_user_id, message=message_text, random_id=0, attachment=document)
    else: