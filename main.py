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
from telegram.ext import Application, CommandHandler

# =========================
# ЛОГИ
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# =========================
# НАСТРОЙКИ
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")

TELEGRAM_CHANNEL_ID = -1003857194781
ADMIN_ID = 417850992

RSSHUB_URL = "https://chelsea-rss-bridge.onrender.com"

CHECK_INTERVAL = 10
MAX_CONCURRENT_REQUESTS = 20

SENT_POSTS_FILE = "sent_posts.txt"
FANS_FILE = "chelsea_fans.txt"
BLOGGERS_FILE = "general_bloggers.txt"
KEYWORDS_FILE = "keywords.txt"

# =========================
# ГЛОБАЛЬНОЕ
# =========================
sent_posts_cache = set()

adding_lock = asyncio.Lock()

# Один общий HTTP клиент
client = httpx.AsyncClient(
    timeout=httpx.Timeout(20.0, connect=10.0),
    follow_redirects=True,
    http2=True,
    headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/rss+xml, */*"
    }
)

# Ограничение параллельности
semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# =========================
# ФАЙЛЫ
# =========================
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
        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
            sent_posts_cache = set(line.strip() for line in f)
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


# =========================
# УТИЛИТЫ
# =========================
def is_admin(user_id):
    return user_id == ADMIN_ID


def post_matches_filter(text, keywords):
    if not keywords:
        return True

    text_lower = text.lower()

    for kw in keywords:
        if kw.lower() in text_lower:
            return True

    return False


def clean_html(text):
    text = re.sub(r"<[^>]+>", "", text)

    text = (
        text
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )

    text = re.sub(r"\n\s*\n", "\n\n", text)

    return text.strip()


def extract_text_and_media(entry):
    images = []
    videos = []
    text = ""

    description = getattr(entry, "description", "") or getattr(entry, "summary", "")

    if description:
        clean_desc = re.split(
            r'<hr[^>]*>|<div class="rsshub-quote">',
            description
        )[0]

        text_with_breaks = re.sub(r"<br\s*/?>", "\n", clean_desc)

        text = clean_html(text_with_breaks)

        img_urls = re.findall(r'<img[^>]+src="([^"]+)"', clean_desc)

        for url in img_urls:
            url = url.replace("&amp;", "&")

            if "pbs.twimg.com" in url and url not in images:
                images.append(url)

        video_urls = re.findall(r'<video[^>]+src="([^"]+)"', clean_desc)

        for url in video_urls:
            url = url.replace("&amp;", "&")

            if url not in videos:
                videos.append(url)

    if not text:
        title = getattr(entry, "title", "")
        text = clean_html(title)

    return text, images, videos


# =========================
# TWITTER FETCH
# =========================
async def fetch_tweets(username):
    async with semaphore:

        url = f"{RSSHUB_URL}/twitter/user/{username}"

        for attempt in range(3):

            try:
                response = await client.get(url)

                if response.status_code == 503:

                    wait_time = 2 * (attempt + 1)

                    logger.warning(
                        f"503 @{username} | retry {attempt+1}/3 | wait {wait_time}s"
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

                    tweet_id_match = re.search(r"/status/(\d+)", link)

                    tweet_id = (
                        tweet_id_match.group(1)
                        if tweet_id_match
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


# =========================
# TELEGRAM
# =========================
async def send_post(bot: Bot, tweet):

    text = tweet["text"]

    username = tweet["username"]

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

            # Только первая картинка = быстрее
            await bot.send_photo(
                TELEGRAM_CHANNEL_ID,
                images[0],
                caption=full_text[:1024]
            )

        else:

            await bot.send_message(
                TELEGRAM_CHANNEL_ID,
                full_text,
                disable_web_page_preview=True
            )

        return True

    except TelegramError as e:

        logger.error(f"TG ошибка: {e}")

        try:

            await bot.send_message(
                TELEGRAM_CHANNEL_ID,
                full_text[:4096],
                disable_web_page_preview=True
            )

            return True

        except Exception:
            return False


# =========================
# ОСНОВНАЯ ПРОВЕРКА
# =========================
async def check_and_post(bot: Bot):

    fans = load_fans()
    bloggers = load_bloggers()

    all_usernames = list(set(fans + bloggers))

    if not all_usernames:
        return 0

    keywords = load_keywords()

    logger.info(f"Проверка {len(all_usernames)} аккаунтов")

    start = time.time()

    all_tweets = await fetch_all_tweets(all_usernames)

    elapsed = round(time.time() - start, 1)

    logger.info(
        f"Получено {len(all_tweets)} твитов за {elapsed} сек"
    )

    # Новые сверху
    all_tweets.sort(
        key=lambda x: int(x["tweet_id"]) if str(x["tweet_id"]).isdigit() else 0,
        reverse=True
    )

    new_posts = 0

    for tweet in reversed(all_tweets):

        tweet_id = tweet["tweet_id"]

        if tweet_id in sent_posts_cache:
            continue

        username = tweet["username"]

        # Фильтр блогеров
        if username in bloggers and username not in fans:

            if not post_matches_filter(tweet["text"], keywords):
                continue

        success = await send_post(bot, tweet)

        if success:

            save_sent_post(tweet_id)

            new_posts += 1

            logger.info(
                f"Отправлен @{username} | {tweet_id}"
            )

            # маленький антифлуд
            await asyncio.sleep(0.15)

    if new_posts:
        logger.info(f"Новых постов: {new_posts}")

    return new_posts


# =========================
# LOOP
# =========================
async def scheduled_check(bot):

    await asyncio.sleep(5)

    while True:

        try:

            await check_and_post(bot)

        except Exception as e:

            logger.error(f"Loop ошибка: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


# =========================
# COMMANDS
# =========================
async def cmd_status(update, context):

    if not is_admin(update.effective_user.id):
        return

    fans = load_fans()
    bloggers = load_bloggers()
    keywords = load_keywords()

    await update.message.reply_text(
        f"✅ Бот работает\n\n"
        f"🔵 Фан: {len(fans)}\n"
        f"🟡 Блогеры: {len(bloggers)}\n"
        f"🔑 Слова: {len(keywords)}\n"
        f"📤 Sent: {len(sent_posts_cache)}"
    )


async def cmd_force(update, context):

    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text("🔄 Проверяю...")

    count = await check_and_post(context.bot)

    await update.message.reply_text(
        f"✅ Отправлено: {count}"
    )


# =========================
# MAIN
# =========================
async def main():

    global sent_posts_cache

    if not TELEGRAM_BOT_TOKEN:

        logger.critical("НЕТ BOT_TOKEN")
        return

    sent_posts_cache = load_sent_posts()

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    app = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("force", cmd_force))

    asyncio.create_task(
        scheduled_check(bot)
    )

    logger.info("БОТ ЗАПУЩЕН")

    await app.run_polling()


# =========================
# START
# =========================
if __name__ == "__main__":

    nest_asyncio.apply()

    try:

        loop = asyncio.get_running_loop()

        loop.create_task(main())

        while True:
            time.sleep(3600)

    except RuntimeError:

        asyncio.run(main())
