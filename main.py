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

# --- ЛОГИ ---
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- НАСТРОЙКИ ---
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHANNEL_ID = -1003857194781
ADMIN_ID = 417850992
RSSHUB_URL = "https://chelsea-rss-bridge.onrender.com?cacheTime=60"
CHECK_INTERVAL = 15
SENT_POSTS_FILE = "sent_posts.txt"
FANS_FILE = "chelsea_fans.txt"
BLOGGERS_FILE = "general_bloggers.txt"
KEYWORDS_FILE = "keywords.txt"

# ✅ ПРИ ПЕРВОМ ЗАПУСКЕ: не отправлять старые посты
WARMUP_MODE = True

sent_posts_cache = set()
adding_lock = asyncio.Lock()

# --- ФАЙЛЫ ---
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
        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
            sent_posts_cache = set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        sent_posts_cache = set()
    return sent_posts_cache

def save_sent_post(post_id):
    global sent_posts_cache
    if not post_id or post_id in sent_posts_cache:
        return
    sent_posts_cache.add(post_id)
    with open(SENT_POSTS_FILE, "a", encoding="utf-8") as f:
        f.write(post_id + "\n")

# --- УТИЛИТЫ ---
def is_admin(user_id): return user_id == ADMIN_ID

def post_matches_filter(text, keywords):
    if not keywords: return True
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)

def clean_html(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    text = text.replace('&#39;', "'").replace('&quot;', '"')
    return re.sub(r'\n\s*\n', '\n\n', text).strip()

def extract_text_and_media(entry):
    images, videos, text = [], [], ""
    description = getattr(entry, "description", "") or getattr(entry, "summary", "")
    
    if description:
        clean_desc = re.split(r'<hr[^>]*>|<div class="rsshub-quote">', description)[0]
        text_with_breaks = re.sub(r'<br\s*/?>', '\n', clean_desc)
        text = clean_html(text_with_breaks)
        
        img_urls = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', clean_desc)
        for url in img_urls:
            url = url.replace('&amp;', '&')
            if 'pbs.twimg.com' in url and url not in images:
                images.append(url)
                
        if not images:
            direct_urls = re.findall(r'https?://pbs\.twimg\.com/media/[^\s"\'&<>]+', description)
            for url in direct_urls:
                url = url.replace('&amp;', '&')
                if url not in images:
                    images.append(url)
                    
        video_urls = re.findall(r'<video[^>]+src=["\']([^"\']+)["\']', clean_desc)
        for url in video_urls:
            url = url.replace('&amp;', '&')
            if url not in videos:
                videos.append(url)
                
    if not text:
        title = getattr(entry, "title", "") or ""
        text = clean_html(title)
        
    return text, images, videos

# --- FETCH ---
async def fetch_tweets(username):
    url = f"{RSSHUB_URL}/twitter/user/{username}"
    logger.debug(f"🔍 @{username}: запрос к {url}")
    
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml, */*"}
                response = await client.get(url, headers=headers)
                
                logger.debug(f"🔍 @{username}: статус {response.status_code}, размер ответа: {len(response.text)} байт")
                
                if response.status_code == 503:
                    wait = 3 if attempt == 0 else 5
                    logger.warning(f"⚠️ @{username}: 503, попытка {attempt+1}/2, жду {wait} сек")
                    await asyncio.sleep(wait)
                    continue
                    
                response.raise_for_status()
                
                # ✅ DEBUG: покажем, что пришло от RSSHub
                if response.text.count('<item>') == 0:
                    logger.warning(f"⚠️ @{username}: RSSHub вернул 200, но НЕТ <item>! Начало ответа: {response.text[:300]}")
                
                feed = feedparser.parse(response.text)
                logger.debug(f"🔍 @{username}: feedparser нашёл {len(feed.entries)} записей")
                
                display_name = username
                if hasattr(feed.feed, "title"):
                    display_name = feed.feed.title.replace("Twitter @", "").strip()
                    
                tweets = []
                for i, entry in enumerate(feed.entries):
                    try:
                        text, images, videos = extract_text_and_media(entry)
                        link = getattr(entry, "link", "")
                        
                        # ✅ FIX: надёжный ключ — извлекаем ID твита или делаем fallback
                        match = re.search(r'/status/(\d+)', link)
                        tweet_id = match.group(1) if match else (link or f"{username}:{text[:30]}")
                        
                        logger.debug(f"🔍 @{username} entry#{i}: tweet_id={tweet_id[:20]}..., text_len={len(text)}, images={len(images)}, videos={len(videos)}")
                        
                        tweets.append({
                            "text": text, "link": link, "tweet_id": tweet_id,
                            "images": images, "videos": videos,
                            "display_name": display_name, "username": username
                        })
                    except Exception as e:
                        logger.error(f"❌ @{username}: ошибка парсинга entry#{i}: {e}")
                        continue
                        
                logger.info(f"✅ @{username}: распарсено {len(tweets)} твитов")
                return display_name, tweets
                
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ @{username}: HTTP {e.response.status_code}")
            if e.response.status_code != 503:
                break
            await asyncio.sleep(3)
        except (httpx.ConnectTimeout, httpx.ReadTimeout):
            logger.warning(f"⚠️ @{username}: таймаут, попытка {attempt+1}/2")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"❌ @{username}: {type(e).__name__}: {e}")
            break
            
    logger.error(f"❌ @{username}: не удалось после попыток")
    return username, []

async def fetch_all_tweets(usernames):
    tasks = [fetch_tweets(u) for u in usernames]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_tweets = []
    for res in results:
        if isinstance(res, Exception):
            logger.error(f"❌ Ошибка при параллельной проверке: {res}")
            continue
        if not isinstance(res, tuple) or len(res) != 2:
            continue
        _, tweets = res
        if tweets:
            all_tweets.extend(tweets)
    return all_tweets

# --- TELEGRAM SEND ---
async def send_post(bot: Bot, tweet, username):
    text = tweet["text"]
    signature = f"\n\n{tweet['display_name']} | https://x.com/{username}"
    full_text = text + signature
    images = tweet["images"]
    videos = tweet["videos"]
    
    try:
        if videos:
            # ✅ FIX: disable_web_page_preview убран (его нет в send_video)
            await bot.send_video(
                TELEGRAM_CHANNEL_ID, video=videos[0],
                caption=full_text[:1024], supports_streaming=True
            )
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
            # ✅ Здесь параметр оставляем
            await bot.send_message(TELEGRAM_CHANNEL_ID, full_text, disable_web_page_preview=True)
        return True
    except TelegramError as e:
        logger.error(f"❌ Ошибка отправки: {e}")
        try:
            await bot.send_message(TELEGRAM_CHANNEL_ID, full_text[:4096], disable_web_page_preview=True)
            return True
        except Exception as e2:
            logger.error(f"❌ Фолбэк не сработал: {e2}")
            return False

# --- CORE ---
async def check_and_post(bot: Bot, warmup: bool = False):
    global sent_posts_cache
    async with adding_lock:
        pass
    fans = load_fans()
    bloggers = load_bloggers()
    all_usernames = list(set(fans + bloggers))
    
    if not all_usernames: 
        logger.info("⚠️ Нет аккаунтов для проверки")
        return 0
    if not sent_posts_cache: 
        sent_posts_cache = load_sent_posts()
        logger.info(f"📦 Загружено {len(sent_posts_cache)} ID из кэша")
    
    logger.info(f"🔄 Параллельная проверка {len(all_usernames)} каналов...")
    start_time = time.time()
    all_tweets = await fetch_all_tweets(all_usernames)
    elapsed = time.time() - start_time
    logger.info(f"⏱ Проверка заняла {elapsed:.1f} сек, получено {len(all_tweets)} твитов")
    
    keywords = load_keywords()
    new_posts = 0
    
    for tweet in all_tweets:
        tweet_id = tweet.get("tweet_id") or tweet.get("link")
        if not tweet_id:
            logger.warning(f"⚠️ Пропущен твит без ID: {tweet.get('username')}")
            continue
        if tweet_id in sent_posts_cache:
            continue
            
        username = tweet["username"]
        if username in bloggers and username not in fans:
            if not post_matches_filter(tweet["text"], keywords):
                continue
        
        # ✅ В режиме прогрева только заполняем кэш, не шлём в ТГ
        if not warmup:
            success = await send_post(bot, tweet, username)
            if success:
                logger.info(f"📨 @{username} | ID: {tweet_id[:20]}...")
                new_posts += 1
                await asyncio.sleep(0.5)
            
        save_sent_post(tweet_id)
        
    if new_posts:
        logger.info(f"✅ Отправлено {new_posts} новых постов")
    return new_posts

async def scheduled_check(bot: Bot):
    global WARMUP_MODE
    if WARMUP_MODE:
        logger.info("🔥 WARMUP MODE: заполняю кэш, НЕ отправляю старые посты")
        await check_and_post(bot, warmup=True)
        WARMUP_MODE = False
        logger.info("✅ Кэш заполнен. Теперь будут отправляться ТОЛЬКО новые посты")
        await asyncio.sleep(5)
        
    await asyncio.sleep(5)
    while True:
        try:
            await check_and_post(bot, warmup=False)
        except Exception as e:
            logger.error(f"❌ Цикл: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL)

# --- КОМАНДЫ ---
async def cmd_start(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("👋 Бот готов. /status, /force, /addfan, /addblogger...")

async def cmd_addfan(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addfan @username"); return
    username = context.args[0].strip().lstrip("@")
    fans = load_fans()
    if username in fans: await update.message.reply_text(f"⚠️ @{username} уже в фан-каналах."); return
    async with adding_lock:
        await update.message.reply_text(f"⏳ Добавляю @{username}...")
        try:
            display_name, _ = await fetch_tweets(username)
            fans.append(username)
            save_fans(fans)
            count = await mark_all_current_as_sent(username)
            await update.message.reply_text(f"✅ {display_name} (@{username}) добавлен.\n📤 {count} постов пропущено.")
        except Exception as e:
            logger.exception(f"❌ Ошибка в cmd_addfan: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_addmanyfan(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addmanyfan ссылки"); return
    raw_input = " ".join(context.args)
    mentions = re.findall(r'@(\w+)', raw_input)
    links = re.findall(r'https?://(?:x.com|twitter.com)/(\w+)', raw_input)
    usernames = list(dict.fromkeys(mentions + links))
    if not usernames: await update.message.reply_text("❌ Не удалось распознать username."); return
    async with adding_lock:
        fans = load_fans()
        added, skipped, failed = [], [], []
        await update.message.reply_text(f"⏳ Обрабатываю {len(usernames)} каналов...")
        for username in usernames:
            if username in fans: skipped.append(f"• @{username}"); continue
            try:
                display_name, _ = await fetch_tweets(username)
                fans.append(username)
                save_fans(fans)
                count = await mark_all_current_as_sent(username)
                added.append(f"✅ {display_name} (@{username}) — {count} пропущено")
            except Exception as e:
                logger.exception(f"❌ Ошибка при добавлении @{username}: {e}")
                failed.append(f"❌ @{username}")
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
            logger.exception(f"❌ Ошибка в cmd_addblogger: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_addmanyblogger(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addmanyblogger ссылки"); return
    raw_input = " ".join(context.args)
    mentions = re.findall(r'@(\w+)', raw_input)
    links = re.findall(r'https?://(?:x.com|twitter.com)/(\w+)', raw_input)
    usernames = list(dict.fromkeys(mentions + links))
    if not usernames: await update.message.reply_text("❌ Не удалось распознать username."); return
    async with adding_lock:
        bloggers = load_bloggers()
        keywords = load_keywords()
        kw_msg = f"🔑 Слова: {', '.join(keywords)}" if keywords else "⚠️ Слов нет — репостится всё!"
        added, skipped, failed = [], [], []
        await update.message.reply_text(f"⏳ Обрабатываю {len(usernames)} блогеров...\n{kw_msg}")
        for username in usernames:
            if username in bloggers: skipped.append(f"• @{username}"); continue
            try:
                display_name, _ = await fetch_tweets(username)
                bloggers.append(username)
                save_bloggers(bloggers)
                count = await mark_all_current_as_sent(username)
                added.append(f"✅ {display_name} (@{username}) — {count} пропущено")
            except Exception as e:
                logger.exception(f"❌ Ошибка при добавлении блогера @{username}: {e}")
                failed.append(f"❌ @{username}")
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

async def cmd_listbloggers(update, context):
    if not is_admin(update.effective_user.id): return
    bloggers = load_bloggers()
    keywords = load_keywords()
    kw_msg = f"🔑 Слова: {', '.join(keywords)}" if keywords else "⚠️ Слов нет"
    if not bloggers: await update.message.reply_text(f"📋 Блогеры: пусто.\n{kw_msg}"); return
    await update.message.reply_text(f"📋 Блогеры:\n" + "\n".join([f"• @{b}" for b in bloggers]) + f"\n\n{kw_msg}")

async def cmd_addword(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addword слово"); return
    word = context.args[0].strip().lower()
    keywords = load_keywords()
    if word in keywords: await update.message.reply_text(f"⚠️ '{word}' уже в списке."); return
    keywords.append(word)
    save_keywords(keywords)
    await update.message.reply_text(f"✅ '{word}' добавлен. Всего: {len(keywords)}")

async def cmd_addwords(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /addwords слово1 слово2 ..."); return
    raw_input = " ".join(context.args)
    words = re.split(r'[,\s;\n]+', raw_input)
    words = [w.strip().lower() for w in words if w.strip()]
    if not words: await update.message.reply_text("❌ Не удалось распознать слова."); return
    keywords = load_keywords()
    added, skipped = [], []
    for word in words:
        if word in keywords: skipped.append(word)
        else: keywords.append(word); added.append(word)
    save_keywords(keywords)
    report = []
    if added: report.append(f"✅ Добавлены ({len(added)}): {', '.join(added)}")
    if skipped: report.append(f"⚠️ Уже были ({len(skipped)}): {', '.join(skipped)}")
    await update.message.reply_text("\n".join(report) + f"\n\n🔑 Всего: {len(keywords)}")

async def cmd_removeword(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ /removeword слово"); return
    word = context.args[0].strip().lower()
    keywords = load_keywords()
    if word not in keywords: await update.message.reply_text(f"⚠️ '{word}' не найден."); return
    keywords.remove(word)
    save_keywords(keywords)
    await update.message.reply_text(f"✅ '{word}' удалён. Всего: {len(keywords)}")

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
    count = await check_and_post(context.bot, warmup=False)
    await update.message.reply_text(f"✅ Отправлено: {count} постов.")

async def mark_all_current_as_sent(username):
    _, tweets = await fetch_tweets(username)
    count = 0
    for t in tweets:
        save_sent_post(t.get("tweet_id") or t.get("link"))
        count += 1
    return count

# --- MAIN ---
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
    logger.info("🤖 Бот запущен")
    await app.run_polling()

if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())
