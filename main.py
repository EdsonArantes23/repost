import asyncio
import os
import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

# --- ЛОГИ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- НАСТРОЙКИ (ЖЁСТКО В КОДЕ) ---
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")  # Bothost пробрасывает сам
TELEGRAM_CHANNEL_ID = -1003857194781         # Ваш канал
ADMIN_ID = 417850992                         # Ваш Telegram ID

SENT_POSTS_FILE = "sent_posts.txt"
ACCOUNTS_FILE = "x_accounts.txt"

# --- ЗАГРУЗКА/СОХРАНЕНИЕ АККАУНТОВ ---
def load_accounts():
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = [line.strip() for line in f if line.strip()]
        return accounts if accounts else ["ChelseaFC"]
    except FileNotFoundError:
        default = ["ChelseaFC", "FabrizioRomano"]
        save_accounts(default)
        return default

def save_accounts(accounts):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        for acc in accounts:
            f.write(acc + "\n")

# --- РАБОТА С ОТПРАВЛЕННЫМИ ---
def load_sent_posts():
    try:
        with open(SENT_POSTS_FILE, "r") as f:
            return set(line.strip() for line in f)
    except FileNotFoundError:
        return set()

def save_sent_post(post_id):
    with open(SENT_POSTS_FILE, "a") as f:
        f.write(post_id + "\n")

# --- ПАРСИНГ ТВИТОВ (БЕСПЛАТНО, БЕЗ API) ---
async def fetch_tweets(username):
    """
    Парсим твиты напрямую с xcancel.com.
    Возвращаем список словарей с текстом, ссылками и картинками.
    """
    url = f"https://xcancel.com/{username}"
    tweets = []

    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5"
            }
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "lxml")

            # Ищем все посты (твиты)
            tweet_divs = soup.select("div.timeline-item")

            for div in tweet_divs:
                # Текст твита
                content_div = div.select_one("div.tweet-content")
                if not content_div:
                    continue
                text = content_div.get_text(strip=True)

                # Ссылка на твит
                link_tag = div.select_one("a.tweet-link")
                if not link_tag:
                    continue
                link = link_tag.get("href", "")
                if link and not link.startswith("http"):
                    link = f"https://xcancel.com{link}"
                # Заменяем xcancel.com на x.com для финальной ссылки
                link = link.replace("xcancel.com", "x.com")

                # Картинки
                images = []
                # Ищем вложения
                attachment_divs = div.select("div.attachment, div.attachments")
                for att in attachment_divs:
                    img_tags = att.select("img")
                    for img in img_tags:
                        src = img.get("src", "")
                        if src and not src.startswith("data:"):
                            if src.startswith("/"):
                                src = f"https://xcancel.com{src}"
                            images.append(src)

                # Если нет вложений, ищем в теле твита
                if not images:
                    img_tags = div.select("div.tweet-body img")
                    for img in img_tags:
                        src = img.get("src", "")
                        if src and not src.startswith("data:") and "emoji" not in src.lower():
                            if src.startswith("/"):
                                src = f"https://xcancel.com{src}"
                            images.append(src)

                tweets.append({
                    "text": text,
                    "link": link,
                    "images": images,
                    "author": username
                })

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP ошибка для @{username}: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Ошибка парсинга @{username}: {e}")

    return tweets

# --- АДМИН-КОМАНДЫ ---
def is_admin(user_id):
    return user_id == ADMIN_ID

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Доступ запрещён.")
        return

    await update.message.reply_text(
        "👋 Привет, админ!\n\n"
        "📋 Команды управления:\n\n"
        "/add @username — добавить канал\n"
        "/remove @username — удалить канал\n"
        "/list — список каналов\n"
        "/status — проверка работы\n"
        "/force — принудительная проверка"
    )

async def cmd_add(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Доступ запрещён.")
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "❌ Укажите username.\nПример: /add @ChelseaFC"
        )
        return

    username = context.args[0].strip().lstrip("@")
    accounts = load_accounts()

    if username in accounts:
        await update.message.reply_text(f"⚠️ @{username} уже в списке.")
        return

    accounts.append(username)
    save_accounts(accounts)
    await update.message.reply_text(f"✅ @{username} добавлен.")

async def cmd_remove(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Доступ запрещён.")
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "❌ Укажите username.\nПример: /remove @ChelseaFC"
        )
        return

    username = context.args[0].strip().lstrip("@")
    accounts = load_accounts()

    if username not in accounts:
        await update.message.reply_text(f"⚠️ @{username} не найден.")
        return

    accounts.remove(username)
    save_accounts(accounts)
    await update.message.reply_text(f"✅ @{username} удалён.")

async def cmd_list(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Доступ запрещён.")
        return

    accounts = load_accounts()
    if not accounts:
        await update.message.reply_text("📋 Список пуст.")
        return

    accounts_list = "\n".join([f"• @{acc}" for acc in accounts])
    await update.message.reply_text(
        f"📋 Отслеживаемые каналы ({len(accounts)}):\n\n{accounts_list}"
    )

async def cmd_status(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Доступ запрещён.")
        return

    accounts = load_accounts()
    sent = len(load_sent_posts())
    await update.message.reply_text(
        f"✅ Бот активен\n"
        f"📡 Канал: @chelsea_news_insider\n"
        f"👀 Отслеживается: {len(accounts)} каналов\n"
        f"📤 Всего постов: {sent}"
    )

async def cmd_force(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Доступ запрещён.")
        return

    await update.message.reply_text("🔄 Запускаю принудительную проверку...")
    count = await check_and_post(context.bot)
    await update.message.reply_text(f"✅ Проверка завершена. Отправлено: {count} постов.")

# --- ОСНОВНАЯ ЛОГИКА ---
async def check_and_post(bot: Bot):
    accounts = load_accounts()
    sent_posts = load_sent_posts()
    new_posts = 0

    if not accounts:
        logger.info("Список аккаунтов пуст.")
        return new_posts

    logger.info(f"🔄 Проверка {len(accounts)} аккаунтов...")

    for username in accounts:
        logger.info(f"  Проверяю @{username}...")
        tweets = await fetch_tweets(username)

        if not tweets:
            continue

        for tweet in tweets:
            post_id = tweet["link"]

            if post_id in sent_posts:
                continue

            # Форматируем пост
            text = tweet["text"]
            author = tweet["author"]
            signature = f"\n\n{author} | {post_id}"
            full_text = text + signature

            images = tweet["images"]

            try:
                if images:
                    if len(images) == 1:
                        await bot.send_photo(
                            chat_id=TELEGRAM_CHANNEL_ID,
                            photo=images[0],
                            caption=full_text[:1024]  # Telegram limit for caption
                        )
                    else:
                        media = []
                        for i, img_url in enumerate(images[:10]):  # Max 10 photos
                            if i == 0:
                                media.append(InputMediaPhoto(
                                    media=img_url,
                                    caption=full_text[:1024]
                                ))
                            else:
                                media.append(InputMediaPhoto(media=img_url))
                        await bot.send_media_group(
                            chat_id=TELEGRAM_CHANNEL_ID,
                            media=media
                        )
                else:
                    await bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=full_text
                    )

                save_sent_post(post_id)
                sent_posts.add(post_id)
                new_posts += 1
                logger.info(f"✅ Пост от @{username}")

                await asyncio.sleep(2)

            except TelegramError as e:
                logger.error(f"Ошибка отправки: {e}")
                # Пробуем без фото
                try:
                    await bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=full_text
                    )
                    save_sent_post(post_id)
                    sent_posts.add(post_id)
                    new_posts += 1
                except:
                    pass

    return new_posts

# --- ТАЙМЕР ---
async def scheduled_check(bot: Bot):
    while True:
        try:
            await check_and_post(bot)
        except Exception as e:
            logger.error(f"Ошибка в цикле: {e}")
        await asyncio.sleep(120)  # Каждые 2 минуты

# --- ЗАПУСК ---
async def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("❌ Нет BOT_TOKEN!")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("force", cmd_force))

    # Фоновая проверка
    asyncio.create_task(scheduled_check(bot))

    # Стартовый список
    accounts = load_accounts()
    logger.info(f"🤖 Бот запущен. Каналов: {len(accounts)} — {', '.join(accounts)}")
    logger.info("📩 Админ-команды доступны в личке.")

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
