import asyncio
import os
import logging
import re
import time
import random

import httpx
import nest_asyncio
from bs4 import BeautifulSoup
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

SENT_POSTS_FILE = "sent_posts.txt"
FANS_FILE = "chelsea_fans.txt"
BLOGGERS_FILE = "general_bloggers.txt"
KEYWORDS_FILE = "keywords.txt"

# xcancel.com первым — он работал раньше
MIRRORS = [
    "https://xcancel.com",
    "https://nitter.net",
]

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
    try:
        with open(SENT_POSTS_FILE, "r") as f:
            return set(line.strip() for line in f)
    except FileNotFoundError:
        return set()

def save_sent_post(post_id):
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

# --- ОЧИСТКА КАРТИНОК ---
def clean_images(images):
    cleaned = []
    for img in images:
        if "pbs.twimg.com" in img or "video.twimg.com" in img:
            if img not in cleaned:
                cleaned.append(img)
    return cleaned

# --- ПАРСИНГ ТВИТОВ ---
def extract_video_url(div, mirror):
    video_tag = div.select_one("video")
    if video_tag:
        src = video_tag.get("src", "")
        if src:
            if src.startswith("/"):
                src = f"{mirror}{src}"
            return src
        source_tag = video_tag.select_one("source")
        if source_tag:
            src = source_tag.get("src", "")
            if src and src.startswith("/"):
                src = f"{mirror}{src}"
            return src
    for att in div.select("div.attachment, div.attachments"):
        vid = att.select_one("video")
        if vid:
            src = vid.get("src", "")
            if src:
                if src.startswith("/"):
                    src = f"{mirror}{src}"
                return src
            source_tag = vid.select_one("source")
            if source_tag:
                src = source_tag.get("src", "")
                if src and src.startswith("/"):
                    src = f"{mirror}{src}"
                return src
    return None

def extract_quote_text(div):
    quote_div = div.select_one("div.quote-tweet, div.quoted-tweet, div.tweet-quote")
    if not quote_div:
        return None
    quote_author = None
    quote_text = None
    author_tag = quote_div.select_one("a.username, span.username, div.tweet-header span")
    if author_tag:
        quote_author = author_tag.get_text(strip=True)
    if not quote_author:
        author_tag = quote_div.select_one("a[href*='status']")
        if author_tag:
            quote_author = author_tag.get_text(strip=True)
            if not quote_author:
                href = author_tag.get("href", "")
                match = re.search(r'/(\w+)/status/', href)
                if match:
                    quote_author = f"@{match.group(1)}"
    text_tag = quote_div.select_one("div.tweet-content, div.quote-content, div.quoted-content")
    if text_tag:
        quote_text = text_tag.get_text(strip=True)
    if quote_text:
        return {"author": quote_author if quote_author else "Оригинальный пост", "text": quote_text}
    return None

async def try_fetch_from_mirror(mirror, username):
    url = f"{mirror}/{username}"
    tweets = []
    display_name = username

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
        response = await client.get(url, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")

        profile_name_tag = soup.select_one("a.profile-card-fullname")
        if profile_name_tag:
            display_name = profile_name_tag.get_text(strip=True)

        tweet_divs = soup.select("div.timeline-item")

        for div in tweet_divs:
            content_div = div.select_one("div.tweet-content")
            if not content_div:
                continue
            text = content_div.get_text(strip=True)

            link_tag = div.select_one("a.tweet-link")
            if not link_tag:
                continue
            link = link_tag.get("href", "")
            if link and not link.startswith("http"):
                link = f"{mirror}{link}"
            link = re.sub(r'https?://[^/]+/', 'https://x.com/', link)

            images = []
            for att in div.select("div.attachment, div.attachments"):
                for img in att.select("img"):
                    src = img.get("src", "")
                    if src and not src.startswith("data:"):
                        if src.startswith("/"):
                            src = f"{mirror}{src}"
                        images.append(src)

            images = clean_images(images)
            video_url = extract_video_url(div, mirror)
            quote = extract_quote_text(div)

            tweets.append({
                "text": text,
                "link": link,
                "images": images,
                "video": video_url,
                "quote": quote,
                "display_name": display_name,
                "username": username
            })

    return display_name, tweets

async def fetch_tweets(username):
    mirrors = MIRRORS.copy()
    random.shuffle(mirrors)
    for mirror in mirrors:
        try:
            display_name, tweets = await try_fetch_from_mirror(mirror, username)
            if tweets:
                logger.info(f"✅ @{username}: {len(tweets)} твитов через {mirror}")
                return display_name, tweets
        except httpx.HTTPStatusError as e:
            logger.warning(f"⚠️ {mirror} -> {e.response.status_code} для @{username}")
        except Exception as e:
            logger.warning(f"⚠️ {mirror} -> {type(e).__name__}: {e} для @{username}")
    logger.error(f"❌ @{username}: все зеркала недоступны")
    return username, []

async def mark_all_current_as_sent(username):
    for attempt in range(3):
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
            logger.info(f"📌 @{username}: {count} постов отмечено (попытка {attempt+1})")
            return count
        await asyncio.sleep(3)
    logger.warning(f"⚠️ @{username}: не удалось загрузить твиты после 3 попыток")
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
            await asyncio.sleep(0.5)
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
            await asyncio.sleep(0.5)
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
    sent = len(load_sent_posts())
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
    quote = tweet.get("quote")
    if quote:
        text += f"\n\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n{quote['author']}:\n{quote['text']}\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
    signature = f"\n\n{tweet['display_name']} | https://x.com/{username}"
    full_text = text + signature
    images = tweet["images"]
    video_url = tweet.get("video")

    try:
        if video_url:
            await bot.send_video(TELEGRAM_CHANNEL_ID, video_url, caption=full_text[:1024], supports_streaming=True)
        elif images:
            if len(images) == 1:
                await bot.send_photo(TELEGRAM_CHANNEL_ID, images[0], caption=full_text[:1024])
            else:
                media = []
                for i, img in enumerate(images[:10]):
                    if i == 0:
                        media.append(InputMediaPhoto(media=img, caption=full_text[:1024]))
                    else:
                        media.append(InputMediaPhoto(media=img))
                await bot.send_media_group(TELEGRAM_CHANNEL_ID, media)
        else:
            await bot.send_message(TELEGRAM_CHANNEL_ID, full_text)
    except TelegramError as e:
        logger.error(f"Ошибка отправки: {e}")
        try:
            await bot.send_message(TELEGRAM_CHANNEL_ID, full_text)
        except:
            pass

async def check_and_post(bot: Bot):
    async with adding_lock:
        pass
    fans = load_fans()
    bloggers = load_bloggers()
    keywords = load_keywords()
    sent_posts = load_sent_posts()
    new_posts = 0

    for username in fans:
        try:
            _, tweets = await fetch_tweets(username)
        except:
            continue
        for tweet in tweets:
            if tweet["link"] in sent_posts:
                continue
            await send_post(bot, tweet, username)
            save_sent_post(tweet["link"])
            sent_posts.add(tweet["link"])
            new_posts += 1
            await asyncio.sleep(3)

    for username in bloggers:
        try:
            _, tweets = await fetch_tweets(username)
        except:
            continue
        for tweet in tweets:
            if tweet["link"] in sent_posts:
                continue
            check_text = tweet["text"]
            if tweet.get("quote"):
                check_text += " " + tweet["quote"]["text"]
            if not post_matches_filter(check_text, keywords):
                continue
            await send_post(bot, tweet, username)
            save_sent_post(tweet["link"])
            sent_posts.add(tweet["link"])
            new_posts += 1
            await asyncio.sleep(3)

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
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("❌ Нет BOT_TOKEN!")
        return
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
    logger.info("🤖 Бот запущен")
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
