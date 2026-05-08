import asyncio
import os
import logging
import random
import re
import json
import time

import feedparser
import httpx
import nest_asyncio

from bs4 import BeautifulSoup

from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
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

BOT_TOKEN = os.getenv("BOT_TOKEN")

TELEGRAM_CHANNEL_ID = -1003857194781
ADMIN_ID = 417850992

CHECK_INTERVAL = 45
SEND_DELAY = 1

CACHE_DIR = "cache"

SENT_POSTS_FILE = "sent_posts.txt"

FANS_FILE = "chelsea_fans.txt"
BLOGGERS_FILE = "general_bloggers.txt"
KEYWORDS_FILE = "keywords.txt"

# =========================================================
# MIRRORS
# =========================================================

MIRRORS = [
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.fdn.fr",
    "https://nitter.cz",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://nitter.net",
]

# =========================================================
# USER AGENTS
# =========================================================

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15",
]

# =========================================================
# GLOBALS
# =========================================================

adding_lock = asyncio.Lock()

os.makedirs(CACHE_DIR, exist_ok=True)

# =========================================================
# HELPERS
# =========================================================

def load_list(filename):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return [x.strip() for x in f if x.strip()]
    except FileNotFoundError:
        return []

def save_list(filename, items):
    with open(filename, "w", encoding="utf-8") as f:
        for item in items:
            f.write(item + "\n")

def load_sent_posts():
    try:
        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
            return set(x.strip() for x in f)
    except:
        return set()

def save_sent_post(post_id):
    with open(SENT_POSTS_FILE, "a", encoding="utf-8") as f:
        f.write(post_id + "\n")

def post_matches_filter(text, keywords):
    if not keywords:
        return True

    text = text.lower()

    for kw in keywords:
        if kw.lower() in text:
            return True

    return False

# =========================================================
# CACHE
# =========================================================

def cache_file(username):
    return os.path.join(CACHE_DIR, f"{username}.json")

def save_cache(username, tweets):
    try:
        with open(cache_file(username), "w", encoding="utf-8") as f:
            json.dump(tweets, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Cache save error: {e}")

def load_cache(username):
    try:
        with open(cache_file(username), "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

# =========================================================
# HTTP CLIENT
# =========================================================

def build_client():

    limits = httpx.Limits(
        max_connections=20,
        max_keepalive_connections=10
    )

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
    }

    return httpx.AsyncClient(
        timeout=25,
        follow_redirects=True,
        headers=headers,
        http2=True,
        limits=limits,
    )

# =========================================================
# RSS FETCH
# =========================================================

async def fetch_rss(username):

    mirrors = MIRRORS.copy()
    random.shuffle(mirrors)

    for mirror in mirrors:

        rss_url = f"{mirror}/{username}/rss"

        try:

            async with build_client() as client:

                response = await client.get(rss_url)

                if response.status_code != 200:
                    continue

                feed = feedparser.parse(response.text)

                tweets = []

                for entry in feed.entries[:10]:

                    link = entry.link

                    text = BeautifulSoup(
                        entry.title,
                        "lxml"
                    ).get_text(" ", strip=True)

                    tweets.append({
                        "text": text,
                        "link": link,
                        "images": [],
                        "video": None,
                        "display_name": username,
                    })

                if tweets:
                    logger.info(f"RSS OK @{username} via {mirror}")
                    save_cache(username, tweets)
                    return tweets

        except Exception as e:
            logger.warning(f"RSS FAIL {mirror} @{username}: {e}")

    logger.warning(f"RSS failed @{username}")

    return load_cache(username)

# =========================================================
# HTML FALLBACK
# =========================================================

async def fetch_html(username):

    mirrors = MIRRORS.copy()
    random.shuffle(mirrors)

    for mirror in mirrors:

        url = f"{mirror}/{username}"

        for attempt in range(4):

            try:

                async with build_client() as client:

                    response = await client.get(url)

                    response.raise_for_status()

                    soup = BeautifulSoup(
                        response.text,
                        "lxml"
                    )

                    tweets = []

                    divs = soup.select("div.timeline-item")

                    for div in divs[:10]:

                        content = div.select_one("div.tweet-content")

                        if not content:
                            continue

                        text = content.get_text(
                            " ",
                            strip=True
                        )

                        link_tag = div.select_one("a.tweet-link")

                        if not link_tag:
                            continue

                        link = link_tag.get("href", "")

                        if link.startswith("/"):
                            link = f"https://x.com{link}"

                        images = []

                        for img in div.select("img"):

                            src = img.get("src", "")

                            if "pbs.twimg.com" in src:
                                images.append(src)

                        tweets.append({
                            "text": text,
                            "link": link,
                            "images": list(set(images)),
                            "video": None,
                            "display_name": username,
                        })

                    if tweets:
                        logger.info(f"HTML OK @{username}")
                        save_cache(username, tweets)
                        return tweets

            except Exception as e:

                logger.warning(
                    f"HTML retry {attempt+1} "
                    f"@{username}: {e}"
                )

                await asyncio.sleep(2 ** attempt)

    logger.warning(f"HTML failed @{username}")

    return load_cache(username)

# =========================================================
# FETCH
# =========================================================

async def fetch_tweets(username):

    tweets = await fetch_rss(username)

    if tweets:
        return tweets

    tweets = await fetch_html(username)

    return tweets

# =========================================================
# SEND
# =========================================================

async def send_post(bot, tweet, username):

    text = tweet["text"]

    signature = f"\n\n@{username}"

    full_text = text + signature

    images = tweet.get("images", [])

    try:

        if images:

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

                        media.append(
                            InputMediaPhoto(
                                media=img,
                                caption=full_text[:1024]
                            )
                        )

                    else:

                        media.append(
                            InputMediaPhoto(media=img)
                        )

                await bot.send_media_group(
                    TELEGRAM_CHANNEL_ID,
                    media
                )

        else:

            await bot.send_message(
                TELEGRAM_CHANNEL_ID,
                full_text
            )

    except TelegramError as e:

        logger.error(f"Telegram error: {e}")

# =========================================================
# CHECK
# =========================================================

async def check_and_post(bot):

    fans = load_list(FANS_FILE)
    bloggers = load_list(BLOGGERS_FILE)
    keywords = load_list(KEYWORDS_FILE)

    sent_posts = load_sent_posts()

    posted = 0

    # =========================================
    # FANS
    # =========================================

    for username in fans:

        try:

            tweets = await fetch_tweets(username)

            for tweet in reversed(tweets):

                if tweet["link"] in sent_posts:
                    continue

                await send_post(bot, tweet, username)

                save_sent_post(tweet["link"])

                sent_posts.add(tweet["link"])

                posted += 1

                await asyncio.sleep(SEND_DELAY)

        except Exception as e:

            logger.error(f"Fan error @{username}: {e}")

    # =========================================
    # BLOGGERS
    # =========================================

    for username in bloggers:

        try:

            tweets = await fetch_tweets(username)

            for tweet in reversed(tweets):

                if tweet["link"] in sent_posts:
                    continue

                if not post_matches_filter(
                    tweet["text"],
                    keywords
                ):
                    continue

                await send_post(bot, tweet, username)

                save_sent_post(tweet["link"])

                sent_posts.add(tweet["link"])

                posted += 1

                await asyncio.sleep(SEND_DELAY)

        except Exception as e:

            logger.error(f"Blogger error @{username}: {e}")

    return posted

# =========================================================
# SCHEDULE LOOP
# =========================================================

async def scheduled_loop(bot):

    await asyncio.sleep(10)

    while True:

        try:

            posted = await check_and_post(bot)

            logger.info(f"Posted: {posted}")

        except Exception as e:

            logger.error(f"Loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

# =========================================================
# COMMANDS
# =========================================================

def is_admin(user_id):
    return user_id == ADMIN_ID

async def cmd_start(update, context):

    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "✅ Бот активен"
    )

async def cmd_status(update, context):

    if not is_admin(update.effective_user.id):
        return

    fans = load_list(FANS_FILE)
    bloggers = load_list(BLOGGERS_FILE)
    keywords = load_list(KEYWORDS_FILE)

    await update.message.reply_text(
        f"🔵 Fans: {len(fans)}\n"
        f"🟡 Bloggers: {len(bloggers)}\n"
        f"🔑 Keywords: {len(keywords)}"
    )

async def cmd_force(update, context):

    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text("🔄 Checking...")

    posted = await check_and_post(context.bot)

    await update.message.reply_text(
        f"✅ Posted: {posted}"
    )

# =========================================================
# MAIN
# =========================================================

async def main():

    if not BOT_TOKEN:

        logger.critical("NO BOT TOKEN")

        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("force", cmd_force))

    bot = Bot(BOT_TOKEN)

    asyncio.create_task(
        scheduled_loop(bot)
    )

    logger.info("BOT STARTED")

    await app.run_polling()

# =========================================================
# ENTRY
# =========================================================

if __name__ == "__main__":

    nest_asyncio.apply()

    asyncio.run(main())
