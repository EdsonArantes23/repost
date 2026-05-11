import asyncio
import os
import logging
import re
import time
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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

MAX_PARALLEL = 10
semaphore = asyncio.Semaphore(MAX_PARALLEL)

sent_posts_cache = set()
adding_lock = asyncio.Lock()

BOOT_TIME = None

# --- ВРЕМЯ ЗАПУСКА БОТА ---
def get_boot_time():
    global BOOT_TIME
    if BOOT_TIME:
        return BOOT_TIME

    try:
        with open(SENT_POSTS_FILE, "r") as f:
            for line in f:
                if line.startswith("BOOT_TIME:"):
                    BOOT_TIME = datetime.fromisoformat(line.strip().split(":", 1)[1].strip())
                    logger.info(f"🕒 Найдено время запуска: {BOOT_TIME.isoformat()}")
                    return BOOT_TIME
    except FileNotFoundError:
        pass

    BOOT_TIME = datetime.now(timezone.utc)
    with open(SENT_POSTS_FILE, "a") as f:
        f.write(f"BOOT_TIME:{BOOT_TIME.isoformat()}\n")
    logger.info(f"🕒 Установлено время запуска: {BOOT_TIME.isoformat()}")
    logger.info("📌 Твиты старше этого времени не будут отправлены")
    return BOOT_TIME

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

def load_fans(): return load_list(FANS_FILE)
def save_fans(fans): save_list(FANS_FILE, fans)
def load_bloggers(): return load_list(BLOGGERS_FILE)
def save_bloggers(bloggers): save_list(BLOGGERS_FILE, bloggers)
def load_keywords(): return load_list(KEYWORDS_FILE)
def save_keywords(keywords): save_list(KEYWORDS_FILE, keywords)

def load_sent_posts():
    global sent_posts_cache
    try:
        with open(SENT_POSTS_FILE, "r") as f:
            sent_posts_cache = set(line.strip() for line in f if not line.startswith("BOOT_TIME:"))
    except FileNotFoundError:
        sent_posts_cache = set()
    return sent_posts_cache

def save_sent_post(post_id):
    global sent_posts_cache
    sent_posts_cache.add(post_id)
    with open(SENT_POSTS_FILE, "a") as f:
        f.write(post_id + "\n")

def post_matches_filter(text, keywords):
    """Точное совпадение слов с границами \b."""
    if not keywords:
        return True
    text_lower = text.lower()
    for kw in keywords:
        pattern = r'\b' + re.escape(kw.lower()) + r'\b'
        if re.search(pattern, text_lower):
            return True
    return False

def escape_html(text):
    """Экранирует HTML-спецсимволы, кроме наших тегов."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def clean_html(text):
    """Убирает HTML-теги и entities из текста."""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    text = text.replace('&#39;', "'").replace('&quot;', '"')
    text = text.replace('&nbsp;', ' ')
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()

def extract_images(entry):
    images = []
    raw_desc = getattr(entry, "description", "") or getattr(entry, "summary", "")

    img_urls = re.findall(r'<img[^>]+src="([^"]+)"', raw_desc)
    for url in img_urls:
        url = url.replace("&amp;", "&")
        if url not in images and "pbs.twimg.com" in url:
            images.append(url)

    if not images and hasattr(entry, "media_content") and entry.media_content:
        for media in entry.media_content:
            url = media.get("url", "").replace("&amp;", "&")
            if url and url not in images:
                images.append(url)

    if not images:
        direct_urls = re.findall(r'https?://pbs\.twimg\.com/media/[^\s"\'&]+', raw_desc)
        for url in direct_urls:
            url = url.replace("&amp;", "&")
            if url not in images:
                images.append(url)

    if not images and hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            url = enc.get("href", "").replace("&amp;", "&")
            if url and url not in images and "image" in enc.get("type", ""):
                images.append(url)

    return images

def extract_text(entry):
    description = getattr(entry, "description", "") or getattr(entry, "summary", "")

    if description:
        parts = re.split(r'<hr[^>]*>|<div class="rsshub-quote">', description)
        main_text = parts[0]
        quote_text = ""
        if len(parts) > 1:
            quote_raw = parts[1]
            quote_text = clean_html(quote_raw)
            if quote_text:
                quote_text = f"\n\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n{quote_text}\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄"

        text_with_breaks = re.sub(r'<br\s*/?>', '\n', main_text)
        text = clean_html(text_with_breaks)
        text = text + quote_text

        if text.strip():
            external_urls = re.findall(r'https?://[^\s"\'<&]+', description)
            for url in external_urls:
                if any(d in url for d in ['x.com', 'twitter.com', 'pbs.twimg.com', 'video.twimg.com']):
                    continue
                if url not in text:
                    text = text + "\n" + url
            return text.strip()

    title = getattr(entry, "title", "") or ""
    return clean_html(title)

def extract_videos(entry):
    videos = []
    raw_desc = getattr(entry, "description", "") or getattr(entry, "summary", "")

    video_urls = re.findall(r'<video[^>]+src="([^"]+)"', raw_desc)
    for url in video_urls:
        url = url.replace("&amp;", "&")
        if url not in videos:
            videos.append(url)

    source_urls = re.findall(r'<source[^>]+src="([^"]+)"', raw_desc)
    for url in source_urls:
        url = url.replace("&amp;", "&")
        if url not in videos:
            videos.append(url)

    return videos

async def fetch_tweets(username):
    url = f"{RSSHUB_URL}/twitter/user/{username}"

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml, */*"}
                response = await client.get(url, headers=headers)

                if response.status_code == 503:
                    wait = 3 if attempt == 0 else 5
                    logger.warning(f"⚠️ @{username}: 503, попытка {attempt+1}/2, жду {wait} сек...")
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                feed = feedparser.parse(response.text)

                display_name = username
                if hasattr(feed.feed, "title"):
                    display_name = feed.feed.title.replace("Twitter @", "").strip()

                tweets = []
                for entry in feed.entries:
                    text = extract_text(entry)
                    images = extract_images(entry)
                    videos = extract_videos(entry)
                    link = entry.link if hasattr(entry, "link") else ""
                    published = entry.published if hasattr(entry, "published") else None

                    tweets.append({
                        "text": text, "link": link,
                        "images": images, "videos": videos,
                        "display_name": display_name, "username": username,
                        "published": published
                    })

                return display_name, tweets

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 503:
                wait = 3 if attempt == 0 else 5
                logger.warning(f"⚠️ @{username}: 503, попытка {attempt+1}/2, жду {wait} сек...")
                await asyncio.sleep(wait)
            else:
                logger.error(f"❌ @{username}: HTTP {e.response.status_code}")
                break
        except (httpx.ConnectTimeout, httpx.ReadTimeout):
            logger.warning(f"⚠️ @{username}: таймаут, попытка {attempt+1}/2")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"❌ @{username}: {type(e).__name__}: {e}")
            break

    logger.error(f"❌ @{username}: не удалось после 2 попыток")
    return username, []

async def fetch_tweets_with_limit(username):
    async with semaphore:
        return await fetch_tweets(username)

async def fetch_all_tweets(usernames):
    tasks = [fetch_tweets_with_limit(username) for username in usernames]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_tweets = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"❌ Ошибка при проверке: {result}")
            continue
        display_name, tweets = result
        if tweets:
            all_tweets.extend(tweets)

    return all_tweets

def extract_tweet_id(tweet):
    link = tweet.get("link", "")
    match = re.search(r'/status/(\d+)', link)
    return int(match.group(1)) if match else 0

async def mark_all_current_as_sent(username):
    _, tweets = await fetch_tweets_with_limit(username)
    if tweets:
        sent_posts = load_sent_posts()
        count = 0
        for tweet in tweets:
            link = tweet["link"]
            if link not in sent_posts:
                sent_posts.add(link)
                save_sent_post(link)
                count += 1
        return count
    return 0

def is_tweet_too_old(tweet, username=None):
    boot_time = get_boot_time()
    if not boot_time:
        return False

    published_str = tweet.get("published")
    if not published_str:
        return False

    try:
        tweet_time = parsedate_to_datetime(published_str)
        return tweet_time < boot_time
    except:
        return False

def is_admin(user_id): return user_id == ADMIN_ID

# --- АДМИН-КОМАНДЫ ---
async def cmd_start(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(
        "👋 Привет, админ!\n\n"
        "📋 Фан-каналы:\n/addfan, /addmanyfan, /removefan, /listfan\n\n"
        "📋 Блогеры:\n/addblogger, /addmanyblogger, /removeblogger, /removemanyblogger, /listbloggers\n\n"
        "🔑 Ключевые слова:\n/addword, /addwords, /removeword, /removemanywords, /listwords\n\n"
        "📊 /status, /force"
    )

async def cmd_addfan(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addfan @username"); return
    username = context.args[0].strip().lstrip("@")
    fans = load_fans()
    if username in fans: await update.message.reply_text(f"⚠️ @{username} уже в фан-каналах."); return
    async with adding_lock:
        await update.message.reply_text(f"⏳ Добавляю @{username}...")
        try:
            display_name, _ = await fetch_tweets_with_limit(username)
            fans.append(username)
            save_fans(fans)
            count = await mark_all_current_as_sent(username)
            await update.message.reply_text(f"✅ {display_name} (@{username}) добавлен.\n📤 {count} постов пропущено.")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_addmanyfan(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addmanyfan ссылки"); return
    raw_input = " ".join(context.args)
    mentions = re.findall(r'@(\w+)', raw_input)
    links = re.findall(r'https?://(?:x\.com|twitter\.com)/(\w+)', raw_input)
    usernames = list(dict.fromkeys(mentions + links))
    if not usernames: await update.message.reply_text("❌ Не удалось распознать username."); return
    async with adding_lock:
        fans = load_fans()
        added, skipped, failed = [], [], []
        await update.message.reply_text(f"⏳ Обрабатываю {len(usernames)} каналов...")
        for username in usernames:
            if username in fans: skipped.append(f"• @{username}"); continue
            try:
                display_name, _ = await fetch_tweets_with_limit(username)
                fans.append(username)
                save_fans(fans)
                count = await mark_all_current_as_sent(username)
                added.append(f"✅ {display_name} (@{username}) — {count} пропущено")
            except: failed.append(f"❌ @{username}")
            await asyncio.sleep(0.3)
        report = []
        if added: report.append(f"✨ Добавлены ({len(added)}):\n" + "\n".join(added))
        if skipped: report.append(f"⚠️ Уже были ({len(skipped)}):\n" + "\n".join(skipped))
        if failed: report.append(f"❌ Не удалось ({len(failed)}):\n" + "\n".join(failed))
        await update.message.reply_text("\n\n".join(report) if report else "Ничего не изменилось.")

async def cmd_removefan(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /removefan @username"); return
    username = context.args[0].strip().lstrip("@")
    fans = load_fans()
    if username not in fans: await update.message.reply_text(f"⚠️ @{username} не найден."); return
    fans.remove(username)
    save_fans(fans)
    await update.message.reply_text(f"✅ @{username} удалён.")

async def cmd_listfan(update, context):
    if not is_admin(update.effective_user.id): return
    fans = load_fans()
    if not fans: await update.message.reply_text("📋 Фан-каналы: пусто."); return
    await update.message.reply_text("📋 Фан-каналы:\n" + "\n".join([f"• @{f}" for f in fans]))

async def cmd_addblogger(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addblogger @username"); return
    username = context.args[0].strip().lstrip("@")
    bloggers = load_bloggers()
    if username in bloggers: await update.message.reply_text(f"⚠️ @{username} уже в блогерах."); return
    keywords = load_keywords()
    kw_msg = f"🔑 Слов: {len(keywords)}" if keywords else "⚠️ Слов нет — репостится всё!"
    async with adding_lock:
        await update.message.reply_text(f"⏳ Добавляю @{username}...\n{kw_msg}")
        try:
            display_name, _ = await fetch_tweets_with_limit(username)
            bloggers.append(username)
            save_bloggers(bloggers)
            count = await mark_all_current_as_sent(username)
            await update.message.reply_text(f"✅ {display_name} (@{username}) добавлен.\n📤 {count} постов пропущено.")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_addmanyblogger(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addmanyblogger ссылки"); return
    raw_input = " ".join(context.args)
    mentions = re.findall(r'@(\w+)', raw_input)
    links = re.findall(r'https?://(?:x\.com|twitter\.com)/(\w+)', raw_input)
    usernames = list(dict.fromkeys(mentions + links))
    if not usernames: await update.message.reply_text("❌ Не удалось распознать username."); return
    async with adding_lock:
        bloggers = load_bloggers()
        keywords = load_keywords()
        kw_msg = f"🔑 Слов: {len(keywords)}" if keywords else "⚠️ Слов нет — репостится всё!"
        added, skipped, failed = [], [], []
        await update.message.reply_text(f"⏳ Обрабатываю {len(usernames)} блогеров...\n{kw_msg}")
        for username in usernames:
            if username in bloggers: skipped.append(f"• @{username}"); continue
            try:
                display_name, _ = await fetch_tweets_with_limit(username)
                bloggers.append(username)
                save_bloggers(bloggers)
                count = await mark_all_current_as_sent(username)
                added.append(f"✅ {display_name} (@{username}) — {count} пропущено")
            except: failed.append(f"❌ @{username}")
            await asyncio.sleep(0.3)
        report = []
        if added: report.append(f"✨ Добавлены ({len(added)}):\n" + "\n".join(added))
        if skipped: report.append(f"⚠️ Уже были ({len(skipped)}):\n" + "\n".join(skipped))
        if failed: report.append(f"❌ Не удалось ({len(failed)}):\n" + "\n".join(failed))
        await update.message.reply_text("\n\n".join(report) if report else "Ничего не изменилось.")

async def cmd_removeblogger(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /removeblogger @username"); return
    username = context.args[0].strip().lstrip("@")
    bloggers = load_bloggers()
    if username not in bloggers: await update.message.reply_text(f"⚠️ @{username} не найден."); return
    bloggers.remove(username)
    save_bloggers(bloggers)
    await update.message.reply_text(f"✅ @{username} удалён.")

async def cmd_removemanyblogger(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /removemanyblogger @user1 @user2 ..."); return
    raw_input = " ".join(context.args)
    mentions = re.findall(r'@(\w+)', raw_input)
    links = re.findall(r'https?://(?:x\.com|twitter\.com)/(\w+)', raw_input)
    usernames = list(dict.fromkeys(mentions + links))
    if not usernames: await update.message.reply_text("❌ Не удалось распознать username."); return

    bloggers = load_bloggers()
    removed, not_found = [], []
    for username in usernames:
        if username in bloggers:
            bloggers.remove(username)
            removed.append(f"• @{username}")
        else:
            not_found.append(f"• @{username}")

    save_bloggers(bloggers)
    report = []
    if removed: report.append(f"✅ Удалены ({len(removed)}):\n" + "\n".join(removed))
    if not_found: report.append(f"⚠️ Не найдены ({len(not_found)}):\n" + "\n".join(not_found))
    await update.message.reply_text("\n\n".join(report) if report else "Ничего не изменилось.")

async def cmd_listbloggers(update, context):
    if not is_admin(update.effective_user.id): return
    bloggers = load_bloggers()
    keywords = load_keywords()
    kw_msg = f"🔑 Слов: {len(keywords)}" if keywords else "⚠️ Слов нет"
    if not bloggers: await update.message.reply_text(f"📋 Блогеры: пусто.\n{kw_msg}"); return
    await update.message.reply_text(f"📋 Блогеры:\n" + "\n".join([f"• @{b}" for b in bloggers]) + f"\n\n{kw_msg}")

async def cmd_addword(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addword слово"); return
    word = " ".join(context.args).strip()
    keywords = load_keywords()
    if word.lower() in [k.lower() for k in keywords]:
        await update.message.reply_text(f"⚠️ '{word}' уже в списке."); return
    keywords.append(word)
    save_keywords(keywords)
    await update.message.reply_text(f"✅ '{word}' добавлен. Всего: {len(keywords)}")

async def cmd_addwords(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addwords слово1, слово2, фраза ..."); return
    raw_input = " ".join(context.args)
    words = re.split(r'[,\n]+', raw_input)
    words = [w.strip() for w in words if w.strip()]
    if not words: await update.message.reply_text("❌ Не удалось распознать слова."); return
    keywords = load_keywords()
    added, skipped = [], []
    for word in words:
        if word.lower() in [k.lower() for k in keywords]:
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
    if not context.args: await update.message.reply_text("❌ /removeword слово"); return
    word = " ".join(context.args).strip()
    keywords = load_keywords()
    found = None
    for kw in keywords:
        if kw.lower() == word.lower():
            found = kw
            break
    if not found: await update.message.reply_text(f"⚠️ '{word}' не найден."); return
    keywords.remove(found)
    save_keywords(keywords)
    await update.message.reply_text(f"✅ '{found}' удалён. Всего: {len(keywords)}")

async def cmd_removemanywords(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /removemanywords слово1, слово2, ..."); return
    raw_input = " ".join(context.args)
    words = re.split(r'[,\n]+', raw_input)
    words = [w.strip() for w in words if w.strip()]
    if not words: await update.message.reply_text("❌ Не удалось распознать слова."); return

    keywords = load_keywords()
    removed, not_found = [], []
    for word in words:
        found = None
        for kw in keywords:
            if kw.lower() == word.lower():
                found = kw
                break
        if found:
            keywords.remove(found)
            removed.append(found)
        else:
            not_found.append(word)

    save_keywords(keywords)
    report = []
    if removed: report.append(f"✅ Удалены ({len(removed)}): {', '.join(removed)}")
    if not_found: report.append(f"⚠️ Не найдены ({len(not_found)}): {', '.join(not_found)}")
    await update.message.reply_text("\n\n".join(report) + f"\n\n🔑 Всего: {len(keywords)}")

async def cmd_listwords(update, context):
    if not is_admin(update.effective_user.id): return
    keywords = load_keywords()
    if not keywords: await update.message.reply_text("🔑 Ключевых слов нет."); return
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
    images = tweet["images"]
    videos = tweet["videos"]
    display_name = tweet["display_name"]
    post_link = tweet["link"]

    # Экранируем HTML в тексте, чтобы не сломать форматирование
    safe_text = escape_html(text)
    # Подпись с жирным автором
    signature = f"\n\n<b>{display_name}</b> | https://x.com/{username}\n\n🔗 Post link: {post_link}"

    try:
        if videos:
            video_link = videos[0]
            full_text = f"{safe_text}\n\n🎬 Video: {video_link}{signature}"
            await bot.send_message(TELEGRAM_CHANNEL_ID, full_text[:4096], parse_mode='HTML', disable_web_page_preview=True)
        elif images:
            full_text = safe_text + signature
            if len(images) == 1:
                await bot.send_photo(TELEGRAM_CHANNEL_ID, images[0], caption=full_text[:1024], parse_mode='HTML')
            else:
                media = []
                for i, img in enumerate(images[:10]):
                    if i == 0:
                        media.append(InputMediaPhoto(media=img, caption=full_text[:1024], parse_mode='HTML'))
                    else:
                        media.append(InputMediaPhoto(media=img))
                await bot.send_media_group(TELEGRAM_CHANNEL_ID, media)
        else:
            full_text = safe_text + signature
            await bot.send_message(TELEGRAM_CHANNEL_ID, full_text, parse_mode='HTML', disable_web_page_preview=True)
    except TelegramError as e:
        logger.error(f"Ошибка отправки: {e}")
        try:
            # Fallback без HTML-форматирования
            fallback = text + f"\n\n{display_name} | https://x.com/{username}\n\n🔗 Post link: {post_link}"
            await bot.send_message(TELEGRAM_CHANNEL_ID, fallback[:4096], disable_web_page_preview=True)
        except:
            pass

async def check_and_post(bot: Bot):
    global sent_posts_cache
    async with adding_lock:
        pass

    fans = load_fans()
    bloggers = load_bloggers()
    all_usernames = list(set(fans + bloggers))

    if not all_usernames:
        return 0

    if not sent_posts_cache:
        sent_posts_cache = load_sent_posts()

    logger.info(f"🔄 Параллельная проверка {len(all_usernames)} каналов (макс {MAX_PARALLEL} одновременно)...")
    start_time = time.time()
    all_tweets = await fetch_all_tweets(all_usernames)
    elapsed = time.time() - start_time
    logger.info(f"⏱ Проверка заняла {elapsed:.1f} сек, получено {len(all_tweets)} твитов")

    all_tweets.sort(key=extract_tweet_id)

    keywords = load_keywords()
    new_posts = 0

    for tweet in all_tweets:
        link = tweet["link"]
        if link in sent_posts_cache:
            continue

        username = tweet["username"]

        if is_tweet_too_old(tweet):
            continue

        if username in bloggers and username not in fans:
            if not post_matches_filter(tweet["text"], keywords):
                continue

        await send_post(bot, tweet, username)
        save_sent_post(link)
        new_posts += 1
        await asyncio.sleep(2)

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
        await asyncio.sleep(30)

async def main():
    global sent_posts_cache
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("❌ Нет BOT_TOKEN!")
        return

    boot_time = get_boot_time()
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
    app.add_handler(CommandHandler("removemanyblogger", cmd_removemanyblogger))
    app.add_handler(CommandHandler("listbloggers", cmd_listbloggers))
    app.add_handler(CommandHandler("addword", cmd_addword))
    app.add_handler(CommandHandler("addwords", cmd_addwords))
    app.add_handler(CommandHandler("removeword", cmd_removeword))
    app.add_handler(CommandHandler("removemanywords", cmd_removemanywords))
    app.add_handler(CommandHandler("listwords", cmd_listwords))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("force", cmd_force))
    asyncio.create_task(scheduled_check(bot))
    logger.info(f"🤖 Бот запущен (время старта: {boot_time.isoformat()})")
    logger.info("📌 Твиты старше этого времени не будут отправлены")
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
