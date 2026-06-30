# --- ملف: main.py (النسخة النهائية للاستضافة السحابية - تخطي حماية MovieBox بالمتصفح) ---

# --- المكتبات الأساسية ---
import sys
import io
import re
import urllib.parse
from urllib.parse import urljoin, quote_plus
import requests
from bs4 import BeautifulSoup
import json
import subprocess
import difflib
import asyncio
import base64
import math
import os
import time
import concurrent.futures
import uuid

# --- مكتبات الـ API والبروكسي ---
from flask import Flask, request, jsonify, Response, send_from_directory, url_for, stream_with_context
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# --- مكتبات التشغيل الآلي للمتصفح ---
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sys.stderr.write("WARN: Playwright not installed. Some providers will not work.\n")
    class PlaywrightTimeoutError(Exception): pass
    async_playwright = None

# --- مكتبة الذكاء الاصطناعي ---
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from fake_useragent import UserAgent
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pysrt
from pydub import AudioSegment
from gradio_client import Client

try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
except (TypeError, AttributeError):
    pass

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.5'
}

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TMDB_BACKEND_URL = os.environ.get("TMDB_BACKEND_URL", "http://localhost:3000")

if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception:
        GEMINI_AVAILABLE = False
else:
    GEMINI_AVAILABLE = False

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def format_bytes(size_bytes):
    if size_bytes is None: return None
    try:
        size_bytes = int(size_bytes)
        if size_bytes <= 0: return None
        size_name = ("B", "KB", "MB", "GB", "TB")
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"
    except (ValueError, TypeError):
        return None

# ==============================================================================
# ========================   PROVIDER: AKWAM   ===============================
# ==============================================================================
def akwam_make_request(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        return BeautifulSoup(response.text, 'html.parser')
    except requests.exceptions.RequestException: return None

def select_best_match_with_gemini(user_query, media_type, target_season, all_results):
    if not GEMINI_AVAILABLE: return all_results[0] if all_results else None
    model = genai.GenerativeModel('gemini-1.5-flash')
    formatted_results = "\n".join([f"id:{i}, title:\"{res['title']}\", url:\"{res['url']}\"" for i, res in enumerate(all_results)])
    prompt = f"""You are an intelligent search result selector. Find the single best match from a list of search results.
USER'S REQUEST:
Title: "{user_query}"
Type: "{media_type}"
Requested Season: {target_season or 'N/A'}
SEARCH RESULTS:
{formatted_results}
Output: MUST be a single JSON object with the ID. Example: {{"best_choice_id": 0}}
"""
    try:
        response = model.generate_content(prompt)
        json_text = re.search(r'\{.*\}', response.text, re.DOTALL).group(0)
        best_id = int(json.loads(json_text).get('best_choice_id'))
        return all_results[best_id] if 0 <= best_id < len(all_results) else all_results[0]
    except Exception: return all_results[0] if all_results else None

def akwam_get_video_links_from_player(content_page_url):
    soup = akwam_make_request(content_page_url)
    if not soup: return []
    final_video_links, watch_page_urls = set(), []
    for tab in soup.find_all('div', class_='tab-content quality'):
        watch_link_tag = tab.find('a', class_='link-show')
        if watch_link_tag and 'href' in watch_link_tag.attrs:
            watch_id = watch_link_tag['href'].split('/')[-1]
            try:
                url_parts = content_page_url.split('/')
                watch_page_urls.append(f"https://ak.sv/watch/{watch_id}/{url_parts[-2]}/{url_parts[-1]}")
            except IndexError: continue
    for url in set(watch_page_urls):
        player_soup = akwam_make_request(url)
        if not player_soup: continue
        video_tag = player_soup.find('video', id='player')
        if video_tag:
            for source in video_tag.find_all('source'):
                if source.get('src'): final_video_links.add((source.get('size', 'N/A'), source['src']))
    return [{"quality": quality, "url": link, "needs_proxy": False} for quality, link in sorted(list(final_video_links), key=lambda x: int(x[0]) if x[0].isdigit() else 0, reverse=True)]

def akwam_find_episode_on_season_page(season_url, episode_number):
    season_soup = akwam_make_request(season_url)
    if not season_soup: return None
    episode_pattern = re.compile(r'(?:الحلقة|حلقة)\s*(\d{1,3})', re.IGNORECASE)
    for container in season_soup.find_all('div', class_='bg-primary2'):
        title_tag = container.find('h2').find('a') if container.find('h2') else None
        if title_tag and title_tag.get('href'):
            match = episode_pattern.search(' '.join(title_tag.text.strip().split()))
            if match and int(match.group(1)) == episode_number: return title_tag['href']
    return None

def scrape_akwam(query, media_type, season_num, episode_num):
    all_search_results, current_page = [], 1
    while True:
        search_soup = akwam_make_request(f"https://ak.sv/search?q={urllib.parse.quote_plus(query)}&page={current_page}")
        if not search_soup: break
        results_on_page = [{'title': tag.text.strip(), 'url': tag['href']} for entry in search_soup.select('div.widget-body div.entry-box-1') if (tag := entry.find('h3', class_='entry-title').find('a')) and tag.get('href')]
        if not results_on_page: break
        all_search_results.extend(results_on_page)
        if search_soup.find('nav', attrs={'aria-label': 'Page navigation'}) and search_soup.find('nav').find('a', class_='page-link', string=re.compile(r'التالي')): current_page += 1
        else: break
    if not all_search_results: return {"status": "error", "message": "No results found on Akwam."}
    selected = select_best_match_with_gemini(query, media_type, season_num, all_search_results)
    if media_type == 'movie':
        links = akwam_get_video_links_from_player(selected['url'])
        return {"status": "success", "links": links} if links else {"status": "error", "message": "No links found."}
    elif media_type == 'series':
        episode_url = akwam_find_episode_on_season_page(selected['url'], episode_num)
        if not episode_url: return {"status": "error", "message": "Episode not found."}
        links = akwam_get_video_links_from_player(episode_url)
        return {"status": "success", "links": links} if links else {"status": "error", "message": "No links found."}

# ==============================================================================
# ========================   PROVIDER: VELORATV   ============================
# ==============================================================================
async def velora_extract_links_from_url(url: str, context):
    VELORA_M3U8_PATTERN = re.compile(r"https://[^\s\"']+.m3u8[^\s\"']")
    VELORA_SUBTITLE_PATTERN = re.compile(r"https://[^\s\"']+format=srt[^\s\"']")
    m3u8_links, subtitle_links = set(), set()
    page = await context.new_page()
    def handle_request(req):
        if VELORA_M3U8_PATTERN.search(req.url): m3u8_links.add(req.url)
        if VELORA_SUBTITLE_PATTERN.search(req.url): subtitle_links.add(req.url)
    page.on("request", handle_request)
    try:
        await page.goto(url, timeout=60000, wait_until='domcontentloaded')
        await page.wait_for_selector("div.player-servers, iframe", timeout=15000)
        for server in ["Alpha", "Bravo", "Charlie"]:
            try:
                await page.get_by_text(server, exact=True).click(timeout=5000)
                await page.wait_for_timeout(3000)
                if m3u8_links: break
            except Exception: pass
    except Exception: pass
    finally: await page.close()
    return m3u8_links, subtitle_links

async def velora_async_main(watch_url):
    if not async_playwright: return set(), set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=HEADERS['User-Agent'])
        return await velora_extract_links_from_url(watch_url, context)

def scrape_veloratv(media_type, season, episode, tmdb_id):
    watch_url = f"https://veloratv.ru/watch/{'movie' if media_type == 'movie' else f'tv/{tmdb_id}/{season}/{episode}'}/{tmdb_id}"
    try:
        m3u8_links, subtitle_links = asyncio.run(velora_async_main(watch_url))
        if not m3u8_links: return {"status": "error", "message": "No m3u8 links found."}
        res = {"status": "success", "links": [{"quality": "proxied_m3u8", "url": link, "needs_proxy": True} for link in m3u8_links]}
        if subtitle_links: res["subtitles"] = [{"lang": "ar", "url": sub} for sub in subtitle_links]
        return res
    except Exception as e: return {"status": "error", "message": str(e)}

# ==============================================================================
# ========================   PROVIDER: AFLAM   ===============================
# ==============================================================================
def scrape_aflam(query, media_type, episode_num):
    session = requests.Session()
    session.headers.update(HEADERS)
    search_url = f"https://afllam.onl/?s={urllib.parse.quote(query)}"
    try:
        response = session.get(search_url, timeout=20)
        soup = BeautifulSoup(response.text, 'html.parser')
        entries = soup.select('div.widget-body .entry-box-1')
        if not entries: return {"status": "error", "message": "No search results."}
        results_map = {entry.select_one('h3.entry-title a').text.strip(): entry.select_one('h3.entry-title a')['href'] for entry in entries if entry.select_one('h3.entry-title a')}
        best_matches = difflib.get_close_matches(query, list(results_map.keys()), n=1, cutoff=0.5)
        if not best_matches: return {"status": "error", "message": "No close match."}
        page_url = results_map[best_matches[0]]
        
        soup = BeautifulSoup(session.get(page_url, timeout=20).text, 'html.parser')
        if soup.find('div', class_='EpisodesArea'):
            for link_tag in soup.select('div.EpisodesArea div.bg-primary2 h2 a'):
                match = re.compile(r'(?:الحلقة|حلقة)\s(\d+)').search(link_tag.get_text(strip=True))
                if match and int(match.group(1)) == episode_num: page_url = link_tag['href']; break
        
        soup = BeautifulSoup(session.post(page_url, headers={'Referer': page_url}, data={'watch': '1'}, timeout=20).text, 'html.parser')
        links = []
        for server_item in soup.select('ul#watch-servers-list li'):
            if encoded_url := server_item.get('data-encoded'):
                try:
                    iframe_src = base64.b64decode(encoded_url).decode('utf-8')
                    result = subprocess.run(['yt-dlp', '-g', '--no-warnings', iframe_src], capture_output=True, text=True, check=True, timeout=45)
                    if result.stdout.strip().startswith('http'): links.append({"quality": "Direct MP4", "url": result.stdout.strip().split('\n')[0], "needs_proxy": False})
                except Exception: pass
        if links: return {"status": "success", "links": links}
        return {"status": "error", "message": "No links found."}
    except Exception as e: return {"status": "error", "message": str(e)}

# ==============================================================================
# ========================   PROVIDER: RISTOANIME   ==========================
# ==============================================================================
def scrape_ristoanime(query, season_num, episode_num):
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        search_results = BeautifulSoup(session.get(f"https://ristoanime.org/?s={urllib.parse.quote_plus(query)}", timeout=15).text, 'html.parser').select('div.MovieItem a')
        if not search_results: return {"status": "error", "message": "Anime not found."}
        soup = BeautifulSoup(session.get(search_results[0]['href'], timeout=15).text, 'html.parser')
        episodes_html = ""
        for tab in soup.select('div.SeasonsList ul li a'):
            if re.compile(fr'(الموسم|موسم)\s*{season_num}').search(tab.get_text(strip=True)):
                episodes_html = session.post("https://ristoanime.org/wp-content/themes/TopAnime/Ajaxt/Single/Episodes.php", data={'season': tab['data-season']}, timeout=15).text
                break
        if not episodes_html and (ep_list := soup.select_one('div.EpisodesList')): episodes_html = str(ep_list)
        
        episode_url = None
        for link in BeautifulSoup(episodes_html, 'html.parser').select('a'):
            match = re.compile(r'(?:الحلقة|حلقة)\s*(\d+)').search(link.get_text(strip=True))
            if match and int(match.group(1)) == episode_num: episode_url = link['href']; break
        if not episode_url: return {"status": "error", "message": "Episode not found."}
        
        server = BeautifulSoup(session.get(episode_url.strip('/') + '/watch/', timeout=15).text, 'html.parser').select_one('ul#watch li[data-watch]')
        if not server: return {"status": "error", "message": "No servers found."}
        
        result = subprocess.run(['yt-dlp', '-g', '--referer', episode_url.strip('/') + '/watch/', server['data-watch']], capture_output=True, text=True, check=True, timeout=60)
        for link in reversed(result.stdout.strip().split('\n')):
            if "m3u8" in link or "mp4" in link: return {"status": "success", "links": [{"quality": "proxied_m3u8", "url": link, "needs_proxy": True}]}
        return {"status": "error", "message": "Stream link extraction failed."}
    except Exception as e: return {"status": "error", "message": str(e)}

# ==============================================================================
# ====================   PROVIDER: ARABIC-TOONS   ============================
# ==============================================================================
def scrape_arabic_toons(query, season_num, episode_num):
    session = requests.Session()
    session.mount("http://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5)))
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5)))
    session.headers.update({'User-Agent': UserAgent().random, 'Referer': "https://www.arabic-toons.com/"})
    try:
        search_html = session.get("https://snowy-term-f692.itsyassine16.workers.dev/", params={"url": f"https://www.arabic-toons.com/livesearch.php?q={urllib.parse.quote(query)}"}, timeout=20).text
        search_results = BeautifulSoup(search_html, 'html.parser').find_all('a', class_='list-group-item')
        best_match = {'path': None, 'score': -1}
        for item in search_results:
            title = item.get_text(strip=True).replace(item.find('span').get_text(strip=True), '').strip()
            score = difflib.SequenceMatcher(None, query, title).ratio()
            if (m := re.compile(r'(?:الموسم|الجزء|موسم|جزء)\s*(\d+)').search(title)) and int(m.group(1)) == season_num: score += 1.0
            if score > best_match['score']: best_match.update({'score': score, 'path': item['href']})
        
        episodes_html = session.get("https://snowy-term-f692.itsyassine16.workers.dev/", params={"url": "https://www.arabic-toons.com/" + best_match['path']}, timeout=20).text
        for episode_div in BeautifulSoup(episodes_html, 'html.parser').find('div', class_='moviesBlocks').find_all('div', class_='movie'):
            link_tag = episode_div.find('a')
            if link_tag and (name_tag := link_tag.find('div', class_='badge-overd')) and re.compile(r'(\d+)').search(name_tag.get_text(strip=True)) and int(re.compile(r'(\d+)').search(name_tag.get_text(strip=True)).group(1)) == episode_num:
                resp = session.get("https://www.arabic-toons.com/" + link_tag['href'], timeout=20).text
                if m := re.search(r'yB0hQ\s=\s*\'([^\']+.m3u8[^\']*)\'', resp): return {"status": "success", "links": [{"quality": "Direct M3U8", "url": m.group(1), "needs_proxy": False}]}
                if m := re.search(r'x9zFqV3\s*=\s*{([^}]+)}', resp):
                    parts = dict(re.findall(r'(\w+):\s*"([^"]+)"', m.group(1)))
                    if all(k in parts for k in ("jC1kO", "hF3nV", "iA5pX", "tN4qY")): return {"status": "success", "links": [{"quality": "Direct M3U8", "url": f"{parts['jC1kO']}://{parts['hF3nV']}/{parts['iA5pX']}?{parts['tN4qY']}", "needs_proxy": False}]}
        return {"status": "error", "message": "Episode not found."}
    except Exception as e: return {"status": "error", "message": str(e)}

# ==============================================================================
# ========================   PROVIDER: TMDB   ================================
# ==============================================================================
def scrape_tmdb(media_type, tmdb_id, season=None, episode=None):
    endpoint = f"{TMDB_BACKEND_URL}/movie/{tmdb_id}" if media_type == 'movie' else f"{TMDB_BACKEND_URL}/tv/{tmdb_id}?s={season}&e={episode}"
    try:
        response = requests.get(endpoint, headers=HEADERS, timeout=60, verify=False)
        response.raise_for_status()
        data = response.json()
        if 'files' not in data or not data['files']: return {"status": "error", "message": "No media files found."}
        links = [{"quality": "MP4" if f['type'] == 'mp4' else "HLS", "url": f['file'], "needs_proxy": False} for f in data['files']]
        result = {"status": "success", "links": links}
        if 'subtitles' in data and data['subtitles']: result["subtitles"] = [{"lang": s.get('lang', 'en'), "url": s['url']} for s in data['subtitles']]
        return result
    except Exception as e: return {"status": "error", "message": str(e)}

# ==============================================================================
# ========================   PROVIDER: MOVIEBOX (PLAYWRIGHT FIX)   =============
# ==============================================================================
# 🌟 التحديث الذهبي: نستخدم Playwright للتعامل مع API الخاص بـ MovieBox لتجاوز حظر 403 Forbidden
async def fetch_moviebox_data_via_browser(detail_path, season_num, episode_num, media_type):
    if not async_playwright:
        raise Exception("Playwright is required to bypass MovieBox WAF protection.")
    
    async with async_playwright() as p:
        # إطلاق المتصفح
        browser = await p.chromium.launch(headless=True)
        # تحديد الهيدرز لتطابق المتصفح الحقيقي
        context = await browser.new_context(
            user_agent=HEADERS['User-Agent'],
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9',
                'Origin': 'https://netfilm.world',
                'Referer': 'https://netfilm.world/'
            }
        )
        page = await context.new_page()

        # 1. الدخول للصفحة الرئيسية لـ MovieBox لتخطي حماية Cloudflare WAF وأخذ الكوكيز الحقيقية
        sys.stderr.write("[*] MOVIEBOX-LOG: Bypassing WAF via Playwright...\n")
        try:
            await page.goto("https://netfilm.world/", timeout=15000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000) # انتظار قليل للسماح للجافاسكريبت بالعمل
        except Exception:
            pass # نتجاهل التايم أوت ونكمل

        # 2. جلب الـ Subject ID باستخدام دالة داخلية في المتصفح (لتفادي أي حظر على مستوى Python)
        sys.stderr.write("[*] MOVIEBOX-LOG: Fetching details API internally...\n")
        detail_api = f"https://h5-api.aoneroom.com/wefeed-h5api-bff/detail?detailPath={detail_path}"
        
        # التوكنز الوهمية
        fake_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1aWQiOjc2OTU3MzE5ODU3Mjk2MDg1MjAsImF0cCI6MywiZXh0IjoiMTc3ODc3NDc1MiIsImV4cCI6MTc4NjU1MDc1MiwiaWF0IjoxNzc4Nzc0NDUyfQ.DNO0J8lH7m650oAsiiib9dVUrFcceOqICkt_OIxACNE"
        
        js_fetch_detail = f"""
        async () => {{
            const res = await fetch("{detail_api}", {{
                headers: {{
                    "x-client-info": '{{"timezone":"Africa/Casablanca"}}',
                    "x-user": '{{"token":"{fake_token}","userId":"7695731985729608520","userType":0,"appType":3}}'
                }}
            }});
            return await res.json();
        }}
        """
        detail_data = await page.evaluate(js_fetch_detail)
        subject_id = detail_data.get('data', {}).get('subject', {}).get('subjectId')

        if not subject_id:
            await browser.close()
            raise Exception("Failed to get Subject ID from MovieBox inside browser.")

        # 3. جلب روابط الفيديو (Play API)
        sys.stderr.write(f"[*] MOVIEBOX-LOG: Fetching Play API for Subject {subject_id}...\n")
        se = season_num if media_type == 'series' and season_num else 0
        ep = episode_num if media_type == 'series' and episode_num else 0
        
        play_api = f"https://netfilm.world/wefeed-h5api-bff/subject/play?subjectId={subject_id}&se={se}&ep={ep}&detailPath={detail_path}"
        
        js_fetch_play = f"""
        async () => {{
            const res = await fetch("{play_api}", {{
                headers: {{
                    "x-client-info": '{{"timezone":"Africa/Casablanca"}}',
                    "x-user": '{{"token":"{fake_token}","userId":"7695731985729608520","userType":0,"appType":3}}'
                }}
            }});
            return await res.json();
        }}
        """
        play_data = await page.evaluate(js_fetch_play)
        
        # إذا كان مسلسل ولم يجد الحلقة، نجرب se=0
        if (not play_data.get('data') or not play_data['data'].get('hasResource')) and media_type == 'series':
            play_api_fallback = f"https://netfilm.world/wefeed-h5api-bff/subject/play?subjectId={subject_id}&se=0&ep={ep}&detailPath={detail_path}"
            play_data = await page.evaluate(js_fetch_play.replace(play_api, play_api_fallback))

        # 4. جلب الترجمات إن وجدت
        subs_data = None
        streams = play_data.get('data', {}).get('streams', [])
        if streams and len(streams) > 0 and streams[0].get('id'):
            stream_id = streams[0]['id']
            subs_api = f"https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/caption?format=MP4&id={stream_id}&subjectId={subject_id}&detailPath={detail_path}"
            try:
                subs_data = await page.evaluate(js_fetch_play.replace(play_api, subs_api))
            except Exception: pass

        await browser.close()
        return play_data.get('data', {}), subs_data

def scrape_moviebox(query, media_type, season_num, episode_num):
    sys.stderr.write(f"[*] MOVIEBOX-LOG: Starting search for '{query}'...\n")
    # 1. البحث باستخدام requests العادي لأنه صفحة HTML ولا يوجد عليها حظر API شديد
    session = requests.Session()
    search_url = f"https://moviebox.ph/web/searchResult?keyword={urllib.parse.quote_plus(query)}"
    headers = {'User-Agent': HEADERS['User-Agent'], 'Accept-Language': 'en-US,en;q=0.5'}
    try:
        res = session.get(search_url, headers=headers, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        cards = soup.find_all('a', href=re.compile(r'^/moviedetail/'))
        if not cards: return {"status": "error", "message": f"No search results for '{query}'."}

        results_map = {card.find('h2', class_='card-title').text.strip(): card.get('href').split('/')[-1] for card in cards if card.find('h2', class_='card-title')}
        best_title, detail_path = None, None
        query_lower = query.lower().strip()
        filtered_results = {k: v for k, v in results_map.items() if "française" not in k.lower()} or results_map

        if media_type == 'series' and season_num:
            for t, p in filtered_results.items():
                if t.lower().startswith(f"{query_lower} s{season_num}"): best_title, detail_path = t, p; break
        if not best_title and media_type == 'series':
            for t, p in filtered_results.items():
                if query_lower in t.lower() and re.search(r's\d+-s\d+', t.lower()): best_title, detail_path = t, p; break
        if not best_title:
            for t, p in filtered_results.items():
                if t.lower() == query_lower: best_title, detail_path = t, p; break
        if not best_title:
            best_matches = difflib.get_close_matches(query_lower, [k.lower() for k in filtered_results.keys()], n=1, cutoff=0.6)
            if best_matches:
                for t, p in filtered_results.items():
                    if t.lower() == best_matches[0]: best_title, detail_path = t, p; break
            else:
                best_title, detail_path = list(filtered_results.items())[0]
    except Exception as e:
        return {"status": "error", "message": f"Search failed: {e}"}

    sys.stderr.write(f"[*] MOVIEBOX-LOG: Found Path '{detail_path}'. Handing over to Playwright...\n")
    
    # 2. تشغيل Playwright لتخطي 403 وجلب الـ JSON
    try:
        play_data, subs_data = asyncio.run(fetch_moviebox_data_via_browser(detail_path, season_num, episode_num, media_type))
    except Exception as e:
        return {"status": "error", "message": f"MovieBox API blocked or failed: {str(e)}"}

    if not play_data or not play_data.get('hasResource'):
        return {"status": "error", "message": "MovieBox: No streams available."}

    links = []
    # 🌟 كل السيرفرات تحتاج إلى البروكسي الداخلي (needs_proxy = True)
    for stream in play_data.get('dash', []) or play_data.get('hls', []):
        if stream.get('url'): links.append({"quality": f"{stream.get('format', 'HLS')} - {stream.get('resolutions', 'HD')}", "url": stream['url'], "needs_proxy": True})
    for stream in play_data.get('streams', []):
        if stream.get('url'): links.append({"quality": f"{stream.get('format', 'MP4')} - {stream.get('resolutions', 'HD')} - {format_bytes(stream.get('size')) or 'Unknown'}", "url": stream['url'], "needs_proxy": True})

    all_subtitles = []
    if subs_data and subs_data.get('data', {}).get('captions'):
        for cap in subs_data['data']['captions']:
            if cap.get('url') and cap.get('lan'): all_subtitles.append({"lang": cap['lan'], "url": cap['url']})

    final_result = {"status": "success", "links": links}
    if all_subtitles: final_result["subtitles"] = all_subtitles
    return final_result

# ==============================================================================
# ========================   PROVIDER: SUBTITLES   ===========================
# ==============================================================================
def get_subtitles_from_wyzie(content_type, tmdb_id, season=None, episode=None):
    url = f"https://sub.wyzie.ru/search?id={tmdb_id}&format=srt" if content_type == "movie" else f"https://sub.wyzie.ru/search?id={tmdb_id}&season={season}&episode={episode}&format=srt"
    try:
        resp = requests.get(url, headers={"user-agent": HEADERS['User-Agent']}, timeout=15)
        return {"status": "success", "response_data": resp.json() if 'application/json' in resp.headers.get('content-type', '') else resp.text}
    except Exception as e: return {"status": "error", "message": str(e)}

# ==============================================================================
# =====================   PROVIDER: DUBBING (TTS)   ==========================
# ==============================================================================
OUTPUT_DIR = "output_audio"
os.makedirs(OUTPUT_DIR, exist_ok=True)
try: tts_client = Client("NihalGazi/Text-To-Speech-Unlimited")
except Exception: tts_client = None

def tts_process_line(i, sub, voice_name, job_dir):
    try:
        out_file = os.path.join(job_dir, f"line_{sub.index}.mp3")
        for attempt in range(5):
            try:
                audio_path, _ = tts_client.predict(sub.text, voice_name, "neutral", True, 12345, "", api_name="/text_to_speech_app")
                os.rename(audio_path, out_file)
                return {"file_path": out_file, "start_ms": (sub.start.hours*3600 + sub.start.minutes*60 + sub.start.seconds)*1000 + sub.start.milliseconds, "end_ms": (sub.end.hours*3600 + sub.end.minutes*60 + sub.end.seconds)*1000 + sub.end.milliseconds, "text": sub.text}
            except Exception: time.sleep(3)
    except Exception: pass
    return None

# ==============================================================================
# ===========================   FLASK API & PROXY   ===============================
# ==============================================================================
app = Flask(__name__)
CORS(app)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
PROXY_SESSION = requests.Session()

@app.route('/dub', methods=['GET'])
def dub_srt_endpoint():
    srt_url = request.args.get('url')
    if not srt_url: return jsonify({"error": "Missing URL"}), 400
    job_id, job_dir = str(uuid.uuid4()), os.path.join(OUTPUT_DIR, str(uuid.uuid4()))
    os.makedirs(job_dir)
    subs = pysrt.from_string(requests.get(srt_url, timeout=20).text)
    voices = ["dan", "nova"] * len(subs)
    def generate():
        batch = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for i, future in enumerate(concurrent.futures.as_completed({ex.submit(tts_process_line, i, sub, voices[i], job_dir): i for i, sub in enumerate(subs)})):
                if res := future.result():
                    batch.append({"audio_url": url_for('serve_dubbed_audio', job_id=job_id, filename=os.path.basename(res["file_path"]), _external=True), **res})
                    if len(batch) >= 5: yield json.dumps({"batch": batch}, ensure_ascii=False) + "\n"; batch.clear()
            if batch: yield json.dumps({"batch": batch}, ensure_ascii=False) + "\n"
    return Response(stream_with_context(generate()), mimetype='application/json')

@app.route('/audio/<job_id>/<filename>')
def serve_dubbed_audio(job_id, filename):
    return send_from_directory(os.path.join(OUTPUT_DIR, job_id), filename)

# 🌟 البروكسي الداخلي السريع
@app.route('/proxy', methods=["GET", "HEAD", "OPTIONS"])
def proxy():
    if request.method == "OPTIONS":
        resp = Response(status=204)
        resp.headers.update({"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS", "Access-Control-Allow-Headers": "*"})
        return resp
    target_url = request.args.get('url')
    if not target_url: return "Missing url", 400

    proxy_headers = {h: request.headers[h] for h in ['User-Agent', 'Accept'] if h in request.headers}
    proxy_headers["Accept-Encoding"] = "identity"
    
    # 🌟 تخطي حماية MovieBox للروابط M3U8 و MP4
    if any(x in target_url for x in ['hakunaymatata.com', 'bcdnxw', 'valiw.']):
        proxy_headers.update({'Referer': 'https://fmoviesunblocked.net/', 'Origin': 'https://fmoviesunblocked.net'})
    elif 'tgtria1dbw.xyz' in target_url: proxy_headers['Referer'] = 'https://veloratv.ru/'
    elif 'vidmoly' in target_url or 'sendvid' in target_url: proxy_headers['Referer'] = 'https://ristoanime.org/'

    try:
        r = PROXY_SESSION.request(request.method, target_url, headers=proxy_headers, stream=True, timeout=20, verify=False, allow_redirects=True)
        resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in {'connection', 'keep-alive', 'transfer-encoding', 'content-encoding'}}
        resp_headers.update({'Access-Control-Allow-Origin': '*', 'Access-Control-Expose-Headers': '*'})
        if request.method == "HEAD": return Response(status=r.status_code, headers=resp_headers)
        
        if 'mpegurl' in r.headers.get('content-type', '').lower():
            def m3u8_stream():
                for line in r.iter_lines():
                    l = line.decode('utf-8', 'ignore')
                    if l and not l.startswith('#'): yield f"{request.host_url.rstrip('/')}/proxy?url={quote_plus(urljoin(target_url, l.strip()))}\n"
                    elif l: yield f"{l}\n"
            return Response(m3u8_stream(), headers=resp_headers, status=r.status_code)
        
        def mp4_stream():
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk: yield chunk
        return Response(stream_with_context(mp4_stream()), headers=resp_headers, status=r.status_code)
    except Exception as e: return str(e), 502

@app.route('/scrape', methods=['GET'])
def scrape_endpoint():
    provider, title, media_type, tmdb_id = request.args.get('provider', '').lower(), request.args.get('title'), request.args.get('type'), request.args.get('tmdb_id')
    if not provider or not media_type: return jsonify({"error": "Missing provider or type"}), 400
    try: season, episode = int(request.args.get('season') or 0), int(request.args.get('episode') or 0)
    except ValueError: return jsonify({"error": "Invalid season/episode"}), 400

    pmap = {
        'akwam': lambda: scrape_akwam(title, media_type, season, episode),
        'veloratv': lambda: scrape_veloratv(media_type, season or 1, episode or 1, tmdb_id),
        'aflam': lambda: scrape_aflam(title, media_type, episode),
        'ristoanime': lambda: scrape_ristoanime(title, season, episode),
        'arabic-toons': lambda: scrape_arabic_toons(title, season, episode),
        'tmdb': lambda: scrape_tmdb(media_type, tmdb_id, season, episode),
        'moviebox': lambda: scrape_moviebox(title, media_type, season, episode)
    }
    if provider not in pmap: return jsonify({"error": "Unknown provider"}), 400
    res = pmap[provider]()
    if res.get('status') == 'success' and res.get('links'):
        for link in res['links']:
            if link.pop('needs_proxy', False): link['url'] = f"{request.host_url.rstrip('/')}/proxy?url={quote_plus(link['url'])}"
    return jsonify(res), 200 if res.get('status') == 'success' else 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
