# --- ملف: main.py (النسخة النهائية للاستضافة السحابية - Production Ready) ---

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

# --- مكتبات التشغيل الآلي للمتصفح (لـ VeloraTV) ---
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sys.stderr.write("WARN: Playwright not installed. The 'veloratv' provider will not work.\n")
    class PlaywrightTimeoutError(Exception): pass
    async_playwright = None

# --- مكتبة الذكاء الاصطناعي ---
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

# --- مكتبات إضافية للمزود الجديد ---
from fake_useragent import UserAgent
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- مكتبات إضافية لمزود الدبلجة ---
import pysrt
from pydub import AudioSegment
from gradio_client import Client

# --- [تهيئة قوية للترميز باللغة العربية] ---
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
except (TypeError, AttributeError):
    pass

# ----- الإعدادات العامة المتغيرة (Environment Variables) -----
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
        sys.stderr.write("INFO: Gemini AI configured successfully.\n")
    except Exception as e:
        sys.stderr.write(f"WARN: Failed to configure Gemini AI. Error: {e}\n")
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
# ========================   PROVIDER 1: AKWAM   ===============================
# ==============================================================================
def akwam_make_request(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        return BeautifulSoup(response.text, 'html.parser')
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"[!] AKWAM-LOG: Request error for URL: {url}\n{e}\n")
        return None

def select_best_match_with_gemini(user_query, media_type, target_season, all_results):
    if not GEMINI_AVAILABLE: return all_results[0] if all_results else None
    model = genai.GenerativeModel('gemini-1.5-flash')
    formatted_results = "\n".join([f"id:{i}, title:\"{res['title']}\", url:\"{res['url']}\"" for i, res in enumerate(all_results)])
    prompt = f"""You are an intelligent search result selector. Find the single best match from a list of search results.
USER'S REQUEST:
Title: "{user_query}"
Type: "{media_type}"
Requested Season: {target_season or 'N/A (This is a movie)'}
SEARCH RESULTS:
{formatted_results}
INSTRUCTIONS:
For 'series': Your PRIMARY goal is to match the 'Requested Season'.
For 'movie': Find the result that most closely matches the movie title.
Output: MUST be a single JSON object with the ID. Example: {{"best_choice_id": <id_number>}}
"""
    try:
        response = model.generate_content(prompt)
        json_text = re.search(r'\{.*\}', response.text, re.DOTALL).group(0)
        decision = json.loads(json_text)
        best_id = int(decision.get('best_choice_id'))
        if 0 <= best_id < len(all_results): return all_results[best_id]
        else: raise ValueError(f"Invalid ID: {best_id}")
    except Exception as e:
        sys.stderr.write(f"ERROR: AKWAM-LOG: Gemini analysis failed: {e}\n")
        return all_results[0] if all_results else None

def akwam_get_video_links_from_player(content_page_url):
    soup = akwam_make_request(content_page_url)
    if not soup: return []
    final_video_links, watch_page_urls = set(), []
    quality_tabs = soup.find_all('div', class_='tab-content quality')
    AKWAM_BASE_URL = "https://ak.sv"
    for tab in quality_tabs:
        watch_link_tag = tab.find('a', class_='link-show')
        if watch_link_tag and 'href' in watch_link_tag.attrs:
            watch_id = watch_link_tag['href'].split('/')[-1]
            try:
                url_parts = content_page_url.split('/')
                content_id, content_slug = url_parts[-2], url_parts[-1]
                watch_page_urls.append(f"{AKWAM_BASE_URL}/watch/{watch_id}/{content_id}/{content_slug}")
            except IndexError: continue
    if not watch_page_urls: return []
    for url in set(watch_page_urls):
        player_soup = akwam_make_request(url)
        if not player_soup: continue
        video_tag = player_soup.find('video', id='player')
        if video_tag:
            for source in video_tag.find_all('source'):
                if source.get('src'):
                    final_video_links.add((source.get('size', 'N/A'), source['src']))
    return [{"quality": quality, "url": link, "needs_proxy": False} for quality, link in sorted(list(final_video_links), key=lambda x: int(x[0]) if x[0].isdigit() else 0, reverse=True)]

def akwam_find_episode_on_season_page(season_url, episode_number):
    season_soup = akwam_make_request(season_url)
    if not season_soup: return None
    episodes_map = {}
    episode_containers = season_soup.find_all('div', class_='bg-primary2')
    episode_pattern = re.compile(r'(?:الحلقة|حلقة)\s*(\d{1,3})', re.IGNORECASE)
    for container in episode_containers:
        h2_tag = container.find('h2')
        if not h2_tag: continue
        title_tag = h2_tag.find('a')
        if title_tag and title_tag.get('href'):
            full_title = ' '.join(title_tag.text.strip().split())
            match = episode_pattern.search(full_title)
            if match: episodes_map[int(match.group(1))] = {'url': title_tag['href'], 'title': full_title}
    found_episode = episodes_map.get(episode_number)
    return found_episode['url'] if found_episode else None

def scrape_akwam(query, media_type, season_num, episode_num):
    AKWAM_BASE_URL = "https://ak.sv"
    search_query_encoded = urllib.parse.quote_plus(query)
    all_search_results, current_page = [], 1
    while True:
        search_url = f"{AKWAM_BASE_URL}/search?q={search_query_encoded}&page={current_page}"
        search_soup = akwam_make_request(search_url)
        if not search_soup: break
        results_on_page = [{'title': tag.text.strip(), 'url': tag['href']} for entry in search_soup.select('div.widget-body div.entry-box-1') if (tag := entry.find('h3', class_='entry-title').find('a')) and tag.get('href')]
        if not results_on_page: break
        all_search_results.extend(results_on_page)
        pagination_nav = search_soup.find('nav', attrs={'aria-label': 'Page navigation'})
        if pagination_nav and pagination_nav.find('a', class_='page-link', string=re.compile(r'التالي')): current_page += 1
        else: break
    if not all_search_results: return {"status": "error", "message": f"No search results found for '{query}' on Akwam."}
    selected_content = select_best_match_with_gemini(query, media_type, season_num, all_search_results)
    if not selected_content: return {"status": "error", "message": "AI could not determine the best match from search results on Akwam."}
    content_url = selected_content['url']
    if media_type == 'movie':
        links = akwam_get_video_links_from_player(content_url)
        return {"status": "success", "links": links} if links else {"status": "error", "message": "No direct video links found for this movie on Akwam."}
    elif media_type == 'series':
        episode_url = akwam_find_episode_on_season_page(content_url, episode_num)
        if not episode_url: return {"status": "error", "message": f"Could not find episode {episode_num} in the selected season on Akwam."}
        links = akwam_get_video_links_from_player(episode_url)
        return {"status": "success", "links": links} if links else {"status": "error", "message": f"No direct video links found for episode {episode_num} on Akwam."}
    return {"status": "error", "message": "Unknown content type for Akwam."}

# ==============================================================================
# ========================   PROVIDER 2: VELORATV   ============================
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
        if not m3u8_links: return {"status": "error", "message": "No m3u8 links found on VeloraTV."}
        response = {"status": "success", "links": [{"quality": "proxied_m3u8", "url": link, "needs_proxy": True} for link in m3u8_links]}
        if subtitle_links: response["subtitles"] = [{"lang": "ar", "url": sub} for sub in subtitle_links]
        return response
    except Exception as e:
        return {"status": "error", "message": f"VeloraTV provider error: {e}"}

# ==============================================================================
# ========================   PROVIDER 3: AFLAM   ===============================
# ==============================================================================
def aflam_get_best_match(query, results):
    titles = list(results.keys())
    best_matches = difflib.get_close_matches(query, titles, n=1, cutoff=0.5)
    if best_matches: return {'title': best_matches[0], 'url': results[best_matches[0]]}
    return None

def aflam_get_video_servers(content_url, session):
    links = []
    try:
        post_headers = HEADERS.copy()
        post_headers['Referer'] = content_url
        response = session.post(content_url, headers=post_headers, data={'watch': '1'}, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        servers_list = soup.find('ul', id='watch-servers-list')
        if not servers_list: return []
        for server_item in servers_list.find_all('li'):
            encoded_url = server_item.get('data-encoded')
            if not encoded_url: continue
            try:
                iframe_src = base64.b64decode(encoded_url).decode('utf-8')
                command = ['yt-dlp', '-g', '--no-warnings', iframe_src]
                result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8', timeout=45)
                direct_link = result.stdout.strip().split('\n')[0]
                if direct_link.startswith('http'): links.append({"quality": "Direct MP4", "url": direct_link, "needs_proxy": False})
            except Exception: pass
    except requests.exceptions.RequestException: pass
    return links

def aflam_handle_series(page_soup, episode_num, session):
    episode_links = page_soup.select('div.EpisodesArea div.bg-primary2 h2 a')
    episode_pattern = re.compile(r'(?:الحلقة|حلقة)\s(\d+)')
    for link_tag in episode_links:
        title = link_tag.get_text(strip=True)
        match = episode_pattern.search(title)
        if match and int(match.group(1)) == episode_num: return aflam_get_video_servers(link_tag['href'], session)
    return []

def scrape_aflam(query, media_type, episode_num):
    AFLAM_BASE_URL = "https://afllam.onl"
    session = requests.Session()
    session.headers.update(HEADERS)
    search_url = f"{AFLAM_BASE_URL}/?s={urllib.parse.quote(query)}"
    try:
        response = session.get(search_url, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        entries = soup.select('div.widget-body .entry-box-1')
        if not entries: return {"status": "error", "message": "No search results on Aflam.onl."}
        results_map = {entry.select_one('h3.entry-title a').text.strip(): entry.select_one('h3.entry-title a')['href'] for entry in entries if entry.select_one('h3.entry-title a') and entry.select_one('h3.entry-title a').has_attr('href')}
        best_match = aflam_get_best_match(query, results_map)
        if not best_match: return {"status": "error", "message": "No close match found in search results on Aflam.onl."}
        page_url = best_match['url']
        response = session.get(page_url, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        links = aflam_handle_series(soup, episode_num, session) if soup.find('div', class_='EpisodesArea') else aflam_get_video_servers(page_url, session)
        if links: return {"status": "success", "links": links}
        else: return {"status": "error", "message": "Could not extract final video links from Aflam.onl."}
    except Exception as e:
        return {"status": "error", "message": f"An error occurred with Aflam.onl provider: {e}"}

# ==============================================================================
# ========================   PROVIDER 4: RISTOANIME   ==========================
# ==============================================================================
def risto_extract_stream_link(embed_url, referer_url):
    if not embed_url or not referer_url: return None
    command = ['yt-dlp', '-g', '--referer', referer_url, embed_url]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8', timeout=60)
        for link in reversed(result.stdout.strip().split('\n')):
            if "m3u8" in link or "mp4" in link: return link
    except Exception: pass
    return None

def scrape_ristoanime(query, season_num, episode_num):
    RISTO_BASE_URL = "https://ristoanime.org"
    RISTO_AJAX_URL = f"{RISTO_BASE_URL}/wp-content/themes/TopAnime/Ajaxt/Single/Episodes.php"
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        search_url = f"{RISTO_BASE_URL}/?s={urllib.parse.quote_plus(query)}"
        res = session.get(search_url, timeout=15)
        res.raise_for_status()
        search_results = BeautifulSoup(res.text, 'html.parser').select('div.MovieItem a')
        if not search_results: return {"status": "error", "message": "Anime not found on Ristoanime."}
        res = session.get(search_results[0]['href'], timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        episodes_html, season_found = "", False
        for tab in soup.select('div.SeasonsList ul li a'):
            if re.compile(fr'(الموسم|موسم)\s*{season_num}').search(tab.get_text(strip=True)):
                ajax_res = session.post(RISTO_AJAX_URL, data={'season': tab['data-season']}, timeout=15)
                ajax_res.raise_for_status()
                episodes_html, season_found = ajax_res.text, True
                break
        if not season_found and (ep_list := soup.select_one('div.EpisodesList')): episodes_html = str(ep_list)
        if not episodes_html: return {"status": "error", "message": f"Could not find season {season_num}."}
        
        episode_url = None
        for link in BeautifulSoup(episodes_html, 'html.parser').select('a'):
            match = re.compile(r'(?:الحلقة|حلقة)\s*(\d+)').search(link.get_text(strip=True))
            if match and int(match.group(1)) == episode_num:
                episode_url = link['href']
                break
        if not episode_url: return {"status": "error", "message": f"Could not find episode {episode_num}."}
        
        watch_page_url = episode_url.strip('/') + '/watch/'
        res = session.get(watch_page_url, timeout=15)
        res.raise_for_status()
        server = BeautifulSoup(res.text, 'html.parser').select_one('ul#watch li[data-watch="sendvid.com"], ul#watch li[data-watch*="vidmoly.net"], ul#watch li[data-watch]')
        if not server: return {"status": "error", "message": "No watch servers found on the page."}
        
        final_link = risto_extract_stream_link(server['data-watch'], watch_page_url)
        if final_link: return {"status": "success", "links": [{"quality": "proxied_m3u8", "url": final_link, "needs_proxy": True}]}
        return {"status": "error", "message": "Failed to extract final stream link using yt-dlp."}
    except Exception as e:
        return {"status": "error", "message": f"An error occurred with Ristoanime provider: {e}"}

# ==============================================================================
# ====================   PROVIDER 5: ARABIC-TOONS   ============================
# ==============================================================================
ATOONS_BASE_URL = "https://www.arabic-toons.com/"
ATOONS_WORKER_URL = "https://snowy-term-f692.itsyassine16.workers.dev/"
atoons_ua = UserAgent()

def atoons_create_robust_session():
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504]))
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({'User-Agent': atoons_ua.random, 'Referer': ATOONS_BASE_URL})
    return session

def scrape_arabic_toons(query, season_num, episode_num):
    session = atoons_create_robust_session()
    try:
        search_html = session.get(ATOONS_WORKER_URL, params={"url": f"{ATOONS_BASE_URL}livesearch.php?q={urllib.parse.quote(query)}"}, timeout=20).text
        search_results = BeautifulSoup(search_html, 'html.parser').find_all('a', class_='list-group-item')
        if not search_results: return {"status": "error", "message": "No results."}
        
        best_match = {'path': None, 'score': -1}
        for item in search_results:
            title = item.get_text(strip=True).replace(item.find('span').get_text(strip=True), '').strip()
            score = difflib.SequenceMatcher(None, query, title).ratio()
            match = re.compile(r'(?:الموسم|الجزء|موسم|جزء)\s*(\d+)').search(title)
            if match and int(match.group(1)) == season_num: score += 1.0
            elif season_num == 1: score += 0.8
            if score > best_match['score']: best_match.update({'score': score, 'path': item['href']})
            
        if not best_match['path']: return {"status": "error", "message": "Season not found."}
        episodes_html = session.get(ATOONS_WORKER_URL, params={"url": ATOONS_BASE_URL + best_match['path']}, timeout=20).text
        movies_container = BeautifulSoup(episodes_html, 'html.parser').find('div', class_='moviesBlocks')
        if not movies_container: return {"status": "error", "message": "Episodes container not found."}
        
        selected_episode_path = None
        for episode_div in movies_container.find_all('div', class_='movie'):
            link_tag = episode_div.find('a')
            if not link_tag: continue
            name_tag = link_tag.find('div', class_='badge-overd')
            if name_tag and re.compile(r'(\d+)').search(name_tag.get_text(strip=True)) and int(re.compile(r'(\d+)').search(name_tag.get_text(strip=True)).group(1)) == episode_num:
                selected_episode_path = link_tag['href']
                break
                
        if not selected_episode_path: return {"status": "error", "message": "Episode not found."}
        
        resp = session.get(ATOONS_BASE_URL + selected_episode_path, timeout=20).text
        if m := re.search(r'yB0hQ\s=\s*\'([^\']+.m3u8[^\']*)\'', resp): m3u8_link = m.group(1)
        elif m := re.search(r'x9zFqV3\s*=\s*{([^}]+)}', resp):
            parts = dict(re.findall(r'(\w+):\s*"([^"]+)"', m.group(1)))
            m3u8_link = f"{parts['jC1kO']}://{parts['hF3nV']}/{parts['iA5pX']}?{parts['tN4qY']}" if all(k in parts for k in ("jC1kO", "hF3nV", "iA5pX", "tN4qY")) else None
        else: m3u8_link = None
        
        if m3u8_link: return {"status": "success", "links": [{"quality": "Direct M3U8", "url": m3u8_link, "needs_proxy": False}]}
        return {"status": "error", "message": "Failed to extract m3u8."}
    except Exception as e:
        return {"status": "error", "message": f"Arabic-Toons error: {e}"}

# ==============================================================================
# ========================   PROVIDER 6: SUBTITLES   ===========================
# ==============================================================================
def get_subtitles_from_wyzie(content_type, tmdb_id, season=None, episode=None):
    if content_type == "movie": url = f"https://sub.wyzie.ru/search?id={tmdb_id}&format=srt"
    elif content_type == "tv" and season and episode: url = f"https://sub.wyzie.ru/search?id={tmdb_id}&season={season}&episode={episode}&format=srt"
    else: return {"status": "error", "message": "Invalid type or missing season/episode"}
    try:
        resp = requests.get(url, headers={"user-agent": HEADERS['User-Agent']}, timeout=15)
        resp.raise_for_status()
        return {"status": "success", "requested_url": url, "response_data": resp.json() if 'application/json' in resp.headers.get('content-type', '') else resp.text}
    except requests.exceptions.RequestException as e:
        return {"status": "error", "message": f"Failed to fetch subtitles: {e}"}

# ==============================================================================
# ========================   PROVIDER 7: TMDB   ================================
# ==============================================================================
def scrape_tmdb(media_type, tmdb_id, season=None, episode=None):
    endpoint = f"{TMDB_BACKEND_URL}/movie/{tmdb_id}" if media_type == 'movie' else f"{TMDB_BACKEND_URL}/tv/{tmdb_id}?s={season}&e={episode}"
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(endpoint, timeout=60, verify=False)
        response.raise_for_status()
        data = response.json()
        if 'files' not in data or not data['files']: return {"status": "error", "message": "No media files found."}
        links = [{"quality": "MP4" if f['type'] == 'mp4' else "HLS", "url": f['file'], "needs_proxy": False} for f in data['files']]
        result = {"status": "success", "links": links}
        if 'subtitles' in data and data['subtitles']: result["subtitles"] = [{"lang": s.get('lang', 'en'), "url": s['url']} for s in data['subtitles']]
        return result
    except Exception as e:
        return {"status": "error", "message": f"TMDB error: {e}"}

# ==============================================================================
# ========================   PROVIDER 8: MOVIEBOX   ============================
# ==============================================================================
def scrape_moviebox(query, media_type, season_num, episode_num):
    sys.stderr.write(f"[*] MOVIEBOX-LOG: Starting scrape for '{query}'...\n")
    session = requests.Session()
    
    # 🌟 إرسال هيدرز وتوكنز وهمية لتبدو كأن الطلب من متصفح شرعي، وهذا يحل مشكلة 403 في الاستضافات السحابية
    fake_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1aWQiOjc2OTU3MzE5ODU3Mjk2MDg1MjAsImF0cCI6MywiZXh0IjoiMTc3ODc3NDc1MiIsImV4cCI6MTc4NjU1MDc1MiwiaWF0IjoxNzc4Nzc0NDUyfQ.DNO0J8lH7m650oAsiiib9dVUrFcceOqICkt_OIxACNE"
    fake_mb_token = "%22eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1aWQiOjY3ODkwODY3OTUzMTczNzIwMDgsImF0cCI6MywiZXh0IjoiMTc4Mjc2NzQ0MiIsImV4cCI6MTc5MDU0MzQ0MiwiaWF0IjoxNzgyNzY3MTQyfQ.yTcmzVCKaX1IJkBqI01EgmTJhfYrJp-yXTZaO60BTog%22"
    
    session.headers.update({
        'User-Agent': HEADERS['User-Agent'],
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Origin': 'https://netfilm.world',
        'Referer': 'https://netfilm.world/',
        'x-client-info': '{"timezone":"Africa/Casablanca"}',
        'x-user': f'{{"token":"{fake_token}","userId":"7695731985729608520","userType":0,"appType":3}}',
        'Cookie': f'token={fake_token}; mb_token={fake_mb_token}'
    })
    
    search_url = f"https://moviebox.ph/web/searchResult?keyword={urllib.parse.quote_plus(query)}"
    try:
        res = session.get(search_url, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        cards = soup.find_all('a', href=re.compile(r'^/moviedetail/'))
        if not cards: return {"status": "error", "message": f"MovieBox: No search results found for '{query}'."}

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
                matched_lower = best_matches[0]
                for t, p in filtered_results.items():
                    if t.lower() == matched_lower: best_title, detail_path = t, p; break
            else:
                for t, p in filtered_results.items():
                    if query_lower in t.lower(): best_title, detail_path = t, p; break
                if not best_title: best_title, detail_path = list(filtered_results.items())[0]
    except Exception as e:
        return {"status": "error", "message": f"MovieBox: HTML Search failed. {e}"}

    subject_id = None
    try:
        detail_res = session.get(f"https://h5-api.aoneroom.com/wefeed-h5api-bff/detail?detailPath={detail_path}", timeout=15)
        detail_res.raise_for_status()
        subject_id = detail_res.json().get('data', {}).get('subject', {}).get('subjectId')
        if not subject_id: return {"status": "error", "message": "MovieBox: Failed to get subjectId."}
    except Exception as e:
        return {"status": "error", "message": f"MovieBox: Detail fetch failed. {e}"}

    links, stream_id_for_subs = [], None
    try:
        se = season_num if media_type == 'series' and season_num else 0
        ep = episode_num if media_type == 'series' and episode_num else 0
        
        session.headers.update({'Referer': f'https://netfilm.world/spa/videoPlayPage/movies/{detail_path}?id={subject_id}&detailSe=&detailEp=&lang=en&type=/movie/detail'})

        play_res = session.get(f"https://netfilm.world/wefeed-h5api-bff/subject/play?subjectId={subject_id}&se={se}&ep={ep}&detailPath={detail_path}", timeout=15)
        play_res.raise_for_status()
        data = play_res.json().get('data', {})
        
        if (not data or not data.get('hasResource')) and media_type == 'series':
             play_res = session.get(f"https://netfilm.world/wefeed-h5api-bff/subject/play?subjectId={subject_id}&se=0&ep={ep}&detailPath={detail_path}", timeout=15)
             play_res.raise_for_status()
             data = play_res.json().get('data', {})

        if not data or not data.get('hasResource'): return {"status": "error", "message": "MovieBox: No streams available."}

        # 🌟 تفعيل البروكسي (needs_proxy = True) لروابط سيرفرات MovieBox لتخطي حظر (CORS & Hotlinking)
        for stream in data.get('dash', []) or data.get('hls', []):
            if stream.get('url'): links.append({"quality": f"{stream.get('format', 'HLS')} - {stream.get('resolutions', 'HD')}", "url": stream['url'], "needs_proxy": True})
        for stream in data.get('streams', []):
            if stream.get('url'):
                stream_id_for_subs = stream.get('id')
                links.append({"quality": f"{stream.get('format', 'MP4')} - {stream.get('resolutions', 'HD')} - {format_bytes(stream.get('size')) or 'Unknown'}", "url": stream['url'], "needs_proxy": True})
        if not links: return {"status": "error", "message": "MovieBox: No valid stream URLs were extracted."}
    except Exception as e:
        return {"status": "error", "message": f"MovieBox: Play API fetch failed. {e}"}

    all_subtitles = []
    if stream_id_for_subs:
        try:
            sub_res = session.get(f"https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/caption?format=MP4&id={stream_id_for_subs}&subjectId={subject_id}&detailPath={detail_path}", timeout=15)
            if sub_res.status_code == 200:
                for cap in sub_res.json().get('data', {}).get('captions', []):
                    if cap.get('url') and cap.get('lan'): all_subtitles.append({"lang": cap['lan'], "url": cap['url']})
        except Exception: pass

    final_result = {"status": "success", "links": links}
    if all_subtitles: final_result["subtitles"] = all_subtitles
    return final_result

# ==============================================================================
# =====================   PROVIDER 9: DUBBING (TTS)   ==========================
# ==============================================================================
OUTPUT_DIR = "output_audio"
os.makedirs(OUTPUT_DIR, exist_ok=True)
tts_client = None
try:
    tts_client = Client("NihalGazi/Text-To-Speech-Unlimited")
except Exception as e:
    sys.stderr.write(f"❌ [DUBBING-LOG] Failed to connect to Text-To-Speech service: {e}\n")

male_voices = ["dan", "onyx", "verse", "ash", "amuch"]
female_voices = ["nova", "fable", "coral", "shimmer", "ballad"]

def analyze_dialogue_with_gemini(srt_content):
    if not GEMINI_AVAILABLE: return None
    try:
        subs = pysrt.from_string(srt_content)
        dialogue_text = "\n".join([f'Line {sub.index}: "{sub.text_without_tags.replace(chr(10), " ")}"' for sub in subs])
        prompt = f"""You are an expert script analyst. Analyze the dialogue and identify speakers. Group lines. Output JSON with "dialogue_analysis": [ {{"line_index": 1, "speaker_id": "Speaker 1"}} ]
---
{dialogue_text}
---"""
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        json_text = re.search(r'(\{.*?\})', response.text, re.DOTALL).group(1)
        return json.loads(json_text)
    except Exception: return None

def tts_to_milliseconds(srt_time):
    return (srt_time.hours * 3600 + srt_time.minutes * 60 + srt_time.seconds) * 1000 + srt_time.milliseconds

def tts_compress_mp3(file_path, bitrate="64k"):
    try:
        audio = AudioSegment.from_file(file_path)
        audio.export(file_path, format="mp3", bitrate=bitrate)
        return file_path
    except Exception: return file_path

def tts_generate_line(text, voice_name, out_path, emotion="neutral", retries=5):
    if not tts_client: raise ConnectionError("TTS client is not available.")
    for attempt in range(1, retries + 1):
        try:
            audio_path, _ = tts_client.predict(text, voice_name, emotion, True, 12345, "", api_name="/text_to_speech_app")
            os.rename(audio_path, out_path)
            return tts_compress_mp3(out_path, bitrate="64k")
        except Exception: time.sleep(3 * attempt)
    return None

def tts_process_line(i, sub, voice_name, job_dir):
    try:
        out_file = os.path.join(job_dir, f"line_{sub.index}.mp3")
        if result_path := tts_generate_line(sub.text, voice_name, out_file):
            return {"file_path": result_path, "start_ms": tts_to_milliseconds(sub.start), "end_ms": tts_to_milliseconds(sub.end), "text": sub.text}
    except Exception: pass
    return None

# ==============================================================================
# ===========================   FLASK API & PROXY   ===============================
# ==============================================================================
app = Flask(__name__)
CORS(app)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# الجلسة المخصصة للبروكسي لدعم البث السريع
PROXY_SESSION = requests.Session()

@app.route('/dub', methods=['GET'])
def dub_srt_endpoint():
    srt_url = request.args.get('url')
    if not srt_url: return jsonify({"error": "Please provide 'url' parameter"}), 400
    job_id, job_dir = str(uuid.uuid4()), os.path.join(OUTPUT_DIR, str(uuid.uuid4()))
    os.makedirs(job_dir)
    try:
        srt_content = requests.get(srt_url, timeout=20).text
        subs = pysrt.from_string(srt_content)
    except Exception as e: return jsonify({"error": str(e)}), 500
    voice_assignments = [male_voices[i % len(male_voices)] if i % 2 == 0 else female_voices[i % len(female_voices)] for i in range(len(subs))]
    def generate_stream():
        total, batch, completed = len(subs), [], 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(tts_process_line, i, sub, voice_assignments[i], job_dir): i for i, sub in enumerate(subs)}
            for future in concurrent.futures.as_completed(futures):
                if result := future.result():
                    batch.append({"audio_url": url_for('serve_dubbed_audio', job_id=os.path.basename(job_dir), filename=os.path.basename(result["file_path"]), _external=True), "start_ms": result["start_ms"], "end_ms": result["end_ms"], "text": result["text"]})
                    completed += 1
                    if len(batch) >= 5: yield json.dumps({"batch": batch, "progress": f"{completed}/{total}"}, ensure_ascii=False) + "\n"; batch.clear()
            if batch: yield json.dumps({"batch": batch, "progress": f"{completed}/{total}"}, ensure_ascii=False) + "\n"
    return Response(stream_with_context(generate_stream()), mimetype='application/json')

@app.route('/audio/<job_id>/<filename>')
def serve_dubbed_audio(job_id, filename):
    return send_from_directory(os.path.join(OUTPUT_DIR, job_id), filename)

# 🌟 البروكسي الداخلي المطور لمعالجة MovieBox (CDN hakunaymatata)
@app.route('/proxy', methods=["GET", "HEAD", "OPTIONS"])
def proxy():
    if request.method == "OPTIONS":
        resp = Response(status=204)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        return resp

    target_url = request.args.get('url')
    if not target_url: return "Missing 'url' parameter", 400

    proxy_headers = {h: request.headers[h] for h in ['User-Agent', 'Accept', 'Accept-Language'] if h in request.headers}
    proxy_headers["Accept-Encoding"] = "identity" # مهم جداً للبث المباشر

    # تخطي حمايات الـ Hotlink للمواقع المختلفة (بما فيها Moviebox)
    if 'tgtria1dbw.xyz' in target_url:
        proxy_headers['Referer'] = 'https://veloratv.ru/'
    elif 'vidmoly.net' in target_url or 'sendvid.com' in target_url:
        proxy_headers['Referer'] = 'https://ristoanime.org/'
    elif 'hakunaymatata.com' in target_url or 'bcdnxw' in target_url or 'valiw.' in target_url:
        proxy_headers['Referer'] = 'https://fmoviesunblocked.net/'
        proxy_headers['Origin'] = 'https://fmoviesunblocked.net'

    try:
        r = PROXY_SESSION.request(
            request.method,
            target_url,
            headers=proxy_headers,
            stream=True,
            timeout=20,
            verify=False,
            allow_redirects=True
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        return f"Error fetching proxied URL: {e}", 502

    hop_by_hop = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'content-encoding'}
    response_headers = {k: v for k, v in r.headers.items() if k.lower() not in hop_by_hop}
    response_headers['Access-Control-Allow-Origin'] = '*'
    response_headers['Access-Control-Expose-Headers'] = '*'

    if request.method == "HEAD":
        return Response(status=r.status_code, headers=response_headers)

    # بث M3U8 وتمرير القطع للبروكسي
    if 'mpegurl' in r.headers.get('content-type', '').lower():
        proxy_base_url = f"{request.host_url.rstrip('/')}/proxy?url="
        def generate_rewritten_playlist():
            for line_bytes in r.iter_lines():
                line = line_bytes.decode('utf-8', errors='ignore')
                if line and not line.startswith('#'):
                    yield f"{proxy_base_url}{quote_plus(urljoin(target_url, line.strip()))}\n"
                elif line: yield f"{line}\n"
        return Response(generate_rewritten_playlist(), headers=response_headers, status=r.status_code)
    
    # بث MP4 بسلاسة (Chunk Streaming)
    else:
        def generate():
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk: yield chunk
        return Response(stream_with_context(generate()), headers=response_headers, status=r.status_code)

@app.route("/subs", methods=["GET"])
def subtitles_endpoint():
    content_type, tmdb_id = request.args.get("type"), request.args.get("id")
    if not content_type or not tmdb_id: return jsonify({"status": "error", "message": "يجب إدخال type و id"}), 400
    result = get_subtitles_from_wyzie(content_type, tmdb_id, request.args.get("season"), request.args.get("episode"))
    return jsonify(result), 200 if result.get('status') == 'success' else 404

@app.route('/health/tmdb', methods=['GET'])
def check_tmdb_health():
    try:
        response = requests.get(TMDB_BACKEND_URL, timeout=10)
        if response.status_code == 200: return jsonify({"status": "success", "message": "CinePro Backend is running"})
        else: return jsonify({"status": "warning"})
    except requests.exceptions.RequestException as e:
        return jsonify({"status": "error"}), 503

@app.route('/scrape', methods=['GET'])
def scrape_endpoint():
    provider = request.args.get('provider', '').lower()
    title, media_type = request.args.get('title'), request.args.get('type')
    if not provider: return jsonify({"status": "error", "message": "Missing 'provider'"}), 400
    if not media_type or media_type not in ['movie', 'series']: return jsonify({"status": "error", "message": "Invalid 'type'"}), 400
    try:
        season = int(s) if (s := request.args.get('season')) else None
        episode = int(e) if (e := request.args.get('episode')) else None
    except (ValueError, TypeError): return jsonify({"status": "error", "message": "'season'/'episode' must be integers"}), 400
    
    provider_map = {
        'akwam': {'func': scrape_akwam, 'args': {'query': title, 'media_type': media_type, 'season_num': season, 'episode_num': episode}},
        'veloratv': {'func': scrape_veloratv, 'args': {'media_type': media_type, 'season': season or 1, 'episode': episode or 1, 'tmdb_id': request.args.get('tmdb_id')}},
        'aflam': {'func': scrape_aflam, 'args': {'query': title, 'media_type': media_type, 'episode_num': episode}},
        'ristoanime': {'func': scrape_ristoanime, 'args': {'query': title, 'season_num': season, 'episode_num': episode}},
        'arabic-toons': {'func': scrape_arabic_toons, 'args': {'query': title, 'season_num': season, 'episode_num': episode}},
        'tmdb': {'func': scrape_tmdb, 'args': {'media_type': media_type, 'tmdb_id': request.args.get('tmdb_id'), 'season': season, 'episode': episode}},
        'moviebox': {'func': scrape_moviebox, 'args': {'query': title, 'media_type': media_type, 'season_num': season, 'episode_num': episode}}
    }

    if provider not in provider_map: return jsonify({"status": "error", "message": f"Invalid provider '{provider}'"}), 400
    if provider in ['veloratv', 'tmdb'] and not request.args.get('tmdb_id'): return jsonify({"status": "error", "message": f"'tmdb_id' is required for {provider}"}), 400
    if provider not in ['veloratv', 'tmdb'] and not title: return jsonify({"status": "error", "message": f"'title' is required for {provider}"}), 400

    config = provider_map[provider]
    result = config['func'](**config['args'])

    # توجيه الروابط المحمية عبر مسار `/proxy` المدمج لتعمل بدون مشاكل CORS
    if result.get('status') == 'success' and result.get('links'):
        api_base_url = request.host_url.rstrip('/')
        for link_item in result['links']:
            if link_item.get("needs_proxy") and (original_url := link_item.get('url')):
                link_item['url'] = f"{api_base_url}/proxy?url={quote_plus(original_url)}"
                del link_item["needs_proxy"]
                
    return jsonify(result), 200 if result.get('status') == 'success' else 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
