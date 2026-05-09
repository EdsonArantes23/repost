import asyncio
import os
import logging
import re
import time

import httpx
import nest_asyncio
import feedparser
from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

# --- ЛОГИ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- НАСТРОЙКИ ---
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHANNEL_ID = -1003857194781
ADMIN_ID = 417850992

RSSHUB_URL = "https://chelsea-rss-bridge.onrender.com"

SENT_POSTS_FILE = "sent_posts.txt"
FANS_FILE = "chelsea_fans.txt"
BLOGGERS_FILE = "general_bloggers.txt"
KEYWORDS_FILE = "keywords.txt"

# Глобальный кеш отправленных — чтобы не читать файл каждый раз
sent_posts_cache = set()
adding_lock = asyncio.Lock()

# --- ЗАГРУЗКА/СОХРАНЕНИЕ ---
def load_list(filename):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        save_list(filename, [])
        return []

def save_list(filename, items):
    with open(filename, "w", encoding="utf-8") as f:
        for item in items:
            f.write(item + "\n")

def load_fans():
    return load_list(FANS_FILE)

def save_fans(fans):
    save_list(FANS_FILE, fans)

def load_bloggers():
    return load_list(BLOGGERS_FILE)

def save_bloggers(bloggers):
    save_list(BLOGGERS_FILE, bloggers)

def load_keywords():
    return load_list(KEYWORDS_FILE)

def save_keywords(keywords):
    save_list(KEYWORDS_FILE, keywords)

def load_sent_posts():
    global sent_posts_cache
    try:
        with open(SENT_POSTS_FILE, "r") as f:
            sent_posts_cache = set(line.strip() for line in f)
    except FileNotFoundError:
        sent_posts_cache = set()
    return sent_posts_cache

def save_sent_post(post_id):
    global sent_posts_cache
    sent_posts_cache.add(post_id)
    with open(SENT_POSTS_FILE, "a") as f:
        f.write(post_id + "\n")

# --- ПРОВЕРКА ФИЛЬТРА ---
def post_matches_filter(text, keywords):
    if not keywords:
        return True
    text_lower = text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return True
    return False

# --- ИЗВЛЕЧЕНИЕ МЕДИА ---
def extract_media(entry):
    """Извлекает картинки и видео из RSS-записи."""
    images = []
    videos = []

    # Способ 1: media_content
    if hasattr(entry, "media_content") and entry.media_content:
        for media in entry.media_content:
            url = media.get("url", "")
            if url and url not in images:
                images.append(url)

    # Способ 2: description/summary
    description = getattr(entry, "description", "") or getattr(entry, "summary", "")
    if description:
        # Картинки
        img_urls = re.findall(r'<img[^>]+src="([^"]+)"', description)
        for url in img_urls:
            if url not in images:
                images.append(url)
        # Видео
        video_urls = re.findall(r'<video[^>]+src="([^"]+)"', description)
        for url in video_urls:
            if url not in videos:
                videos.append(url)

    # Способ 3: links
    if hasattr(entry, "links"):
        for link in entry.links:
            href = link.get("href", "")
            link_type = link.get("type", "")
            if href:
                if "image" in link_type:
                    if href not in images:
                        images.append(href)
                elif "video" in link_type:
                    if href not in videos:
                        videos.append(href)

    return images, videos

# --- ПОЛУЧЕНИЕ ТВИТОВ ---
async def fetch_tweets(username):
    """Получает твиты через RSSHub."""
    url = f"{RSSHUB_URL}/twitter/user/{username}"
    tweets = []
    display_name = username

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml, */*"}
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            feed = feedparser.parse(response.text)

            if hasattr(feed.feed, "title"):
                display_name = feed.feed.title.replace("Twitter @", "").strip()

            for entry in feed.entries:
                text = entry.title if hasattr(entry, "title") else ""
                link = entry.link if hasattr(entry, "link") else ""
                images, videos = extract_media(entry)

                if images:
                    logger.info(f"🖼 @{username}: найдено {len(images)} фото для «{text[:50]}...»")

                tweets.append({
                    "text": text,
                    "link": link,
                    "images": images,
                    "videos": videos,
                    "display_name": display_name,
                    "username": username
                })

        logger.info(f"✅ @{username}: {len(tweets)} твитов")
        return display_name, tweets

    except Exception as e:
        logger.error(f"❌ @{username}: {type(e).__name__}: {e}")
        return username, []

async def mark_all_current_as_sent(username):
    """Отмечает все текущие посты."""
    _, tweets = await fetch_tweets(username)
    if tweets:
        sent_posts = load_sent_posts()
        count = 0
        for tweet in tweets:
            link = tweet["link"]
            if link not in sent_posts:
                sent_posts.add(link)
                save_sent_post(link)
                count += 1
        logger.info(f"📌 @{username}: {count} отмечено")
        return count
    return 0

# --- АДМИН-КОМАНДЫ ---
def is_admin(user_id):
    return user_id == ADMIN_ID

async def cmd_start(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(
        "👋 Привет, админ!\n\n"
        "📋 Фан-каналы:\n/addfan, /addmanyfan, /removefan, /listfan\n\n"
        "📋 Блогеры:\n/addblogger, /addmanyblogger, /removeblogger, /listbloggers\n\n"
        "🔑 Ключевые слова:\n/addword, /addwords, /removeword, /listwords\n\n"
        "📊 /status, /force"
    )

async def cmd_addfan(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ /addfan @username"); return
    username = context.args[0].strip().lstrip("@")
    fans = load_fans()
    if username in fans:
        await update.message.reply_text(f"⚠️ @{username} уже в фан-каналах."); return
    async with adding_lock:
        await update.message.reply_text(f"⏳ Добавляю @{username}...")
        try:
            display_name, _ = await fetch_tweets(username)
            fans.append(username)
            save_fans(fans)
            count = await mark_all_current_as_sent(username)
            await update.message.reply_text(f"✅ {display_name} (@{username}) добавлен.\n📤 {count} постов пропущено.")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_addmanyfan(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ /addmanyfan ссылки"); return
    raw_input = " ".join(context.args)
    mentions = re.findall(r'@(\w+)', raw_input)
    links = re.findall(r'https?://(?:x\.com|twitter\.com)/(\w+)', raw_input)
    usernames = list(dict.fromkeys(mentions + links))
    if not usernames:
        await update.message.reply_text("❌ Не удалось распознать username."); return
    async with adding_lock:
        fans = load_fans()
        added, skipped, failed = [], [], []
        await update.message.reply_text(f"⏳ Обрабатываю {len(usernames)} каналов...")
        for username in usernames:
            if username in fans:
                skipped.append(f"• @{username}")
                continue
            try:
                display_name, _ = await fetch_tweets(username)
                fans.append(username)
                save_fans(fans)
                count = await mark_all_current_as_sent(username)
                added.append(f"✅ {display_name} (@{username}) — {count} пропущено")
            except:
                failed.append(f"❌ @{username}")
            await asyncio.sleep(0.3)
        report = []
        if added: report.append(f"✨ Добавлены ({len(added)}):\n" + "\n".join(added))
        if skipped: report.append(f"⚠️ Уже были ({len(skipped)}):\n" + "\n".join(skipped))
        if failed: report.append(f"❌ Не удалось ({len(failed)}):\n" + "\n".join(failed))
        await update.message.reply_text("\n\n".join(report) if report else "Ничего не изменилось.")

async def cmd_removefan(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ /removefan @username"); return
    username = context.args[0].strip().lstrip("@")
    fans = load_fans()
    if username not in fans:
        await update.message.reply_text(f"⚠️ @{username} не найден."); return
    fans.remove(username)
    save_fans(fans)
    await update.message.reply_text(f"✅ @{username} удалён.")

async def cmd_listfan(update, context):
    if not is_admin(update.effective_user.id): return
    fans = load_fans()
    if not fans:
        await update.message.reply_text("📋 Фан-каналы: пусто."); return
    await update.message.reply_text("📋 Фан-каналы:\n" + "\n".join([f"• @{f}" for f in fans]))

async def cmd_addblogger(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ /addblogger @username"); return
    username = context.args[0].strip().lstrip("@")
    bloggers = load_bloggers()
    if username in bloggers:
        await update.message.reply_text(f"⚠️ @{username} уже в блогерах."); return
    keywords = load_keywords()
    kw_msg = f"🔑 Слова: {', '.join(keywords)}" if keywords else "⚠️ Слов нет — репостится всё!"
    async with adding_lock:
        await update.message.reply_text(f"⏳ Добавляю @{username}...\n{kw_msg}")
        try:
            display_name, _ = await fetch_tweets(username)
            bloggers.append(username)
            save_bloggers(bloggers)
            count = await mark_all_current_as_sent(username)
            await update.message.reply_text(f"✅ {display_name} (@{username}) добавлен.\n📤 {count} постов пропущено.")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_addmanyblogger(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ /addmanyblogger ссылки"); return
    raw_input = " ".join(context.args)
    mentions = re.findall(r'@(\w+)', raw_input)
    links = re.findall(r'https?://(?:x\.com|twitter\.com)/(\w+)', raw_input)
    usernames = list(dict.fromkeys(mentions + links))
    if not usernames:
        await update.message.reply_text("❌ Не удалось распознать username."); return
    async with adding_lock:
        bloggers = load_bloggers()
        keywords = load_keywords()
        kw_msg = f"🔑 Слова: {', '.join(keywords)}" if keywords else "⚠️ Слов нет — репостится всё!"
        added, skipped, failed = [], [], []
        await update.message.reply_text(f"⏳ Обрабатываю {len(usernames)} блогеров...\n{kw_msg}")
        for username in usernames:
            if username in bloggers:
                skipped.append(f"• @{username}")
                continue
            try:
                display_name, _ = await fetch_tweets(username)
                bloggers.append(username)
                save_bloggers(bloggers)
                count = await mark_all_current_as_sent(username)
                added.append(f"✅ {display_name} (@{username}) — {count} пропущено")
            except:
                failed.append(f"❌ @{username}")
            await asyncio.sleep(0.3)
        report = []
        if added: report.append(f"✨ Добавлены ({len(added)}):\n" + "\n".join(added))
        if skipped: report.append(f"⚠️ Уже были ({len(skipped)}):\n" + "\n".join(skipped))
        if failed: report.append(f"❌ Не удалось ({len(failed)}):\n" + "\n".join(failed))
        await update.message.reply_text("\n\n".join(report) if report else "Ничего не изменилось.")

async def cmd_removeblogger(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ /removeblogger @username"); return
    username = context.args[0].strip().lstrip("@")
    bloggers = load_bloggers()
    if username not in bloggers:
        await update.message.reply_text(f"⚠️ @{username} не найден."); return
    bloggers.remove(username)
    save_bloggers(bloggers)
    await update.message.reply_text(f"✅ @{username} удалён.")

async def cmd_listbloggers(update, context):
    if not is_admin(update.effective_user.id): return
    bloggers = load_bloggers()
    keywords = load_keywords()
    kw_msg = f"🔑 Слова: {', '.join(keywords)}" if keywords else "⚠️ Слов нет"
    if not bloggers:
        await update.message.reply_text(f"📋 Блогеры: пусто.\n{kw_msg}"); return
    await update.message.reply_text(f"📋 Блогеры:\n" + "\n".join([f"• @{b}" for b in bloggers]) + f"\n\n{kw_msg}")

async def cmd_addword(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ /addword слово"); return
    word = context.args[0].strip().lower()
    keywords = load_keywords()
    if word in keywords:
        await update.message.reply_text(f"⚠️ '{word}' уже в списке."); return
    keywords.append(word)
    save_keywords(keywords)
    await update.message.reply_text(f"✅ '{word}' добавлен. Всего: {len(keywords)}")

async def cmd_addwords(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ /addwords слово1 слово2 ..."); return
    raw_input = " ".join(context.args)
    words = re.split(r'[,\s;\n]+', raw_input)
    words = [w.strip().lower() for w in words if w.strip()]
    if not words:
        await update.message.reply_text("❌ Не удалось распознать слова."); return
    keywords = load_keywords()
    added, skipped = [], []
    for word in words:
        if word in keywords:
            skipped.append(word)
        else:
            keywords.append(word)
            added.append(word)
    save_keywords(keywords)
    report = []
    if added: report.append(f"✅ Добавлены ({len(added)}): {', '.join(added)}")
    if skipped: report.append(f"⚠️ Уже были ({len(skipped)}): {', '.join(skipped)}")
    await update.message.reply_text("\n".join(report) + f"\n\n🔑 Всего: {len(keywords)}")

async def cmd_removeword(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ /removeword слово"); return
    word = context.args[0].strip().lower()
    keywords = load_keywords()
    if word not in keywords:
        await update.message.reply_text(f"⚠️ '{word}' не найден."); return
    keywords.remove(word)
    save_keywords(keywords)
    await update.message.reply_text(f"✅ '{word}' удалён. Всего: {len(keywords)}")

async def cmd_listwords(update, context):
    if not is_admin(update.effective_user.id): return
    keywords = load_keywords()
    if not keywords:
        await update.message.reply_text("🔑 Ключевых слов нет."); return
    await update.message.reply_text(f"🔑 Ключевые слова ({len(keywords)}):\n" + ", ".join(keywords))

async def cmd_status(update, context):
    if not is_admin(update.effective_user.id): return
    fans = load_fans()
    bloggers = load_bloggers()
    keywords = load_keywords()
    sent = len(sent_posts_cache)
    await update.message.reply_text(
        f"✅ Бот активен\n📡 @chelsea_news_insider\n🔵 Фан-каналов: {len(fans)}\n🟡 Блогеров: {len(bloggers)}\n🔑 Слов: {len(keywords)}\n📤 Постов: {sent}"
    )

async def cmd_force(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("🔄 Проверка...")
    count = await check_and_post(context.bot)
    await update.message.reply_text(f"✅ Отправлено: {count} постов.")

# --- ОСНОВНАЯ ЛОГИКА ---
async def send_post(bot: Bot, tweet, username):
    text = tweet["text"]
    signature = f"\n\n{tweet['display_name']} | https://x.com/{username}"
    full_text = text + signature
    images = tweet["images"]
    videos = tweet["videos"]

    try:
        if videos:
            await bot.send_video(
                TELEGRAM_CHANNEL_ID,
                video=videos[0],
                caption=full_text[:1024],
                supports_streaming=True,
                disable_web_page_preview=True
            )
        elif images:
            if len(images) == 1:
                await bot.send_photo(
                    TELEGRAM_CHANNEL_ID,
                    images[0],
                    caption=full_text[:1024]
                )
            else:
                media = []
                for i, img in enumerate(images[:10]):
                    if i == 0:
                        media.append(InputMediaPhoto(media=img, caption=full_text[:1024]))
                    else:
                        media.append(InputMediaPhoto(media=img))
                await bot.send_media_group(TELEGRAM_CHANNEL_ID, media)
        else:
            await bot.send_message(
                TELEGRAM_CHANNEL_ID,
                full_text,
                disable_web_page_preview=True
            )
    except TelegramError as e:
        logger.error(f"Ошибка отправки: {e}")
        try:
            await bot.send_message(TELEGRAM_CHANNEL_ID, full_text, disable_web_page_preview=True)
        except:
            pass

async def check_and_post(bot: Bot):
    global sent_posts_cache

    async with adding_lock:
        pass

    fans = load_fans()
    bloggers = load_bloggers()
    keywords = load_keywords()

    # Обновляем кеш отправленных
    if not sent_posts_cache:
        sent_posts_cache = load_sent_posts()

    new_posts = 0

    for username in fans:
        try:
            _, tweets = await fetch_tweets(username)
        except:
            continue
        for tweet in tweets:
            link = tweet["link"]
            if link in sent_posts_cache:
                continue
            await send_post(bot, tweet, username)
            save_sent_post(link)  # Сохраняет и в файл, и в кеш
            new_posts += 1
            await asyncio.sleep(3)

    for username in bloggers:
        try:
            _, tweets = await fetch_tweets(username)
        except:
            continue
        for tweet in tweets:
            link = tweet["link"]
            if link in sent_posts_cache:
                continue
            if not post_matches_filter(tweet["text"], keywords):
                continue
            await send_post(bot, tweet, username)
            save_sent_post(link)
            new_posts += 1
            await asyncio.sleep(3)

    if new_posts:
        logger.info(f"📤 Отправлено {new_posts} новых постов")

    return new_posts

async def scheduled_check(bot: Bot):
    await asyncio.sleep(10)
    while True:
        try:
            await check_and_post(bot)
        except Exception as e:
            logger.error(f"Цикл: {e}")
        await asyncio.sleep(120)

async def main():
    global sent_posts_cache
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("❌ Нет BOT_TOKEN!")
        return

    sent_posts_cache = load_sent_posts()

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addfan", cmd_addfan))
    app.add_handler(CommandHandler("addmanyfan", cmd_addmanyfan))
    app.add_handler(CommandHandler("removefan", cmd_removefan))
    app.add_handler(CommandHandler("listfan", cmd_listfan))
    app.add_handler(CommandHandler("addblogger", cmd_addblogger))
    app.add_handler(CommandHandler("addmanyblogger", cmd_addmanyblogger))
    app.add_handler(CommandHandler("removeblogger", cmd_removeblogger))
    app.add_handler(CommandHandler("listbloggers", cmd_listbloggers))
    app.add_handler(CommandHandler("addword", cmd_addword))
    app.add_handler(CommandHandler("addwords", cmd_addwords))
    app.add_handler(CommandHandler("removeword", cmd_removeword))
    app.add_handler(CommandHandler("listwords", cmd_listwords))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("force", cmd_force))
    asyncio.create_task(scheduled_check(bot))
    logger.info("🤖 Бот запущен через RSSHub")
    await app.run_polling()

if __name__ == "__main__":
    nest_asyncio.apply()
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(main())
        while True:
            time.sleep(3600)
    except RuntimeError:
        asyncio.run(main())
