import asyncio
import logging
import os
import re
import time

import feedparser
import httpx
import nest_asyncio

from telegram import (
    Bot,
    InputMediaPhoto
)

from telegram.error import TelegramError

from telegram.ext import (
    Application,
    CommandHandler
)

# =========================================================
# LOGGING
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# =========================================================
# CONFIG
# =========================================================
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")

TELEGRAM_CHANNEL_ID = -1003857194781
ADMIN_ID = 417850992

RSSHUB_URL = "https://chelsea-rss-bridge.onrender.com"

CHECK_INTERVAL = 10
MAX_CONCURRENT_REQUESTS = 25

SENT_POSTS_FILE = "sent_posts.txt"

FANS_FILE = "chelsea_fans.txt"
BLOGGERS_FILE = "general_bloggers.txt"
KEYWORDS_FILE = "keywords.txt"

# =========================================================
# GLOBAL
# =========================================================
sent_posts_cache = set()

adding_lock = asyncio.Lock()

semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# =========================================================
# GLOBAL HTTP CLIENT
# =========================================================
client = httpx.AsyncClient(
    timeout=httpx.Timeout(20.0, connect=10.0),
    follow_redirects=True,
    http2=True,
    headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/rss+xml, */*"
    }
)

# =========================================================
# FILES
# =========================================================
def load_list(filename):

    try:

        with open(filename, "r", encoding="utf-8") as f:

            return [
                line.strip()
                for line in f
                if line.strip()
            ]

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

        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:

            sent_posts_cache = set(
                line.strip()
                for line in f
            )

    except FileNotFoundError:

        sent_posts_cache = set()

    return sent_posts_cache


def save_sent_post(post_id):

    global sent_posts_cache

    if post_id in sent_posts_cache:
        return

    sent_posts_cache.add(post_id)

    with open(SENT_POSTS_FILE, "a", encoding="utf-8") as f:

        f.write(post_id + "\n")


# =========================================================
# UTILS
# =========================================================
def is_admin(user_id):
    return user_id == ADMIN_ID


def post_matches_filter(text, keywords):

    if not keywords:
        return True

    text = text.lower()

    for kw in keywords:

        if kw.lower() in text:
            return True

    return False


def clean_html(text):

    text = re.sub(r"<[^>]+>", "", text)

    text = (
        text
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )

    text = re.sub(r"\n\s*\n", "\n\n", text)

    return text.strip()


def extract_text_and_media(entry):

    images = []
    videos = []

    text = ""

    description = (
        getattr(entry, "description", "")
        or getattr(entry, "summary", "")
    )

    if description:

        clean_desc = re.split(
            r'<hr[^>]*>|<div class="rsshub-quote">',
            description
        )[0]

        text_with_breaks = re.sub(
            r"<br\s*/?>",
            "\n",
            clean_desc
        )

        text = clean_html(text_with_breaks)

        img_urls = re.findall(
            r'<img[^>]+src="([^"]+)"',
            clean_desc
        )

        for url in img_urls:

            url = url.replace("&amp;", "&")

            if "pbs.twimg.com" in url:

                if url not in images:
                    images.append(url)

        video_urls = re.findall(
            r'<video[^>]+src="([^"]+)"',
            clean_desc
        )

        for url in video_urls:

            url = url.replace("&amp;", "&")

            if url not in videos:
                videos.append(url)

    if not text:

        title = getattr(entry, "title", "")

        text = clean_html(title)

    return text, images, videos


# =========================================================
# FETCH
# =========================================================
async def fetch_tweets(username):

    async with semaphore:

        url = f"{RSSHUB_URL}/twitter/user/{username}"

        for attempt in range(3):

            try:

                response = await client.get(url)

                if response.status_code == 503:

                    wait_time = 2 * (attempt + 1)

                    logger.warning(
                        f"503 @{username} retry {attempt+1}/3"
                    )

                    await asyncio.sleep(wait_time)

                    continue

                response.raise_for_status()

                feed = feedparser.parse(response.text)

                display_name = username

                if hasattr(feed.feed, "title"):

                    display_name = (
                        feed.feed.title
                        .replace("Twitter @", "")
                        .strip()
                    )

                tweets = []

                for entry in feed.entries:

                    text, images, videos = extract_text_and_media(entry)

                    link = getattr(entry, "link", "")

                    if not link:
                        continue

                    match = re.search(
                        r"/status/(\d+)",
                        link
                    )

                    tweet_id = (
                        match.group(1)
                        if match
                        else link
                    )

                    tweets.append({
                        "tweet_id": tweet_id,
                        "text": text,
                        "link": link,
                        "images": images,
                        "videos": videos,
                        "display_name": display_name,
                        "username": username
                    })

                return tweets

            except Exception as e:

                logger.error(f"Ошибка @{username}: {e}")

                if attempt < 2:
                    await asyncio.sleep(2)

        return []


async def fetch_all_tweets(usernames):

    tasks = [
        fetch_tweets(username)
        for username in usernames
    ]

    results = await asyncio.gather(
        *tasks,
        return_exceptions=True
    )

    all_tweets = []

    for result in results:

        if isinstance(result, Exception):

            logger.error(result)

            continue

        if result:
            all_tweets.extend(result)

    return all_tweets


# =========================================================
# TELEGRAM SEND
# =========================================================
async def send_post(bot: Bot, tweet):

    text = tweet["text"]

    username = tweet["username"]

    signature = (
        f"\n\n{tweet['display_name']} | "
        f"https://x.com/{username}"
    )

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

            # если хочешь супер скорость —
            # отправляем только 1 фото

            await bot.send_photo(
                TELEGRAM_CHANNEL_ID,
                images[0],
                caption=full_text[:1024]
            )

        else:

            await bot.send_message(
                TELEGRAM_CHANNEL_ID,
                full_text[:4096],
                disable_web_page_preview=True
            )

        return True

    except TelegramError as e:

        logger.error(f"Telegram error: {e}")

        return False


# =========================================================
# CORE
# =========================================================
async def check_and_post(bot: Bot):

    fans = load_fans()
    bloggers = load_bloggers()

    all_usernames = list(
        set(fans + bloggers)
    )

    if not all_usernames:
        return 0

    keywords = load_keywords()

    logger.info(
        f"Проверка {len(all_usernames)} аккаунтов"
    )

    start = time.time()

    all_tweets = await fetch_all_tweets(
        all_usernames
    )

    elapsed = round(
        time.time() - start,
        1
    )

    logger.info(
        f"Получено {len(all_tweets)} твитов "
        f"за {elapsed} сек"
    )

    all_tweets.sort(
        key=lambda x: int(x["tweet_id"])
        if str(x["tweet_id"]).isdigit()
        else 0
    )

    new_posts = 0

    for tweet in all_tweets:

        tweet_id = tweet["tweet_id"]

        if tweet_id in sent_posts_cache:
            continue

        username = tweet["username"]

        # blogger filter
        if username in bloggers and username not in fans:

            if not post_matches_filter(
                tweet["text"],
                keywords
            ):
                continue

        success = await send_post(bot, tweet)

        if success:

            save_sent_post(tweet_id)

            new_posts += 1

            logger.info(
                f"Отправлен @{username}"
            )

            await asyncio.sleep(0.1)

    if new_posts:

        logger.info(
            f"Новых постов: {new_posts}"
        )

    return new_posts


async def scheduled_check(bot):

    await asyncio.sleep(5)

    while True:

        try:

            await check_and_post(bot)

        except Exception as e:

            logger.error(f"Loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


# =========================================================
# COMMANDS
# =========================================================
async def cmd_start(update, context):

    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "🤖 Бот работает\n\n"
        "📋 Фан:\n"
        "/addfan @username\n"
        "/addmanyfan\n"
        "/removefan @username\n"
        "/listfan\n\n"
        "📋 Блогеры:\n"
        "/addblogger @username\n"
        "/addmanyblogger\n"
        "/removeblogger @username\n"
        "/listbloggers\n\n"
        "🔑 Слова:\n"
        "/addword слово\n"
        "/addwords\n"
        "/removeword слово\n"
        "/listwords\n\n"
        "/status\n"
        "/force"
    )


# =========================================================
# FAN COMMANDS
# =========================================================
async def cmd_addfan(update, context):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:

        await update.message.reply_text(
            "❌ /addfan @username"
        )

        return

    username = (
        context.args[0]
        .strip()
        .lstrip("@")
    )

    fans = load_fans()

    if username in fans:

        await update.message.reply_text(
            f"⚠️ @{username} уже есть"
        )

        return

    tweets = await fetch_tweets(username)

    fans.append(username)

    save_fans(fans)

    for tweet in tweets:
        save_sent_post(tweet["tweet_id"])

    await update.message.reply_text(
        f"✅ @{username} добавлен"
    )


async def cmd_addmanyfan(update, context):

    if not is_admin(update.effective_user.id):
        return

    text = update.message.text

    usernames = set()

    mentions = re.findall(
        r'@([A-Za-z0-9_]+)',
        text
    )

    links = re.findall(
        r'https?://(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)',
        text,
        flags=re.IGNORECASE
    )

    for username in mentions + links:

        username = username.strip()

        if username:
            usernames.add(username)

    usernames = list(usernames)

    if not usernames:

        await update.message.reply_text(
            "❌ Не удалось распознать usernames"
        )

        return

    fans = load_fans()

    added = []
    skipped = []

    await update.message.reply_text(
        f"⏳ Добавляю {len(usernames)} аккаунтов..."
    )

    for username in usernames:

        if username in fans:

            skipped.append(username)

            continue

        try:

            tweets = await fetch_tweets(username)

            fans.append(username)

            save_fans(fans)

            for tweet in tweets:
                save_sent_post(tweet["tweet_id"])

            added.append(username)

        except Exception as e:

            logger.error(e)

        await asyncio.sleep(0.1)

    msg = ""

    if added:

        msg += (
            f"✅ Добавлены ({len(added)}):\n"
            + "\n".join(
                f"• @{u}"
                for u in added
            )
        )

    if skipped:

        msg += (
            f"\n\n⚠️ Уже были:\n"
            + "\n".join(
                f"• @{u}"
                for u in skipped
            )
        )

    await update.message.reply_text(msg)


async def cmd_removefan(update, context):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        return

    username = (
        context.args[0]
        .strip()
        .lstrip("@")
    )

    fans = load_fans()

    if username not in fans:

        await update.message.reply_text(
            "❌ Не найден"
        )

        return

    fans.remove(username)

    save_fans(fans)

    await update.message.reply_text(
        f"✅ @{username} удалён"
    )


async def cmd_listfan(update, context):

    if not is_admin(update.effective_user.id):
        return

    fans = load_fans()

    if not fans:

        await update.message.reply_text(
            "Пусто"
        )

        return

    await update.message.reply_text(
        "📋 Фан аккаунты:\n\n"
        + "\n".join(
            f"• @{f}"
            for f in fans
        )
    )


# =========================================================
# BLOGGERS
# =========================================================
async def cmd_addblogger(update, context):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        return

    username = (
        context.args[0]
        .strip()
        .lstrip("@")
    )

    bloggers = load_bloggers()

    if username in bloggers:

        await update.message.reply_text(
            "⚠️ Уже есть"
        )

        return

    tweets = await fetch_tweets(username)

    bloggers.append(username)

    save_bloggers(bloggers)

    for tweet in tweets:
        save_sent_post(tweet["tweet_id"])

    await update.message.reply_text(
        f"✅ @{username} добавлен"
    )


async def cmd_addmanyblogger(update, context):

    if not is_admin(update.effective_user.id):
        return

    text = update.message.text

    usernames = set()

    mentions = re.findall(
        r'@([A-Za-z0-9_]+)',
        text
    )

    links = re.findall(
        r'https?://(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)',
        text,
        flags=re.IGNORECASE
    )

    for username in mentions + links:

        username = username.strip()

        if username:
            usernames.add(username)

    usernames = list(usernames)

    bloggers = load_bloggers()

    added = []

    for username in usernames:

        if username in bloggers:
            continue

        tweets = await fetch_tweets(username)

        bloggers.append(username)

        save_bloggers(bloggers)

        for tweet in tweets:
            save_sent_post(tweet["tweet_id"])

        added.append(username)

        await asyncio.sleep(0.1)

    await update.message.reply_text(
        f"✅ Добавлено {len(added)}"
    )


async def cmd_removeblogger(update, context):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        return

    username = (
        context.args[0]
        .strip()
        .lstrip("@")
    )

    bloggers = load_bloggers()

    if username not in bloggers:
        return

    bloggers.remove(username)

    save_bloggers(bloggers)

    await update.message.reply_text(
        f"✅ @{username} удалён"
    )


async def cmd_listbloggers(update, context):

    if not is_admin(update.effective_user.id):
        return

    bloggers = load_bloggers()

    if not bloggers:

        await update.message.reply_text(
            "Пусто"
        )

        return

    await update.message.reply_text(
        "📋 Блогеры:\n\n"
        + "\n".join(
            f"• @{b}"
            for b in bloggers
        )
    )


# =========================================================
# KEYWORDS
# =========================================================
async def cmd_addword(update, context):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        return

    word = context.args[0].lower()

    keywords = load_keywords()

    if word in keywords:
        return

    keywords.append(word)

    save_keywords(keywords)

    await update.message.reply_text(
        f"✅ '{word}' добавлено"
    )


async def cmd_addwords(update, context):

    if not is_admin(update.effective_user.id):
        return

    text = " ".join(context.args)

    words = re.split(
        r"[,\s;\n]+",
        text
    )

    words = [
        w.strip().lower()
        for w in words
        if w.strip()
    ]

    keywords = load_keywords()

    added = []

    for word in words:

        if word not in keywords:

            keywords.append(word)

            added.append(word)

    save_keywords(keywords)

    await update.message.reply_text(
        f"✅ Добавлено {len(added)}"
    )


async def cmd_removeword(update, context):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        return

    word = context.args[0].lower()

    keywords = load_keywords()

    if word not in keywords:
        return

    keywords.remove(word)

    save_keywords(keywords)

    await update.message.reply_text(
        f"✅ '{word}' удалено"
    )


async def cmd_listwords(update, context):

    if not is_admin(update.effective_user.id):
        return

    keywords = load_keywords()

    if not keywords:

        await update.message.reply_text(
            "Слов нет"
        )

        return

    await update.message.reply_text(
        "🔑 Ключевые слова:\n\n"
        + ", ".join(keywords)
    )


# =========================================================
# STATUS
# =========================================================
async def cmd_status(update, context):

    if not is_admin(update.effective_user.id):
        return

    fans = load_fans()
    bloggers = load_bloggers()
    keywords = load_keywords()

    await update.message.reply_text(
        f"✅ ONLINE\n\n"
        f"🔵 Fans: {len(fans)}\n"
        f"🟡 Bloggers: {len(bloggers)}\n"
        f"🔑 Words: {len(keywords)}\n"
        f"📤 Sent: {len(sent_posts_cache)}"
    )


async def cmd_force(update, context):

    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "🔄 Проверка..."
    )

    count = await check_and_post(
        context.bot
    )

    await update.message.reply_text(
        f"✅ Отправлено: {count}"
    )


# =========================================================
# MAIN
# =========================================================
async def main():

    global sent_posts_cache

    if not TELEGRAM_BOT_TOKEN:

        logger.critical(
            "❌ Нет BOT_TOKEN"
        )

        return

    sent_posts_cache = load_sent_posts()

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN
    )

    app = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))

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

    asyncio.create_task(
        scheduled_check(bot)
    )

    logger.info(
        "🤖 БОТ ЗАПУЩЕН"
    )

    await app.run_polling()


# =========================================================
# START
# =========================================================
if __name__ == "__main__":

    nest_asyncio.apply()

    asyncio.run(main())
