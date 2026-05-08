import asyncio
import os
import logging
import sys
import subprocess
from io import StringIO

from telegram import Bot
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes
import feedparser

# --- ЛОГИ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- НАСТРОЙКИ ---
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")  # Bothost пробрасывает сам
TELEGRAM_CHANNEL_ID = -1003857194781         # Ваш канал
ADMIN_ID = 417850992                         # Ваш Telegram ID

SENT_POSTS_FILE = "sent_posts.txt"
ACCOUNTS_FILE = "x_accounts.txt"  # Список аккаунтов храним в файле

# --- ЗАГРУЗКА/СОХРАНЕНИЕ АККАУНТОВ ---
def load_accounts():
    """Читаем список отслеживаемых аккаунтов из файла"""
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = [line.strip() for line in f if line.strip()]
        return accounts if accounts else ["ChelseaFC"]  # Дефолтный если файл пуст
    except FileNotFoundError:
        # Если файла нет — создаём с дефолтным списком
        default = ["ChelseaFC", "FabrizioRomano"]
        save_accounts(default)
        return default

def save_accounts(accounts):
    """Сохраняем список аккаунтов в файл"""
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

# --- СКРАПИНГ ТВИТОВ (БЕСПЛАТНО, БЕЗ API) ---
def get_tweets_as_feed(username):
    """
    Получаем твиты через xcancel.com и tweeper.
    Возвращает feedparser-объект или None при ошибке.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "tweeper", f"https://xcancel.com/{username}"],
            capture_output=True,
            text=True,
            timeout=25
        )
        feed_xml = result.stdout
        if not feed_xml.strip():
            logger.warning(f"Пустой ответ от xcancel.com для @{username}")
            return None
        return feedparser.parse(StringIO(feed_xml))
    except subprocess.TimeoutExpired:
        logger.warning(f"Таймаут при получении твитов от @{username}")
        return None
    except Exception as e:
        logger.error(f"Ошибка получения твитов от @{username}: {e}")
        return None

def extract_images(entry):
    """
    Ищем ссылки на изображения в твите.
    Возвращаем список URL картинок.
    """
    images = []
    
    # Способ 1: через media_content в RSS
    if hasattr(entry, "media_content") and entry.media_content:
        for media in entry.media_content:
            if "url" in media and (media.get("type", "").startswith("image") or media["url"].endswith((".jpg", ".jpeg", ".png"))):
                images.append(media["url"])
    
    # Способ 2: через ссылки в summary/description
    if hasattr(entry, "summary") and entry.summary:
        import re
        img_urls = re.findall(r'https?://\S+?\.(?:jpg|jpeg|png|webp)', entry.summary, re.IGNORECASE)
        images.extend(img_urls)
    
    # Способ 3: через links (если есть фото напрямую)
    if hasattr(entry, "links"):
        for link in entry.links:
            if "image" in link.get("type", "") or link.get("href", "").endswith((".jpg", ".jpeg", ".png")):
                images.append(link["href"])
    
    # Убираем дубли
    return list(set(images))

def extract_author_info(entry, username):
    """
    Извлекаем имя автора и формируем подпись.
    """
    # Пробуем взять author из RSS
    author_name = None
    if hasattr(entry, "author") and entry.author:
        author_name = entry.author
    elif hasattr(entry, "source") and entry.source:
        if hasattr(entry.source, "title") and entry.source.title:
            author_name = entry.source.title
    
    # Если не нашли — используем username
    if not author_name:
        author_name = username
    
    # Очищаем имя (убираем лишнее)
    author_name = author_name.strip()
    
    # Если имя начинается с @ — оставляем как есть
    if not author_name.startswith("@"):
        author_name = f"@{author_name}"
    
    return author_name

def format_post(entry, username):
    """
    Форматируем пост в нужном стиле:
    
    Текст поста (полностью)
    
    ИмяАвтора | https://x.com/ИмяАвтораВСылке
    """
    text = entry.title if hasattr(entry, "title") and entry.title else ""
    
    # Очищаем текст от возможных URL в конце (если они приклеились)
    if " http" in text:
        text = text.rsplit(" http", 1)[0].strip()
    
    # Автор
    author_name = extract_author_info(entry, username)
    
    # Ссылка на пост
    post_link = entry.link if hasattr(entry, "link") else f"https://x.com/{username}"
    
    # Формируем подпись
    signature = f"\n\n{author_name} | {post_link}"
    
    return text + signature

# --- АДМИН-КОМАНДЫ ДЛЯ ЛИЧКИ ---
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
        "/force — принудительная проверка\n\n"
        "Формат поста:\n"
        "• Текст из твита\n"
        "• Фотографии (если есть)\n"
        "• Подпись: ИмяАвтора | ссылка"
    )

async def cmd_add(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Доступ запрещён.")
        return
    
    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "❌ Укажите username для добавления.\n"
            "Пример: /add @ChelseaFC"
        )
        return
    
    username = context.args[0].strip().lstrip("@")
    accounts = load_accounts()
    
    if username in accounts:
        await update.message.reply_text(f"⚠️ @{username} уже в списке.")
        return
    
    accounts.append(username)
    save_accounts(accounts)
    await update.message.reply_text(f"✅ @{username} добавлен в отслеживание.")

async def cmd_remove(update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Доступ запрещён.")
        return
    
    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "❌ Укажите username для удаления.\n"
            "Пример: /remove @ChelseaFC"
        )
        return
    
    username = context.args[0].strip().lstrip("@")
    accounts = load_accounts()
    
    if username not in accounts:
        await update.message.reply_text(f"⚠️ @{username} не найден в списке.")
        return
    
    accounts.remove(username)
    save_accounts(accounts)
    await update.message.reply_text(f"✅ @{username} удалён из отслеживания.")

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
    await check_and_post(context.bot)
    await update.message.reply_text("✅ Проверка завершена.")

# --- ОСНОВНАЯ ЛОГИКА ---
async def check_and_post(bot: Bot):
    accounts = load_accounts()
    sent_posts = load_sent_posts()
    
    if not accounts:
        logger.info("Список аккаунтов пуст. Пропускаем проверку.")
        return
    
    logger.info(f"🔄 Проверка {len(accounts)} аккаунтов...")
    
    for username in accounts:
        logger.info(f"  Проверяю @{username}...")
        feed = get_tweets_as_feed(username)
        
        if not feed:
            logger.warning(f"  Не удалось получить данные от @{username}")
            continue
        
        if not feed.entries:
            logger.info(f"  Нет новых записей от @{username}")
            continue
        
        for entry in feed.entries:
            post_id = entry.link if hasattr(entry, "link") else str(hash(entry.title))
            
            if post_id in sent_posts:
                continue
            
            # Форматируем пост
            text = format_post(entry, username)
            
            # Ищем картинки
            images = extract_images(entry)
            
            try:
                if images:
                    # Отправляем с первой картинкой и подписью
                    media_group = []
                    # Первое фото с подписью
                    media_group.append(images[0])
                    
                    if len(images) == 1:
                        # Одна картинка
                        await bot.send_photo(
                            chat_id=TELEGRAM_CHANNEL_ID,
                            photo=images[0],
                            caption=text,
                            parse_mode='HTML'
                        )
                    elif len(images) > 1:
                        # Несколько картинок — отправляем альбомом
                        from telegram import InputMediaPhoto
                        media = []
                        for i, img_url in enumerate(images):
                            if i == 0:
                                # Первое фото с подписью
                                media.append(InputMediaPhoto(media=img_url, caption=text, parse_mode='HTML'))
                            else:
                                media.append(InputMediaPhoto(media=img_url))
                        
                        await bot.send_media_group(
                            chat_id=TELEGRAM_CHANNEL_ID,
                            media=media
                        )
                else:
                    # Без картинок — просто текст
                    await bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=text,
                        parse_mode='HTML'
                    )
                
                save_sent_post(post_id)
                sent_posts.add(post_id)
                logger.info(f"✅ Отправлен пост от @{username}")
                
                await asyncio.sleep(2)  # Пауза между постами
                
            except TelegramError as e:
                logger.error(f"Ошибка отправки: {e}")
                # Пробуем отправить без фото
                try:
                    await bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=text,
                        parse_mode='HTML'
                    )
                    save_sent_post(post_id)
                    sent_posts.add(post_id)
                except:
                    pass

# --- ТАЙМЕР ---
async def scheduled_check(bot: Bot):
    while True:
        try:
            await check_and_post(bot)
        except Exception as e:
            logger.error(f"Ошибка в цикле проверки: {e}")
        await asyncio.sleep(120)  # Проверка каждые 2 минуты

# --- ЗАПУСК ---
async def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("❌ Нет BOT_TOKEN!")
        return
    
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Регистрируем команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("force", cmd_force))
    
    # Фоновая проверка
    asyncio.create_task(scheduled_check(bot))
    
    logger.info("🤖 Бот запущен. Админка через личку.")
    # Печатаем список аккаунтов при старте
    accounts = load_accounts()
    logger.info(f"👀 Отслеживается {len(accounts)} аккаунтов: {', '.join(accounts)}")
    
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
