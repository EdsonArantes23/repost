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

MAX_PARALLEL = 3  # Меньше одновременных запросов
semaphore = asyncio.Semaphore(MAX_PARALLEL)

sent_posts_cache = set()
adding_lock = asyncio.Lock()

BOOT_TIME = None

# 12 Nitter-зеркал
NITTER_MIRRORS = [
    "https://nitter.net",
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://nitter.unixfox.eu",
    "https://nitter.domain.glass",
    "https://nitter.cz",
    "https://nitter.fdn.fr",
    "https://nitter.mint.lgbt",
    "https://nitter.space",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

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
    if not keywords:
        return True
    text_lower = text.lower()
    for kw in keywords:
        pattern = r'\b' + re.escape(kw.lower()) + r'\b'
        if re.search(pattern, text_lower):
            return True
    return False

def escape_html(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def clean_html(text):
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    text = text.replace('&#39;', "'").replace('&quot;', '"')
    text = text.replace('&nbsp;', ' ')
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()

def parse_tweet_time(datetime_str):
    if not datetime_str:
        return None
    for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S.%f"]:
        try:
            dt = datetime.strptime(datetime_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except:
            pass
    return None

# --- ПАРСИНГ ТВИТОВ ЧЕРЕЗ NITTER (12 ЗЕРКАЛ) ---
async def fetch_tweets_nitter(username):
    tweets = []
    display_name = username

    # Перемешиваем зеркала
    mirrors = NITTER_MIRRORS.copy()
    random.shuffle(mirrors)

    for mirror in mirrors:
        url = f"{mirror}/{username}"
        try:
            ua = random.choice(USER_AGENTS)
            headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }

            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(url, headers=headers)

                if response.status_code == 429:
                    logger.warning(f"⚠️ @{username}: {mirror} -> 429, пропускаем")
                    continue
                if response.status_code == 403:
                    logger.warning(f"⚠️ @{username}: {mirror} -> 403, пропускаем")
                    continue
                if response.status_code == 503:
                    logger.warning(f"⚠️ @{username}: {mirror} -> 503, пропускаем")
                    continue
                if response.status_code != 200:
                    continue

                soup = BeautifulSoup(response.text, "lxml")

                # Имя профиля
                name_tag = soup.select_one("a.profile-card-fullname")
                if name_tag:
                    display_name = name_tag.text.strip()

                # Ищем твиты
                tweet_divs = soup.select("div.timeline-item")

                if not tweet_divs:
                    logger.info(f"⚠️ @{username}: {mirror} — нет твитов на странице")
                    continue

                for div in tweet_divs:
                    try:
                        content_div = div.select_one("div.tweet-content")
                        if not content_div:
                            continue
                        text = content_div.text.strip()

                        link_tag = div.select_one("a.tweet-link")
                        if not link_tag:
                            continue
                        href = link_tag.get("href", "")
                        if not href.startswith("http"):
                            href = f"{mirror}{href}"
                        link = href
                        # Приводим к x.com для сохранения
                        link_x = re.sub(r'https?://[^/]+/', 'https://x.com/', link)

                        date_tag = div.select_one("span.tweet-date a")
                        published = date_tag.get("title", "") if date_tag else ""
                        tweet_time = parse_tweet_time(published)

                        images = []
                        for att in div.select("div.attachment, div.attachments"):
                            for img in att.select("img"):
                                src = img.get("src", "")
                                if src and "pbs.twimg.com" in src and src not in images:
                                    if src.startswith("/"):
                                        src = f"{mirror}{src}"
                                    images.append(src)

                        if not images:
                            for img in div.select("div.tweet-body img"):
                                src = img.get("src", "")
                                if src and "pbs.twimg.com" in src and "emoji" not in src.lower() and src not in images:
                                    if src.startswith("/"):
                                        src = f"{mirror}{src}"
                                    images.append(src)

                        videos = []
                        video_tag = div.select_one("video")
                        if video_tag:
                            src = video_tag.get("src", "")
                            if src:
                                if src.startswith("/"):
                                    src = f"{mirror}{src}"
                                videos.append(src)
                            else:
                                source_tag = video_tag.select_one("source")
                                if source_tag:
                                    src = source_tag.get("src", "")
                                    if src and src.startswith("/"):
                                        src = f"{mirror}{src}"
                                    if src:
                                        videos.append(src)

                        tweets.append({
                            "text": text,
                            "link": link_x,
                            "images": images,
                            "videos": videos,
                            "display_name": display_name,
                            "username": username,
                            "published": published if published else None,
                            "tweet_time": tweet_time
                        })
                    except Exception as e:
                        logger.error(f"⚠️ @{username}: ошибка парсинга: {e}")
                        continue

                if tweets:
                    logger.info(f"✅ @{username}: {len(tweets)} твитов через {mirror}")
                    return display_name, tweets

        except Exception as e:
            logger.warning(f"⚠️ @{username}: {mirror} ошибка: {e}")
            continue

    logger.error(f"❌ @{username}: ни одно зеркало не ответило")
    return username, []

async def fetch_tweets_with_limit(username):
    async with semaphore:
        # Увеличенная пауза между запросами
        await asyncio.sleep(random.uniform(1.0, 3.0))
        return await fetch_tweets_nitter(username)

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

    tweet_time = tweet.get("tweet_time")
    if not tweet_time:
        published_str = tweet.get("published")
        if not published_str:
            return False
        tweet_time = parse_tweet_time(published_str)
        if not tweet_time:
            return False

    return tweet_time < boot_time

def is_admin(user_id): return user_id == ADMIN_ID

# --- АДМИН-КОМАНДЫ ---
async def cmd_start(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(
        "👋 Привет, админ!\n\n"
        "📋 Фан-каналы:\n/addfan, /addmanyfan, /removefan, /listfan\n\n"
        "📋 Блогеры:\n/addblogger, /addmanyblogger, /removeblogger, /removemanyblogger, /listbloggers\n\n"
        "🔑 Ключевые слова:\n/addword, /addwords, /removeword, /removemanywords, /listwords\n\n"
        "📊 /status, /force\n\n"
        "⚡ Nitter (12 зеркал)"
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
        f"✅ Бот активен\n📡 @chelsea_news_insider\n🔵 Фан-каналов: {len(fans)}\n🟡 Блогеров: {len(bloggers)}\n🔑 Слов: {len(keywords)}\n📤 Постов: {sent}\n⚡ Nitter (12 зеркал)"
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

    safe_text = escape_html(text)
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

    logger.info(f"🔄 Проверка {len(all_usernames)} каналов (макс {MAX_PARALLEL} одновременно, 12 зеркал)...")
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
    logger.info(f"🤖 Бот запущен — Nitter (12 зеркал, время старта: {boot_time.isoformat()})")
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
