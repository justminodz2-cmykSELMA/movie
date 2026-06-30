# --- ملف: main.py (النسخة النهائية لتجاوز WAF الخاص بـ MovieBox) ---

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
import random

# --- مكتبات الـ API والبروكسي ---
from flask import Flask, request, jsonify, Response, send_from_directory, url_for, stream_with_context
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# --- مكتبات التشغيل الآلي للمتصفح (لـ VeloraTV) ---
try:
    from playwright.async_api import async_playwright
except ImportError:
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
        return f"{round(size_bytes / p, 2)} {size_name[i]}"
    except: return None

# ==============================================================================
# ========================   PROVIDER 1: AKWAM   ===============================
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
            try:
                url_parts = content_page_url.split('/')
                watch_page_urls.append(f"https://ak.sv/watch/{watch_link_tag['href'].split('/')[-1]}/{url_parts[-2]}/{url_parts[-1]}")
            except IndexError: continue
    for url in set(watch_page_urls):
        player_soup = akwam_make_request(url)
        if not player_soup: continue
        if video_tag := player_soup.find('video', id='player'):
            for source in video_tag.find_all('source'):
                if source.get('src'): final_video_links.add((source.get('size', 'N/A'), source['src']))
    return [{"quality": q, "url": l, "needs_proxy": False} for q, l in sorted(list(final_video_links), key=lambda x: int(x[0]) if x[0].isdigit() else 0, reverse=True)]

def akwam_find_episode_on_season_page(season_url, episode_number):
    season_soup = akwam_make_request(season_url)
    if not season_soup: return None
    episode_pattern = re.compile(r'(?:الحلقة|حلقة)\s*(\d{1,3})', re.IGNORECASE)
    for container in season_soup.find_all('div', class_='bg-primary2'):
        if (title_tag := container.find('a')) and title_tag.get('href'):
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
    if not all_search_results: return {"status": "error", "message": "No search results."}
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
# ========================   PROVIDER 2: VELORATV   ============================
# ==============================================================================
async def velora_async_main(watch_url):
    if not async_playwright: return set(), set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=HEADERS['User-Agent'])
        page = await context.new_page()
        m3u8_links, subtitle_links = set(), set()
        page.on("request", lambda req: (m3u8_links.add(req.url) if ".m3u8" in req.url else None, subtitle_links.add(req.url) if "format=srt" in req.url else None))
        try:
            await page.goto(watch_url, timeout=60000, wait_until='domcontentloaded')
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
# ========================   PROVIDER 3: AFLAM   ===============================
# ==============================================================================
def scrape_aflam(query, media_type, episode_num):
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        response = session.get(f"https://afllam.onl/?s={urllib.parse.quote(query)}", timeout=20)
        soup = BeautifulSoup(response.text, 'html.parser')
        entries = soup.select('div.widget-body .entry-box-1')
        if not entries: return {"status": "error", "message": "No search results."}
        results_map = {entry.select_one('h3.entry-title a').text.strip(): entry.select_one('h3.entry-title a')['href'] for entry in entries if entry.select_one('h3.entry-title a')}
        best_matches = difflib.get_close_matches(query, list(results_map.keys()), n=1, cutoff=0.5)
        if not best_matches: return {"status": "error", "message": "No match."}
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
# ========================   PROVIDER 4: RISTOANIME   ==========================
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
# ====================   PROVIDER 5: ARABIC-TOONS   ============================
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
# ========================   PROVIDER 6: TMDB   ================================
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
# ========================   PROVIDER 7: MOVIEBOX (THE FIX)   ==================
# ==============================================================================
try:
    import cloudscraper
    CLOUDSCRAPER_AVAILABLE = True
except ImportError:
    CLOUDSCRAPER_AVAILABLE = False
    sys.stderr.write("WARN: cloudscraper not installed. Run: pip install cloudscraper\n")

MOVIEBOX_API_HOSTS = [
    "https://h5-api.aoneroom.com",
    "https://netfilm.world",
]


def _build_moviebox_session():
    if CLOUDSCRAPER_AVAILABLE:
        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    else:
        session = requests.Session()

    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="142", "Not(A:Brand";v="24", "Google Chrome";v="142"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "x-client-info": '{"timezone":"Africa/Casablanca"}',
    })

    # Route ALL MovieBox requests through a residential proxy to bypass the
    # 403 that datacenter IPs (Render/Northflank/etc.) get from Cloudflare.
    # Set the env var MOVIEBOX_PROXY, e.g.:
    #   http://user:pass@host:port  or  socks5://user:pass@host:port
    proxy = os.environ.get("MOVIEBOX_PROXY", "").strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
        sys.stderr.write("[MOVIEBOX] using residential proxy.\n")

    return session


def _ensure_moviebox_token(session):
    for c in session.cookies:
        if c.name in ("mb_token", "token"):
            return True

    try:
        session.get(
            "https://netfilm.world/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
            },
            timeout=15,
        )
    except Exception as e:
        sys.stderr.write("[MOVIEBOX] landing page failed: %s\n" % e)

    token_endpoints = [
        "/wefeed-h5api-bff/user/info",
        "/wefeed-h5api-bff/visitor/register",
        "/wefeed-h5api-bff/user/visitor",
    ]
    api_headers = {
        "Origin": "https://netfilm.world",
        "Referer": "https://netfilm.world/",
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    for host in MOVIEBOX_API_HOSTS:
        for ep in token_endpoints:
            try:
                r = session.get(host + ep, headers=api_headers, timeout=15)
                token = None
                try:
                    body = r.json()
                    token = (
                        body.get("data", {}).get("token")
                        or body.get("data", {}).get("user", {}).get("token")
                    )
                except Exception:
                    pass
                if not token:
                    xu = r.headers.get("x-user")
                    if xu:
                        try:
                            token = json.loads(xu).get("token")
                        except Exception:
                            pass
                if token:
                    session.cookies.set("mb_token", '"%s"' % token, domain="netfilm.world")
                    session.cookies.set("mb_token", '"%s"' % token, domain="h5-api.aoneroom.com")
                    session.headers["x-user"] = json.dumps({"token": token, "appType": 3})
                    sys.stderr.write("[MOVIEBOX] guest token acquired.\n")
                    return True
                for c in session.cookies:
                    if c.name in ("mb_token", "token"):
                        return True
            except Exception as e:
                sys.stderr.write("[MOVIEBOX] token endpoint %s failed: %s\n" % (ep, e))
                continue
    return False


def scrape_moviebox(query, media_type, season_num, episode_num):
    sys.stderr.write("[*] MOVIEBOX-LOG: Starting scrape for '%s'...\n" % query)
    session = _build_moviebox_session()

    _ensure_moviebox_token(session)

    api_headers = {
        "Origin": "https://netfilm.world",
        "Referer": "https://netfilm.world/",
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }

    search_url = "https://moviebox.ph/web/searchResult?keyword=%s" % urllib.parse.quote_plus(query)
    try:
        res = session.get(search_url, timeout=20)
        soup = BeautifulSoup(res.text, "html.parser")
        cards = soup.find_all("a", href=re.compile(r"^/moviedetail/"))
        if not cards:
            return {"status": "error", "message": "No search results found for '%s'." % query}

        results_map = {card.find("h2", class_="card-title").text.strip(): card.get("href").split("/")[-1] for card in cards if card.find("h2", class_="card-title")}
        best_title, detail_path = None, None
        query_lower = query.lower().strip()
        filtered_results = {k: v for k, v in results_map.items() if "francaise" not in k.lower()} or results_map

        if media_type == "series" and season_num:
            for t, p in filtered_results.items():
                if t.lower().startswith("%s s%s" % (query_lower, season_num)):
                    best_title, detail_path = t, p
                    break
        if not best_title and media_type == "series":
            for t, p in filtered_results.items():
                if query_lower in t.lower() and re.search(r"s\d+-s\d+", t.lower()):
                    best_title, detail_path = t, p
                    break
        if not best_title:
            for t, p in filtered_results.items():
                if t.lower() == query_lower:
                    best_title, detail_path = t, p
                    break
        if not best_title:
            best_matches = difflib.get_close_matches(query_lower, [k.lower() for k in filtered_results.keys()], n=1, cutoff=0.6)
            if best_matches:
                matched_lower = best_matches[0]
                for t, p in filtered_results.items():
                    if t.lower() == matched_lower:
                        best_title, detail_path = t, p
                        break
            else:
                for t, p in filtered_results.items():
                    if query_lower in t.lower() or t.lower() in query_lower:
                        best_title, detail_path = t, p
                        break
                if not best_title:
                    best_title, detail_path = list(filtered_results.items())[0]
    except Exception as e:
        return {"status": "error", "message": "MovieBox: Search failed. %s" % e}

    subject_id = None
    detail_err = None
    for host in MOVIEBOX_API_HOSTS:
        try:
            detail_api_url = "%s/wefeed-h5api-bff/detail?detailPath=%s" % (host, detail_path)
            detail_res = session.get(detail_api_url, headers=api_headers, timeout=20)
            detail_res.raise_for_status()
            subject_id = detail_res.json().get("data", {}).get("subject", {}).get("subjectId")
            if subject_id:
                break
        except Exception as e:
            detail_err = e
            continue
    if not subject_id:
        return {"status": "error", "message": "MovieBox: Failed to get subjectId. %s" % detail_err}

    links, stream_id_for_subs = [], None
    se = season_num if media_type == "series" and season_num else 0
    ep = episode_num if media_type == "series" and episode_num else 0
    play_headers = api_headers.copy()
    play_headers["Referer"] = "https://netfilm.world/spa/videoPlayPage/movies/%s?id=%s&detailSe=&detailEp=&lang=en&type=/movie/detail" % (detail_path, subject_id)

    data = None
    play_err = None
    for host in MOVIEBOX_API_HOSTS:
        try:
            play_api_url = "%s/wefeed-h5api-bff/subject/play?subjectId=%s&se=%s&ep=%s&detailPath=%s" % (host, subject_id, se, ep, detail_path)
            play_res = session.get(play_api_url, headers=play_headers, timeout=20)
            play_res.raise_for_status()
            data = play_res.json().get("data", {})

            if (not data or not data.get("hasResource")) and media_type == "series":
                play_api_url = "%s/wefeed-h5api-bff/subject/play?subjectId=%s&se=0&ep=%s&detailPath=%s" % (host, subject_id, ep, detail_path)
                play_res = session.get(play_api_url, headers=play_headers, timeout=20)
                data = play_res.json().get("data", {})

            if data and data.get("hasResource"):
                break
        except Exception as e:
            play_err = e
            continue

    if not data or not data.get("hasResource"):
        msg = "MovieBox: No streams available." if not play_err else "MovieBox: Play API fetch failed. %s" % play_err
        return {"status": "error", "message": msg}

    for stream in data.get("dash", []) or data.get("hls", []):
        if stream.get("url"):
            links.append({"quality": "%s - %s" % (stream.get("format", "HLS"), stream.get("resolutions", "HD")), "url": stream["url"], "needs_proxy": True})
    for stream in data.get("streams", []):
        if stream.get("url"):
            stream_id_for_subs = stream.get("id")
            links.append({"quality": "%s - %s - %s" % (stream.get("format", "MP4"), stream.get("resolutions", "HD"), format_bytes(stream.get("size")) or "Unknown"), "url": stream["url"], "needs_proxy": True})
    if not links:
        return {"status": "error", "message": "MovieBox: No valid stream URLs were extracted."}

    all_subtitles = []
    if stream_id_for_subs:
        for host in MOVIEBOX_API_HOSTS:
            try:
                sub_api_url = "%s/wefeed-h5api-bff/subject/caption?format=MP4&id=%s&subjectId=%s&detailPath=%s" % (host, stream_id_for_subs, subject_id, detail_path)
                sub_res = session.get(sub_api_url, headers=api_headers, timeout=15)
                if sub_res.status_code == 200:
                    for cap in sub_res.json().get("data", {}).get("captions", []):
                        if cap.get("url") and cap.get("lan"):
                            all_subtitles.append({"lang": cap["lan"], "url": cap["url"]})
                    if all_subtitles:
                        break
            except Exception:
                continue

    final_result = {"status": "success", "links": links}
    if all_subtitles:
        final_result["subtitles"] = all_subtitles
    return final_result

# ==============================================================================
# ========================   PROVIDER 8: SUBTITLES   ===========================
# ==============================================================================
def get_subtitles_from_wyzie(content_type, tmdb_id, season=None, episode=None):
    url = f"https://sub.wyzie.ru/search?id={tmdb_id}&format=srt" if content_type == "movie" else f"https://sub.wyzie.ru/search?id={tmdb_id}&season={season}&episode={episode}&format=srt"
    try:
        resp = requests.get(url, headers={"user-agent": HEADERS['User-Agent']}, timeout=15)
        return {"status": "success", "response_data": resp.json() if 'application/json' in resp.headers.get('content-type', '') else resp.text}
    except Exception as e: return {"status": "error", "message": str(e)}

# ==============================================================================
# =====================   PROVIDER 9: DUBBING (TTS)   ==========================
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
    
    # تجاوز حماية MovieBox / hakunaymatata للبروكسي
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
