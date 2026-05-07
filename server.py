#!/usr/bin/env python3
"""
Signal Intelligence Dashboard — Algorithmic Engine
Scoring, clustering, extractive summaries, velocity — zero mandatory AI calls.
Claude used only on explicit user request.
"""

import hashlib, json, logging, math, os, re, sys, threading, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests, feedparser
from flask import Flask, jsonify, render_template_string, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-7s  %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

_DATA_DIR   = Path(os.environ.get('SIGNAL_DATA_DIR', str(Path.home() / '.signal-dashboard')))
CONFIG_PATH = _DATA_DIR / 'config.json'
DATA_PATH   = _DATA_DIR / 'signals.json'
BRIEF_PATH  = _DATA_DIR / 'weekly_brief.json'

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try: return json.loads(CONFIG_PATH.read_text())
        except: pass
    return {'ntfy_topic': os.environ.get('NTFY_TOPIC', ''),
            'anthropic_api_key': os.environ.get('ANTHROPIC_API_KEY', ''),
            'port': int(os.environ.get('PORT', os.environ.get('SIGNAL_DASHBOARD_PORT', 8765))),
            'breaking_threshold': 8}

CFG = load_config()

# ── Signal store ───────────────────────────────────────────────────────────────

signals: dict[str, dict] = {}
alerts_sent: set[str]    = set()
_lock = threading.Lock()
_velocity: dict[str, list[tuple[float, int]]] = defaultdict(list)  # id → [(ts, score)]

def save_signals():
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        DATA_PATH.write_text(json.dumps(list(signals.values()), default=str, indent=2))

def load_signals():
    if DATA_PATH.exists():
        try:
            with _lock:
                for item in json.loads(DATA_PATH.read_text()):
                    signals[item['id']] = item
            log.info(f'Loaded {len(signals)} cached signals')
        except Exception as e: log.warning(f'Cache: {e}')

# ── Stop words ─────────────────────────────────────────────────────────────────

STOP = {
    'the','a','an','is','are','was','were','be','been','being','have','has','had',
    'do','does','did','will','would','could','should','may','might','can','to','of',
    'in','for','on','with','at','by','from','as','into','through','during','before',
    'after','above','below','between','out','off','over','under','again','then','once',
    'and','but','or','nor','so','yet','both','either','neither','not','this','that',
    'these','those','i','you','he','she','it','we','they','what','which','who','whom',
    'whose','when','where','why','how','all','each','every','any','few','more','most',
    'other','some','such','no','only','own','same','than','too','very','just','new',
    'also','about','here','there','its','our','their','your','his','her','my','its',
    'get','got','say','said','says','use','used','uses','make','made','makes','one',
    'two','three','first','second','last','now','well','like','see','look','come',
    'go','take','know','think','need','want','give','show','find','tell','seem',
}

def tokenize(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r'\b[a-zA-Z]\w{2,}\b', text)
            if w.lower() not in STOP]

# ── TF-IDF Extractive Summariser ───────────────────────────────────────────────

def extractive_summary(text: str, n: int = 2) -> str:
    """Pure Python TF-IDF extractive summary. No AI. No deps beyond stdlib."""
    if not text or len(text) < 120:
        return text or ''
    text = re.sub(r'\s+', ' ', text).strip()
    # Split sentences
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if len(s.strip()) > 30]
    if len(sentences) <= n:
        return ' '.join(sentences)

    sw = [tokenize(s) for s in sentences]
    N  = len(sentences)

    # DF
    df: Counter = Counter()
    for words in sw:
        for w in set(words): df[w] += 1

    # IDF (smoothed)
    idf = {w: math.log((N + 1) / (df[w] + 1)) + 1 for w in df}

    scores = []
    for i, (s, words) in enumerate(zip(sentences, sw)):
        if not words:
            scores.append(0.0); continue
        tf = Counter(words)
        tfidf = sum((tf[w] / len(words)) * idf.get(w, 0) for w in words)
        # First-sentence bias
        pos_w = 1.2 if i == 0 else (0.9 if i == len(sentences) - 1 else 1.0)
        scores.append(tfidf * pos_w)

    top = sorted(sorted(range(len(scores)), key=lambda i: -scores[i])[:n])
    return ' '.join(sentences[i] for i in top)

# ── Velocity ───────────────────────────────────────────────────────────────────

def record_velocity(sig_id: str, engagement: int):
    now = time.time()
    _velocity[sig_id].append((now, engagement))
    # Keep only last 4h
    _velocity[sig_id] = [(t, e) for t, e in _velocity[sig_id] if now - t < 14400]

def velocity_score(sig_id: str) -> float:
    """Returns engagement growth rate per hour. 0 if insufficient data."""
    pts = _velocity.get(sig_id, [])
    if len(pts) < 2: return 0.0
    pts = sorted(pts)
    oldest, newest = pts[0], pts[-1]
    dt_h = max((newest[0] - oldest[0]) / 3600, 0.01)
    return max(0.0, (newest[1] - oldest[1]) / dt_h)

# ── Scoring ────────────────────────────────────────────────────────────────────

AI_KW = {
    'llm','gpt','claude','gemini','mistral','openai','anthropic','transformer',
    'diffusion','machine learning','deep learning','ai agent','rag','fine-tun',
    'multimodal','reasoning model','vibe cod','mcp','model context','cursor',
    'windsurf','replit','agentic','neural','llama','local model','ollama',
    'embedding','inference','quantiz','lora','qlora','instruct','chat model',
    'foundation model','o1','o3','o4','deepsek','deepseek','qwen','mistral',
}

BREAKING_KW = {
    'breaking','just in','urgent','exclusive','announces','acquires','acquisition',
    'shuts down','raises','launches','releases','fires','breach','hack','outage',
    'ban','blocked','war ','attack','collapse','bankrupt','ipo','merger',
}

TIER1 = {
    'techcrunch.com','reuters.com','bloomberg.com','theverge.com','arstechnica.com',
    'wired.com','ft.com','9to5mac.com','macrumors.com','github.com','arxiv.org',
    'apple.com','news.ycombinator.com','simonwillison.net','stratechery.com',
    'paperswithcode.com','huggingface.co',
}
TIER2 = {
    'venturebeat.com','thenextweb.com','zdnet.com','theregister.com','dev.to',
    'lobste.rs','indiehackers.com','reddit.com','producthunt.com',
}

def score_signal(item: dict) -> int:
    base  = 3
    title = (item.get('title') or '').lower()
    dom   = (item.get('domain') or '').lower()

    # Source tier
    if any(t in dom for t in TIER1): base += 2
    elif any(t in dom for t in TIER2): base += 1

    # AI/tech relevance
    if any(k in title for k in AI_KW): base += 1

    # Apple specificity
    if 'apple' in title and any(d in dom for d in ['9to5mac','macrumors','apple.com']): base += 1

    # Breaking keywords
    if any(k in title for k in BREAKING_KW): base += 2

    # Platform-specific engagement
    hn = item.get('hn_score', 0)
    if hn > 300: base += 3
    elif hn > 150: base += 2
    elif hn > 75:  base += 1

    reddit_ups = item.get('reddit_score', 0)
    if reddit_ups > 1000: base += 2
    elif reddit_ups > 300: base += 1

    if item.get('stars_today', 0) > 500: base += 2
    elif item.get('stars_today', 0) > 100: base += 1

    # Cross-source boost (applied separately after clustering)
    base += item.get('cross_source_boost', 0)

    # Velocity bonus
    vel = velocity_score(item['id'])
    if vel > 200: base += 2
    elif vel > 50: base += 1

    # Recency (< 3h = bonus)
    try:
        pub = str(item.get('published','')).replace('Z','').split('+')[0].strip()
        age = datetime.utcnow() - datetime.fromisoformat(pub)
        if age < timedelta(hours=1):   base += 2
        elif age < timedelta(hours=3): base += 1
    except: pass

    return min(base, 10)

# ── Clustering ─────────────────────────────────────────────────────────────────

def jaccard(a: set, b: set) -> float:
    if not a or not b: return 0.0
    return len(a & b) / len(a | b)

def cluster_and_boost(items: list[dict], threshold: float = 0.28) -> list[dict]:
    """
    Group signals by keyword overlap (Jaccard).
    Representative = highest scored item per cluster.
    Cross-source boost: +1 per extra source, capped at +3.
    """
    kws = []
    for item in items:
        text = (item.get('title','') + ' ' + (item.get('summary') or ''))
        kws.append(set(tokenize(text)))

    used   = [False] * len(items)
    result = []

    for i, (item, kw) in enumerate(zip(items, kws)):
        if used[i]: continue
        cluster = [i]
        used[i] = True
        for j in range(i + 1, len(items)):
            if used[j]: continue
            if jaccard(kw, kws[j]) >= threshold:
                cluster.append(j)
                used[j] = True

        members  = [items[k] for k in cluster]
        rep      = max(members, key=lambda x: x.get('score', 0))
        rep      = dict(rep)
        n_extra  = len(cluster) - 1
        if n_extra > 0:
            rep['cross_source_boost'] = min(n_extra, 3)
            rep['score'] = min(10, rep.get('score', 1) + rep['cross_source_boost'])
            rep['cluster_count']   = len(cluster)
            rep['cluster_sources'] = list({m.get('source','') for m in members})
        result.append(rep)

    return sorted(result, key=lambda x: x.get('score', 0), reverse=True)

# ── Brand detection ────────────────────────────────────────────────────────────

KNOWN_BRANDS = [
    'Anthropic','OpenAI','Google','DeepMind','Apple','Microsoft','Meta','Amazon','AWS','NVIDIA',
    'GitHub','Hugging Face','HuggingFace','Docker','Ollama','Cursor','SpaceX','Tesla','AMD','Cloudflare',
    'Stripe','Claude','ChatGPT','Gemini','Mistral','Llama','DeepSeek','Grok','Perplexity','Copilot',
    'Y Combinator','Vercel','Linear','Notion','Figma','Slack','Discord','Reddit','Substack',
    'Windsurf','Replit','Bolt','Lovable','Zed','Ghostty','Arc','Raycast','Warp',
    'Rust','Python','TypeScript','React','Go','Bun','Deno',
    'Papers With Code','Hugging Face','LangChain','LlamaIndex',
]

def get_brands(items: list[dict]) -> list[dict]:
    counts: dict[str, list] = {}
    for sig in items:
        title = sig.get('title', '')
        for brand in KNOWN_BRANDS:
            if brand.lower() in title.lower():
                counts.setdefault(brand, []).append(sig)
    result = []
    for brand, sigs in sorted(counts.items(), key=lambda x: -len(x[1])):
        top     = max(sigs, key=lambda s: s.get('score', 0))
        words   = [w for w in top['title'].split()
                   if w.lower() not in STOP and brand.lower() not in w.lower() and len(w) > 2]
        tag     = ' '.join(words[-2:]).upper()[:18] or 'IN PLAY'
        result.append({'name': brand, 'count': len(sigs), 'tag': tag,
                       'top_url': top.get('url', ''), 'score': top.get('score', 0)})
    return result[:20]

# ── RSS (fresh fetch via requests) ────────────────────────────────────────────

NO_CACHE_HEADERS = {
    'User-Agent':     'Mozilla/5.0 Signal-Dashboard/2.0',
    'Cache-Control':  'no-cache, no-store',
    'Pragma':         'no-cache',
    'Accept':         'application/rss+xml, application/xml, text/xml, */*',
}

def fetch_rss_fresh(url: str) -> feedparser.FeedParserDict:
    """Fetch RSS bypassing caches, then parse."""
    bust = f'{"&" if "?" in url else "?"}_{int(time.time())}'
    try:
        r = requests.get(url + bust, headers=NO_CACHE_HEADERS, timeout=12)
        return feedparser.parse(r.content)
    except:
        return feedparser.parse(url)

RSS_FEEDS = {
    # AI & Tech
    'TechCrunch':          ('https://techcrunch.com/feed/',                               'AI & Tech'),
    'The Verge':           ('https://www.theverge.com/rss/index.xml',                    'AI & Tech'),
    'Wired':               ('https://www.wired.com/feed/rss',                            'AI & Tech'),
    'Ars Technica':        ('https://feeds.arstechnica.com/arstechnica/technology-lab',  'AI & Tech'),
    'VentureBeat AI':      ('https://venturebeat.com/category/ai/feed/',                 'AI & Tech'),
    'Simon Willison':      ('https://simonwillison.net/atom/everything/',                 'AI & Tech'),
    'Papers With Code':    ('https://paperswithcode.com/newsletter/rss',                 'AI & Tech'),
    # Apple
    '9to5Mac':             ('https://9to5mac.com/feed/',                                 'Apple'),
    'MacRumors':           ('https://feeds.macrumors.com/MacRumors-All',                'Apple'),
    'AppleInsider':        ('https://appleinsider.com/rss/news/',                        'Apple'),
    # Geopolitics / Macro
    'Reuters World':       ('https://feeds.reuters.com/reuters/worldNews',               'Geopolitics'),
    'The Register':        ('https://www.theregister.com/headlines.atom',                'Geopolitics'),
    # Open Source / Indie
    'Dev.to':              ('https://dev.to/feed',                                       'Open Source'),
    'Lobste.rs':           ('https://lobste.rs/rss',                                     'Open Source'),
    'IndieHackers':        ('https://www.indiehackers.com/feed.xml',                    'Open Source'),
    'FOSS Weekly':         ('https://fossweekly.beehiiv.com/feed',                      'Open Source'),
}

SUBREDDITS = [
    ('LocalLLaMA',    'AI & Tech',    'local AI models / LLMs'),
    ('MachineLearning','AI & Tech',   'ML research'),
    ('opensource',    'Open Source',  'FOSS / OSS projects'),
    ('macapps',       'Apple',        'Mac apps'),
    ('selfhosted',    'Open Source',  'self-hosted OSS'),
    ('SideProject',   'Open Source',  'indie dev'),
    ('commandline',   'Open Source',  'CLI tools'),
    ('linux',         'Open Source',  'Linux / open-source OS'),
    ('privacy',       'Open Source',  'FOSS privacy tools'),
    ('programming',   'AI & Tech',    'general programming'),
]

# ── Fetchers ───────────────────────────────────────────────────────────────────

def _parse_pub(entry) -> str:
    """Extract ISO timestamp from feedparser entry. Always returns ISO 8601 string."""
    for field in ('published_parsed', 'updated_parsed'):
        t = entry.get(field)
        if t:
            try: return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except: pass
    for field in ('published', 'updated'):
        s = entry.get(field, '')
        if s:
            try:
                from dateutil import parser as dp
                return dp.parse(s).astimezone(timezone.utc).isoformat()
            except:
                pass
    return datetime.now(timezone.utc).isoformat()

def _add(sig: dict):
    # Auto-generate extractive summary if none
    if not sig.get('summary_extracted') and sig.get('summary'):
        sig['summary_extracted'] = extractive_summary(sig['summary'])
    sig['score'] = score_signal(sig)
    with _lock:
        if sig['id'] not in signals:
            signals[sig['id']] = sig
        else:
            # Update engagement metrics but keep rest
            for k in ('hn_score', 'hn_comments', 'reddit_score', 'stars_today'):
                if k in sig: signals[sig['id']][k] = sig[k]
            record_velocity(sig['id'], sig.get('hn_score', 0) + sig.get('reddit_score', 0))
            signals[sig['id']]['score'] = score_signal(signals[sig['id']])

def _too_old(published: str, hours: int = 48) -> bool:
    """Return True if published timestamp is older than `hours`. Always uses UTC."""
    if not published:
        return False
    now_utc = datetime.now(timezone.utc)
    try:
        from dateutil import parser as dp
        dt = dp.parse(str(published))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now_utc - dt) > timedelta(hours=hours)
    except:
        return False  # unparseable → keep it

# HN via Algolia (much fresher + searchable) ───────────────────────────────────

HN_TOPIC_QUERIES = [
    ('AI OR LLM OR Claude OR OpenAI OR Anthropic OR "machine learning"', 'AI & Tech'),
    ('Apple OR macOS OR "Apple silicon" OR iPhone OR iPad', 'Apple'),
    ('open source OR FOSS OR "self-hosted" OR indie OR Linux', 'Open Source'),
    ('geopolitics OR "trade war" OR "chip ban" OR "tech regulation"', 'Geopolitics'),
]

# Maps keywords → domain_label for the catch-all results
HN_KEYWORD_LABELS = {
    'AI & Tech':   {'ai','llm','openai','anthropic','claude','gpt','gemini','ml','neural',
                    'model','inference','transformer','deepseek','mistral','llama'},
    'Apple':       {'apple','macos','ios','iphone','ipad','swift','xcode','mac'},
    'Open Source': {'open source','foss','linux','github','git','rust','python','golang',
                    'self-hosted','selfhosted','docker','kubernetes','terminal','cli'},
    'Geopolitics': {'china','russia','nato','tariff','sanction','geopolit','chip ban',
                    'regulation','law','policy','congress','senate','election'},
}

def _hn_label(title: str, url: str) -> str:
    text = (title + ' ' + url).lower()
    for label, kws in HN_KEYWORD_LABELS.items():
        if any(k in text for k in kws):
            return label
    return 'AI & Tech'  # default HN to AI bucket

def fetch_hn_algolia():
    since = int(time.time()) - 3600 * 8
    seen_ids: set = set()
    n = 0

    def _upsert(hit, label):
        nonlocal n
        key = f'hna-{hit["objectID"]}'
        if key in seen_ids: return
        seen_ids.add(key)
        url = hit.get('url') or f'https://news.ycombinator.com/item?id={hit["objectID"]}'
        pub = datetime.fromtimestamp(hit.get('created_at_i', time.time()),
                                     tz=timezone.utc).isoformat()
        eng = hit.get('points', 0)
        record_velocity(key, eng)
        if key in signals:
            with _lock:
                signals[key]['hn_score'] = eng
                signals[key]['hn_comments'] = hit.get('num_comments', 0)
            return
        _add({'id': key, 'title': hit.get('title', ''), 'url': url,
              'domain': url.split('/')[2] if '://' in url else 'news.ycombinator.com',
              'source': 'Hacker News', 'domain_label': label, 'published': pub,
              'hn_score': eng, 'hn_comments': hit.get('num_comments', 0),
              'hn_id': hit['objectID'], 'summary': None})
        n += 1

    # 1. Catch-all: top recent stories regardless of topic (points>5, last 8h)
    try:
        r = requests.get('https://hn.algolia.com/api/v1/search_by_date',
                         params={'tags': 'story', 'hitsPerPage': 50,
                                 'numericFilters': f'created_at_i>{since},points>5'},
                         headers=NO_CACHE_HEADERS, timeout=10)
        for hit in r.json().get('hits', []):
            label = _hn_label(hit.get('title', ''), hit.get('url', ''))
            _upsert(hit, label)
    except Exception as e:
        log.warning(f'HN Algolia catch-all: {e}')

    # 2. Topic queries to catch lower-scored but relevant stories
    for query, label in HN_TOPIC_QUERIES:
        try:
            r = requests.get('https://hn.algolia.com/api/v1/search_by_date',
                             params={'query': query, 'tags': 'story', 'hitsPerPage': 20,
                                     'numericFilters': f'created_at_i>{since},points>2'},
                             headers=NO_CACHE_HEADERS, timeout=10)
            for hit in r.json().get('hits', []):
                _upsert(hit, label)
        except Exception as e:
            log.warning(f'HN Algolia [{query[:20]}]: {e}')

    log.info(f'HN Algolia: +{n}')

def fetch_rss():
    n = 0
    for name, (url, label) in RSS_FEEDS.items():
        try:
            feed = fetch_rss_fresh(url)
            for e in feed.entries[:15]:
                link = e.get('link','')
                key  = 'rss-' + hashlib.md5((link + e.get('title','')).encode()).hexdigest()[:12]
                if key in signals: continue
                pub  = _parse_pub(e)
                if _too_old(pub, 16): continue
                raw_sum = re.sub(r'<[^>]+>', '', e.get('summary') or e.get('content','') or '')
                _add({'id': key, 'title': e.get('title','').strip(), 'url': link,
                      'domain': link.split('/')[2] if '://' in link else '',
                      'source': name, 'domain_label': label, 'published': pub,
                      'summary': raw_sum[:600]})
                n += 1
        except Exception as ex: log.warning(f'RSS {name}: {ex}')
    log.info(f'RSS: +{n}')

def fetch_reddit():
    n = 0
    cutoff = int(time.time()) - 86400  # last 24h
    for sub, label, _ in SUBREDDITS:
        try:
            r = requests.get(
                f'https://www.reddit.com/r/{sub}/hot.json',
                params={'limit': 20, 't': 'day'},
                headers={**NO_CACHE_HEADERS, 'User-Agent': 'signal-dashboard:v2 (by /u/signalbot)'},
                timeout=10
            )
            if r.status_code != 200: continue
            for post in r.json().get('data', {}).get('children', []):
                p = post.get('data', {})
                if p.get('stickied') or p.get('is_self') is False and not p.get('url'):
                    continue
                created = int(p.get('created_utc', 0))
                if created < cutoff: continue
                score = p.get('score', 0)
                if score < 20: continue  # noise floor
                key = f'reddit-{p["id"]}'
                if key in signals: continue
                pub = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                url = p.get('url') or f'https://reddit.com{p.get("permalink","")}'
                title = p.get('title','')
                summary = p.get('selftext','')[:400] or None
                record_velocity(key, score)
                _add({'id': key, 'title': f'{title}', 'url': url,
                      'domain': 'reddit.com', 'source': f'r/{sub}',
                      'domain_label': label, 'published': pub,
                      'reddit_score': score, 'reddit_comments': p.get('num_comments',0),
                      'subreddit': sub, 'summary': summary})
                n += 1
        except Exception as e: log.warning(f'Reddit r/{sub}: {e}')
    log.info(f'Reddit: +{n}')

def fetch_github_trending():
    try:
        r = requests.get('https://gh-trending-api.waningflow.com/repositories',
                         params={'since': 'daily'}, headers=NO_CACHE_HEADERS, timeout=10)
        if r.status_code == 200:
            for repo in r.json()[:25]:
                _add_gh(repo.get('name',''), repo.get('description',''),
                        repo.get('url',''), repo.get('stars',0), repo.get('starsSince',0))
            log.info('GitHub trending: OK'); return
    except: pass
    # Fallback: GitHub Search
    try:
        since = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
        r = requests.get('https://api.github.com/search/repositories',
                         params={'q': f'created:>{since} stars:>50', 'sort': 'stars', 'per_page': 20},
                         headers={**NO_CACHE_HEADERS, 'Accept': 'application/vnd.github.v3+json'},
                         timeout=10)
        if r.status_code == 200:
            for repo in r.json().get('items', []):
                _add_gh(repo.get('full_name',''), repo.get('description',''),
                        repo.get('html_url',''), repo.get('stargazers_count',0), 0)
    except Exception as e: log.error(f'GH fallback: {e}')

def _add_gh(name, desc, url, stars, stars_today):
    if not url: return
    key = 'gh-' + hashlib.md5(url.encode()).hexdigest()[:12]
    d   = (desc or '')[:100]
    record_velocity(key, stars)
    _add({'id': key, 'title': f'⭐ {name}' + (f' — {d}' if d else ''),
          'url': url, 'domain': 'github.com', 'source': 'GitHub Trending',
          'domain_label': 'Open Source', 'published': datetime.utcnow().isoformat(),
          'stars': stars, 'stars_today': stars_today, 'summary': desc})

def fetch_arxiv():
    try:
        r    = requests.get('http://export.arxiv.org/api/query',
                            params={'search_query': 'cat:cs.AI OR cat:cs.LG OR cat:cs.CL',
                                    'start': 0, 'max_results': 20,
                                    'sortBy': 'submittedDate', 'sortOrder': 'descending'},
                            headers=NO_CACHE_HEADERS, timeout=20)
        feed = feedparser.parse(r.content)
        n = 0
        for e in feed.entries:
            key = 'arxiv-' + e.id.split('/abs/')[-1].replace('/','_')
            if key in signals: continue
            pub = _parse_pub(e)
            if _too_old(pub, 48): continue
            abstract = re.sub(r'\s+', ' ', e.get('summary',''))
            _add({'id': key, 'title': f'📄 {e.title.strip()}', 'url': e.link,
                  'domain': 'arxiv.org', 'source': 'ArXiv', 'domain_label': 'AI & Tech',
                  'published': pub, 'summary': abstract[:500],
                  'authors': ', '.join(a.name for a in e.get('authors',[])[:3])})
            n += 1
        log.info(f'ArXiv: +{n}')
    except Exception as e: log.error(f'ArXiv: {e}')

# ── Alerts ─────────────────────────────────────────────────────────────────────

def check_and_alert():
    topic = CFG.get('ntfy_topic','')
    if not topic: return
    thr = int(CFG.get('breaking_threshold', 8))
    with _lock:
        cands = [(sid,sig) for sid,sig in signals.items()
                 if sig.get('score',0) >= thr and sid not in alerts_sent]
    for sid, sig in cands:
        try:
            requests.post(f'https://ntfy.sh/{topic}', data=sig['title'].encode('utf-8'),
                          headers={'Title': f'Signal [{sig["score"]}/10] {sig["source"]}',
                                   'Priority': 'high', 'Tags': 'bell', 'Click': sig['url']},
                          timeout=6)
            alerts_sent.add(sid)
        except Exception as e: log.warning(f'ntfy: {e}')

def cleanup_old():
    # Keep signals for 30 days so users can scroll back through history
    with _lock:
        rm = [sid for sid,sig in signals.items() if _too_old(str(sig.get('published','')), 720)]
        for sid in rm: signals.pop(sid, None)
    if rm: log.info(f'Cleaned {len(rm)}')

# ── Scheduler ──────────────────────────────────────────────────────────────────

def run_fast():
    """Every 10 min: HN + Reddit (fastest-changing sources)."""
    log.info('── fast poll ──')
    fetch_hn_algolia()
    fetch_reddit()
    check_and_alert()
    save_signals()
    log.info(f'── fast done: {len(signals)} ──')

def run_full():
    """Every 30 min: all sources."""
    log.info('── full poll ──')
    fetch_hn_algolia()
    fetch_rss()
    fetch_reddit()
    fetch_github_trending()
    check_and_alert()
    cleanup_old()
    save_signals()
    log.info(f'── full done: {len(signals)} ──')

def run_arxiv():
    fetch_arxiv(); save_signals()

# ── Flask ──────────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.after_request
def cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

def _items_sorted() -> list[dict]:
    with _lock:
        items = list(signals.values())
    # Only show signals from the last 16h — keeps the feed current
    items = [i for i in items if not _too_old(str(i.get('published','')), 16)]
    return sorted(items, key=lambda x: (x.get('score',0), x.get('published','')), reverse=True)

def _pub_epoch(item: dict) -> float:
    """Parse published timestamp to UTC epoch seconds for sorting. Returns 0 on failure."""
    pub = str(item.get('published', ''))
    if not pub:
        return 0.0
    try:
        from dateutil import parser as dp
        dt = dp.parse(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except:
        return 0.0

def _items_by_time() -> list[dict]:
    """All signals sorted newest-first by parsed UTC timestamp."""
    with _lock:
        items = list(signals.values())
    items = [i for i in items if not _too_old(str(i.get('published', '')), 16)]
    return sorted(items, key=_pub_epoch, reverse=True)

@app.route('/api/signals')
def api_signals():
    domain = request.args.get('domain', 'all')
    items  = _items_sorted()
    if domain != 'all':
        items = [i for i in items if i.get('domain_label','') == domain]

    # Cluster (score-sorted, for category tabs)
    clustered = cluster_and_boost(items)

    grouped: dict[str,list] = {}
    for i in clustered:
        grouped.setdefault(i.get('domain_label','Other'), []).append(i)

    # Chronological view: all signals newest-first (no score filter)
    chrono = _items_by_time()

    thr = int(CFG.get('breaking_threshold', 8))
    return jsonify({
        'signals':      clustered,
        'chrono':       chrono,                # newest-first, all sources
        'grouped':      grouped,
        'last_updated': datetime.utcnow().isoformat(),
        'total':        len(items),
        'breaking':     [i for i in clustered if i.get('score',0) >= thr][:8],
    })

@app.route('/api/stats')
def api_stats():
    items  = _items_sorted()
    thr    = int(CFG.get('breaking_threshold', 8))
    act    = [i for i in items if i.get('score',0) >= thr]
    watch  = [i for i in items if 5 <= i.get('score',0) < thr]
    repos  = [i for i in items if i.get('source') == 'GitHub Trending']
    reddit = [i for i in items if 'reddit' in i.get('id','')]
    top    = act[0] if act else (items[0] if items else None)
    brands = get_brands(items)
    # Ticker: 20 most recent items (chronological) so it shows fresh news
    recent = _items_by_time()
    ticker = [{'id': i['id'], 'title': i['title'], 'score': i['score'], 'url': i['url'],
               'source': i['source']} for i in recent[:20]]
    return jsonify({
        'act_count':       len(act),
        'watch_count':     len(watch),
        'trending_repos':  len(repos),
        'reddit_signals':  len(reddit),
        'total':           len(items),
        'brands':          brands,
        'top_story':       top['title'][:65] if top else '—',
        'top_story_score': top.get('score',0) if top else 0,
        'ticker':          ticker,
        'last_updated':    datetime.utcnow().isoformat(),
    })

_fetch_lock = threading.Lock()

@app.route('/api/fetch-now', methods=['POST'])
def fetch_now():
    """Trigger an immediate fast poll (non-blocking)."""
    if not _fetch_lock.acquire(blocking=False):
        return jsonify({'status': 'already_running'})
    def _run():
        try:
            run_fast()
        finally:
            _fetch_lock.release()
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'started'})

@app.route('/api/deeper-take/<signal_id>')
def deeper_take(signal_id: str):
    """Claude on-demand only. Never called automatically."""
    with _lock: sig = signals.get(signal_id)
    if not sig: return jsonify({'error': 'Not found'}), 404
    key = CFG.get('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY','')
    if not key: return jsonify({'error': 'Set anthropic_api_key in ~/.signal-dashboard/config.json'}), 400

    content = sig.get('summary','')
    url     = sig.get('url','')
    if url and 'arxiv.org' not in url:
        try:
            r = requests.get(url, timeout=8, headers={'User-Agent':'Mozilla/5.0'})
            if r.status_code == 200 and 'text/html' in r.headers.get('content-type',''):
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, 'html.parser')
                for t in soup(['script','style','nav','footer','aside','header']): t.decompose()
                fetched = soup.get_text(separator='\n', strip=True)
                if len(fetched) > 300: content = fetched[:4000]
        except: pass

    prompt = f"""Sharp analyst. AI, tech, open source, geopolitics beat.

Title: {sig['title']}
Source: {sig['source']} | Score: {sig.get('score')}/10
URL: {url}
Content: {content[:3000] if content else '(unavailable)'}

200-300 word analyst brief:
**What this actually is** — one sentence, cut the noise.
**Why it matters** — real signal, not press release.
**Who should pay attention** — and why.
**What to watch next** — one follow-on signal to confirm.

Direct. Specific. Opinionated. Insider audience."""

    try:
        r = requests.post('https://api.anthropic.com/v1/messages',
                          headers={'x-api-key': key, 'anthropic-version': '2023-06-01',
                                   'content-type': 'application/json'},
                          json={'model': 'claude-opus-4-7', 'max_tokens': 700,
                                'messages': [{'role': 'user', 'content': prompt}]},
                          timeout=35)
        r.raise_for_status()
        return jsonify({'analysis': r.json()['content'][0]['text'], 'content': content[:1500]})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/weekly-brief')
def weekly_brief():
    """Algo-generated digest. Claude optional."""
    items     = _items_sorted()
    thr       = int(CFG.get('breaking_threshold', 8))
    act       = [i for i in items if i.get('score',0) >= thr][:8]
    watch     = [i for i in items if 5 <= i.get('score',0) < thr][:10]
    clustered = cluster_and_boost(items)

    # Source breakdown
    src_counts: Counter = Counter(i.get('source','') for i in items)
    domain_counts: Counter = Counter(i.get('domain_label','') for i in items)

    # Top velocity items
    velocity_items = sorted(items, key=lambda x: velocity_score(x['id']), reverse=True)[:5]

    brief = {
        'generated_at':   datetime.utcnow().isoformat(),
        'signal_count':   len(items),
        'act_tier':       [{'title': i['title'], 'score': i['score'], 'source': i['source'],
                            'url': i['url'], 'summary': i.get('summary_extracted','')[:200]}
                           for i in act],
        'watch_tier':     [{'title': i['title'], 'score': i['score'], 'source': i['source'],
                            'url': i['url']} for i in watch],
        'velocity_movers':[{'title': i['title'], 'score': i['score'],
                            'velocity': round(velocity_score(i['id']), 1)} for i in velocity_items],
        'source_breakdown': dict(src_counts.most_common(10)),
        'domain_breakdown': dict(domain_counts),
        'top_clusters':   [{'title': i['title'], 'score': i['score'],
                            'cluster_count': i.get('cluster_count',1),
                            'cluster_sources': i.get('cluster_sources',[])}
                           for i in clustered[:5] if i.get('cluster_count',1) > 1],
    }
    BRIEF_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRIEF_PATH.write_text(json.dumps(brief, indent=2))
    return jsonify(brief)

@app.route('/api/config')
def api_config():
    return jsonify({k: v for k, v in CFG.items() if 'key' not in k.lower()})

# ── Dashboard HTML ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=Playfair+Display:ital,wght@1,700;1,800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#fff;--bg2:#f7f7f5;--bg3:#efefec;
  --border:#e5e5e0;--border2:#d0d0c8;
  --text:#0a0a0a;--muted:#888;--muted2:#bbb;
  --ga:#ff6b35;--gb:#c026d3;
  --green:#059669;--blue:#2563eb;--red:#dc2626;--orange:#ea580c;--yellow:#ca8a04;
  --shadow:0 1px 3px rgba(0,0,0,.07),0 4px 12px rgba(0,0,0,.04);
}
[data-theme="dark"]{
  --bg:#0a0a0f;--bg2:#0f0f1a;--bg3:#161628;
  --border:#1e1e35;--border2:#2a2a45;
  --text:#e8e8f0;--muted:#6b6b8a;--muted2:#3a3a5a;
  --shadow:0 1px 3px rgba(0,0,0,.3),0 4px 12px rgba(0,0,0,.2);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;
  line-height:1.6;min-height:100vh;transition:background .2s,color .2s}
a{color:inherit;text-decoration:none}button{font-family:'Inter',sans-serif}

/* ── Chrome ── */
.chrome{position:sticky;top:0;z-index:50;background:var(--bg);border-bottom:1px solid var(--border);
  display:flex;align-items:center;padding:0 28px;height:42px;gap:14px}
.chrome-logo{font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;
  background:linear-gradient(135deg,var(--ga),var(--gb));-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;background-clip:text}
.chrome-spacer{flex:1}
.chrome-meta{font-size:11px;color:var(--muted)}
.theme-btn{background:none;border:1px solid var(--border);border-radius:6px;
  padding:3px 9px;font-size:11px;color:var(--muted);cursor:pointer;transition:all .15s}
.theme-btn:hover{border-color:var(--border2);color:var(--text)}
.fetch-btn{background:none;border:1px solid var(--border);border-radius:6px;
  padding:3px 10px;font-size:11px;font-weight:500;color:var(--ga);cursor:pointer;
  transition:all .15s;font-family:inherit}
.fetch-btn:hover{border-color:var(--ga);background:rgba(255,107,53,.06)}
.fetch-btn:disabled{opacity:.5;cursor:default}

/* ── Ticker ── */
.ticker-wrap{background:var(--bg2);border-bottom:1px solid var(--border);
  height:32px;overflow:hidden;display:flex;align-items:center;position:sticky;top:42px;z-index:49}
.ticker-label{flex-shrink:0;padding:0 14px 0 20px;font-size:9.5px;font-weight:800;
  letter-spacing:.12em;text-transform:uppercase;color:var(--ga);border-right:1px solid var(--border);
  height:100%;display:flex;align-items:center;gap:6px}
.ticker-dot{width:6px;height:6px;border-radius:50%;background:var(--ga);
  animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.8)}}
.ticker-track{flex:1;overflow:hidden;cursor:default;min-width:0}
.ticker-inner{display:flex;align-items:center;gap:0;
  width:max-content;animation:ticker 60s linear infinite;white-space:nowrap;will-change:transform}
.ticker-inner:hover{animation-play-state:paused}
@keyframes ticker{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
.ticker-item{display:inline-flex;align-items:center;gap:7px;padding:0 24px;font-size:12px;
  cursor:pointer;border-radius:4px;transition:background .12s}
.ticker-item:hover{background:rgba(255,107,53,.07)}
.ticker-item:hover .ticker-title{text-decoration:underline;text-decoration-color:var(--ga)}
.ticker-score{font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px;
  background:rgba(255,107,53,.12);color:var(--ga)}
.ticker-title{color:var(--text)}
.ticker-src{color:var(--muted2);font-size:11px}
.ticker-sep{color:var(--muted2);margin:0 2px}

/* ── Page ── */
.page{max-width:1100px;margin:0 auto;padding:0 28px 80px}

/* ── Hero ── */
.hero{padding:36px 0 24px}
.hero-eyebrow{font-size:10.5px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);display:flex;align-items:center;gap:8px;margin-bottom:12px}
.live-dot{width:7px;height:7px;border-radius:50%;background:var(--ga);
  animation:pulse 2s ease-in-out infinite;flex-shrink:0}
.hero-title{font-size:clamp(38px,5.5vw,64px);font-weight:900;line-height:1.05;letter-spacing:-.03em}
.word-dashboard{font-family:'Playfair Display',Georgia,serif;font-style:italic;
  background:linear-gradient(135deg,var(--ga) 0%,var(--gb) 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-sub{margin-top:10px;font-size:13.5px;color:var(--muted);max-width:580px}

/* ── Stats ── */
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;
  background:var(--border);border:1px solid var(--border);border-radius:10px;
  overflow:hidden;margin-bottom:24px}
.stat{background:var(--bg);padding:16px 18px 14px;position:relative}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.stat:nth-child(1)::before{background:var(--ga)}
.stat:nth-child(2)::before{background:#f59e0b}
.stat:nth-child(3)::before{background:#3b82f6}
.stat:nth-child(4)::before{background:#8b5cf6}
.stat:nth-child(5)::before{background:var(--green)}
.stat-num{font-size:34px;font-weight:800;line-height:1;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.stat:nth-child(1) .stat-num{color:var(--ga)}
.stat:nth-child(2) .stat-num{color:#f59e0b}
.stat:nth-child(3) .stat-num{color:#3b82f6}
.stat:nth-child(4) .stat-num{color:#8b5cf6}
.stat:nth-child(5) .stat-num{color:var(--green)}
.stat-label{font-size:9px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-top:4px}
.stat-sub{font-size:10.5px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ── Brands ── */
.brands-section{margin-bottom:24px}
.brands-eye{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);margin-bottom:9px;display:flex;align-items:center;gap:7px}
.brands-dot{width:6px;height:6px;border-radius:50%;background:var(--ga);flex-shrink:0}
.brands-scroll{display:flex;gap:7px;overflow-x:auto;padding-bottom:4px;scrollbar-width:none}
.brands-scroll::-webkit-scrollbar{display:none}
.brand-pill{display:inline-flex;align-items:center;gap:6px;padding:5px 11px 5px 10px;
  border-radius:100px;border:1px solid var(--border);background:var(--bg2);
  white-space:nowrap;cursor:pointer;transition:all .15s;flex-shrink:0}
.brand-pill:hover{border-color:var(--border2);background:var(--bg3)}
.brand-name{font-size:12px;font-weight:600;color:var(--text)}
.brand-tag{font-size:9px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}

/* ── Breaking ── */
.breaking{display:none;align-items:center;gap:10px;padding:10px 0;margin-bottom:18px;
  border-top:1px solid rgba(220,38,38,.2);border-bottom:1px solid rgba(220,38,38,.2)}
.breaking.show{display:flex}
.breaking-badge{background:var(--red);color:#fff;font-size:9px;font-weight:800;
  letter-spacing:.1em;text-transform:uppercase;padding:3px 9px;border-radius:100px;flex-shrink:0}
.breaking-items{display:flex;gap:10px;overflow-x:auto;scrollbar-width:none}
.breaking-item{font-size:12px;color:var(--red);cursor:pointer;white-space:nowrap;transition:opacity .15s}
.breaking-item:hover{opacity:.7}

/* ── Tabs ── */
.tabs{display:flex;border-bottom:1px solid var(--border);overflow-x:auto;scrollbar-width:none;
  margin-bottom:24px;position:sticky;top:74px;z-index:40;background:var(--bg)}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:10px 16px;font-size:13px;font-weight:500;color:var(--muted);cursor:pointer;
  border-bottom:2px solid transparent;white-space:nowrap;transition:all .15s;
  background:none;border-top:none;border-left:none;border-right:none}
.tab:hover{color:var(--text)}
.tab.active{color:var(--text);border-bottom-color:var(--ga)}
.tab-pane{display:none}
.tab-pane.active{display:block}

/* ── Section head ── */
.sec-head{display:flex;align-items:center;gap:12px;margin-bottom:14px;margin-top:4px}
.sec-head h2{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);white-space:nowrap}
.sec-line{flex:1;height:1px;background:var(--border)}

/* ── Signal rows ── */
.signal-list{margin-bottom:28px}
.signal-row{display:flex;align-items:flex-start;gap:12px;padding:13px 8px;
  border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s;border-radius:4px;margin:0 -8px}
.signal-row:hover,.signal-row.expanded{background:var(--bg2)}
.signal-row:last-child{border-bottom:none}

.sig-score{flex-shrink:0;margin-top:1px}
.score-badge{width:30px;height:20px;border-radius:4px;display:flex;align-items:center;
  justify-content:center;font-size:10.5px;font-weight:700;font-variant-numeric:tabular-nums}
.s10,.s9{background:rgba(220,38,38,.1);color:#dc2626}
.s8{background:rgba(234,88,12,.1);color:#ea580c}
.s7{background:rgba(245,158,11,.1);color:#ca8a04}
.s6{background:rgba(101,163,13,.1);color:#65a30d}
.s5{background:rgba(37,99,235,.1);color:#2563eb}
.s4,.s3,.s2,.s1{background:var(--bg3);color:var(--muted)}

.sig-body{flex:1;min-width:0}
.sig-title{font-size:14px;font-weight:500;line-height:1.4;color:var(--text);margin-bottom:4px}
.sig-summary{font-size:12.5px;color:var(--muted);line-height:1.55;margin-bottom:5px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sig-meta{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.sig-source{font-size:11px;font-weight:500;color:var(--muted)}
.sig-domain{font-size:11px;color:var(--muted2)}
.sig-time{font-size:11px;color:var(--muted2);margin-left:auto}
.cluster-badge{font-size:9.5px;font-weight:700;padding:1px 7px;border-radius:100px;
  background:rgba(139,92,246,.1);border:1px solid rgba(139,92,246,.25);color:#7c3aed}
.vel-badge{font-size:9.5px;font-weight:700;padding:1px 7px;border-radius:100px;
  background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.25);color:var(--green)}

/* ── Expand ── */
.sig-expand{display:none;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.signal-row.expanded .sig-expand{display:block}
.expand-actions{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}
.btn{padding:5px 13px;border-radius:6px;font-size:12px;font-weight:500;cursor:pointer;
  transition:all .15s;border:1px solid var(--border);background:var(--bg);color:var(--muted)}
.btn:hover{border-color:var(--border2);color:var(--text)}
.btn.grad{background:linear-gradient(135deg,var(--ga),var(--gb));border-color:transparent;color:#fff}
.btn.grad:hover{opacity:.9}
.btn:disabled{opacity:.4;cursor:default}

.article-box{background:var(--bg);border:1px solid var(--border);border-radius:7px;
  padding:12px;font-size:12px;font-family:ui-monospace,monospace;color:var(--muted);
  max-height:220px;overflow-y:auto;white-space:pre-wrap;line-height:1.65;display:none;margin-bottom:10px}
.article-box.show{display:block}

.claude-panel{background:linear-gradient(135deg,rgba(255,107,53,.04),rgba(192,38,211,.04));
  border:1px solid rgba(255,107,53,.15);border-radius:9px;
  padding:14px 16px;display:none}
.claude-panel.show{display:block}
.claude-label{font-size:9px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;
  background:linear-gradient(135deg,var(--ga),var(--gb));-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;background-clip:text;margin-bottom:8px}
.claude-body{font-size:13px;line-height:1.8;color:var(--text)}
.claude-body strong{font-weight:600}
.claude-body p{margin-bottom:9px}
.claude-body p:last-child{margin-bottom:0}

.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--border2);
  border-top-color:var(--ga);border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Weekly brief (algo) ── */
.brief-wrap{max-width:740px}
.brief-card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;
  padding:18px 20px;margin-bottom:16px}
.brief-card h3{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.brief-row{display:flex;align-items:flex-start;gap:10px;padding:9px 0;
  border-bottom:1px solid var(--border)}
.brief-row:last-child{border-bottom:none}
.brief-score{flex-shrink:0}
.brief-title{font-size:13px;font-weight:500;color:var(--text);margin-bottom:2px}
.brief-meta{font-size:11px;color:var(--muted)}
.brief-sum{font-size:12px;color:var(--muted);margin-top:3px;line-height:1.5}
.cluster-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)}
.cluster-row:last-child{border-bottom:none}
.cluster-num{font-size:20px;font-weight:800;color:#7c3aed;min-width:32px;text-align:center}
.vel-num{font-size:20px;font-weight:800;color:var(--green);min-width:48px;text-align:center;font-size:14px}

/* ── Empty ── */
.empty{text-align:center;padding:50px 0;color:var(--muted)}
.empty strong{display:block;font-size:15px;font-weight:600;margin-bottom:5px;color:var(--text)}

/* ── Loading ── */
#loading{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:14px;z-index:100;transition:opacity .3s}
#loading.out{opacity:0;pointer-events:none}
#loading p{color:var(--muted);font-size:13px}

/* ── Mobile ── */
@media(max-width:640px){
  .chrome{padding:0 14px;height:40px;gap:8px}
  .chrome-meta{display:none}
  .page{padding:0 14px 60px}
  .hero{padding:22px 0 16px}
  .hero-title{font-size:clamp(30px,8vw,48px)}
  .stats{grid-template-columns:repeat(2,1fr)}
  .stat{padding:12px 14px 10px}
  .stat-num{font-size:26px}
  .tabs{top:40px}
  .sig-title{font-size:13px}
  .brands-section{margin-bottom:16px}
  .score-badge{width:26px;height:18px;font-size:10px}
  .signal-row{gap:9px;padding:11px 6px}
  .ticker-wrap{top:40px}
}
@media(max-width:400px){
  .stats{grid-template-columns:1fr 1fr}
  .stat:last-child{display:none}
}
</style>
</head>
<body>
<div id="loading">
  <div style="font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;
    background:linear-gradient(135deg,#ff6b35,#c026d3);-webkit-background-clip:text;
    -webkit-text-fill-color:transparent">SIGNAL DASHBOARD</div>
  <div class="spin" style="width:18px;height:18px"></div>
  <p>Loading intelligence feed…</p>
</div>

<!-- Chrome -->
<div class="chrome">
  <div class="chrome-logo">Signal</div>
  <div class="chrome-spacer"></div>
  <div class="chrome-meta" id="chrome-meta">—</div>
  <button class="fetch-btn" id="fetch-btn" onclick="fetchNow(this)">⟳ Fetch Now</button>
  <button class="theme-btn" onclick="toggleTheme()" id="theme-btn">☀ Light</button>
</div>

<!-- Ticker -->
<div class="ticker-wrap">
  <div class="ticker-label"><div class="ticker-dot"></div>LIVE</div>
  <div class="ticker-track">
    <div class="ticker-inner" id="ticker-inner">
      <span class="ticker-item"><span style="color:var(--muted)">Loading signals…</span></span>
    </div>
  </div>
</div>

<div class="page">
  <!-- Hero -->
  <div class="hero">
    <div class="hero-eyebrow"><div class="live-dot"></div><span id="hero-eyebrow">LIVE INTELLIGENCE · —</span></div>
    <h1 class="hero-title"><span>Signal</span><span class="word-dashboard"> Dashboard</span></h1>
    <p class="hero-sub" id="hero-sub">Real-time algorithmic intelligence across 20+ sources.</p>
  </div>

  <!-- Stats -->
  <div class="stats">
    <div class="stat"><div class="stat-num" id="s-act">—</div><div class="stat-label">Act Tier (8–10)</div></div>
    <div class="stat"><div class="stat-num" id="s-watch">—</div><div class="stat-label">Watch Tier (5–7)</div></div>
    <div class="stat"><div class="stat-num" id="s-repos">—</div><div class="stat-label">GitHub Trending</div></div>
    <div class="stat"><div class="stat-num" id="s-reddit">—</div><div class="stat-label">Reddit Signals</div></div>
    <div class="stat">
      <div class="stat-num" id="s-top">—</div>
      <div class="stat-label">Top Score</div>
      <div class="stat-sub" id="s-top-title">—</div>
    </div>
  </div>

  <!-- Brands -->
  <div class="brands-section">
    <div class="brands-eye"><div class="brands-dot"></div>THIS CYCLE — BRANDS IN PLAY</div>
    <div class="brands-scroll" id="brands-scroll"><span style="color:var(--muted);font-size:13px">Loading…</span></div>
  </div>

  <!-- Breaking -->
  <div class="breaking" id="breaking">
    <div class="breaking-badge">⚡ Breaking</div>
    <div class="breaking-items" id="breaking-items"></div>
  </div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab active" data-tab="top"    onclick="switchTab('top',this)">Top Signals</button>
    <button class="tab"        data-tab="ai"     onclick="switchTab('ai',this)">AI & Tech</button>
    <button class="tab"        data-tab="apple"  onclick="switchTab('apple',this)">Apple</button>
    <button class="tab"        data-tab="geo"    onclick="switchTab('geo',this)">Geopolitics</button>
    <button class="tab"        data-tab="oss"    onclick="switchTab('oss',this)">Open Source</button>
    <button class="tab"        data-tab="github" onclick="switchTab('github',this)">GitHub</button>
    <button class="tab"        data-tab="reddit" onclick="switchTab('reddit',this)">Reddit</button>
    <button class="tab"        data-tab="brief"  onclick="switchTab('brief',this)">Brief</button>
  </div>

  <div id="pane-top"    class="tab-pane active"></div>
  <div id="pane-ai"     class="tab-pane"></div>
  <div id="pane-apple"  class="tab-pane"></div>
  <div id="pane-geo"    class="tab-pane"></div>
  <div id="pane-oss"    class="tab-pane"></div>
  <div id="pane-github" class="tab-pane"></div>
  <div id="pane-reddit" class="tab-pane"></div>
  <div id="pane-brief"  class="tab-pane"></div>
</div>

<script>
let _data=null,_stats=null,_brief=null;

// ── Theme ──────────────────────────────────────────────────────────────────────
function toggleTheme(){
  const d=document.documentElement,dark=d.getAttribute('data-theme')==='dark';
  d.setAttribute('data-theme',dark?'light':'dark');
  document.getElementById('theme-btn').textContent=dark?'☀ Light':'◑ Dark';
  localStorage.setItem('sg-theme',dark?'light':'dark');
}
(()=>{
  const s=localStorage.getItem('sg-theme');
  if(s){document.documentElement.setAttribute('data-theme',s);
    const b=document.getElementById('theme-btn');if(b)b.textContent=s==='dark'?'◑ Dark':'☀ Light';}
})();

// ── Tabs ───────────────────────────────────────────────────────────────────────
function switchTab(name,el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('pane-'+name).classList.add('active');
  if(name==='brief')renderBriefPane();
}

// ── Ticker ─────────────────────────────────────────────────────────────────────
function renderTicker(items){
  if(!items||!items.length)return;
  const html=items.map(i=>
    `<span class="ticker-item" data-id="${esc(i.id||'')}">
      <span class="ticker-score">${i.score}</span>
      <span class="ticker-title">${esc(i.title.substring(0,60))}${i.title.length>60?'…':''}</span>
      <span class="ticker-src">${esc(i.source)}</span>
      <span class="ticker-sep">·</span>
    </span>`
  ).join('');
  // Duplicate for seamless loop
  document.getElementById('ticker-inner').innerHTML=html+html;
}

// Ticker click — delegated on the track so it fires even on duplicated nodes
document.getElementById('ticker-inner').addEventListener('click',function(e){
  const item=e.target.closest('.ticker-item');
  if(!item||!item.dataset.id)return;
  openSignalFromTicker(item.dataset.id);
});
function openSignalFromTicker(id){
  // Switch to Top Signals tab (signal is always rendered there)
  const topTab=document.querySelector('.tab[data-tab="top"]');
  if(topTab) switchTab('top',topTab);
  // Small delay so the pane renders before we scroll
  setTimeout(()=>focusSig(id),50);
}

// ── Fetch ──────────────────────────────────────────────────────────────────────
async function loadAll(){
  const [dRes,sRes]=await Promise.all([fetch('/api/signals'),fetch('/api/stats')]);
  _data=await dRes.json(); _stats=await sRes.json();
  renderStats(_stats);
  renderBrands(_stats.brands||[]);
  renderBreaking(_data.breaking||[]);
  renderTicker(_stats.ticker||[]);
  renderAllPanes(_data);
  const d=new Date(_data.last_updated+'Z');
  document.getElementById('chrome-meta').textContent='Updated '+d.toLocaleTimeString();
  const today=new Date().toLocaleDateString('en-US',{weekday:'long',month:'long',day:'numeric',year:'numeric'}).toUpperCase();
  document.getElementById('hero-eyebrow').textContent='LIVE INTELLIGENCE · '+today;
  document.getElementById('hero-sub').textContent=
    `Algorithmic engine. ${_stats.total} signals · ${_stats.act_count} act-tier · ${_stats.watch_count} watch-tier · ${_stats.reddit_signals} from Reddit.`;
}

function renderStats(s){
  document.getElementById('s-act').textContent   =s.act_count??'—';
  document.getElementById('s-watch').textContent =s.watch_count??'—';
  document.getElementById('s-repos').textContent =s.trending_repos??'—';
  document.getElementById('s-reddit').textContent=s.reddit_signals??'—';
  document.getElementById('s-top').textContent   =s.top_story_score?s.top_story_score+'/10':'—';
  document.getElementById('s-top-title').textContent=s.top_story||'—';
}

function renderBrands(brands){
  const el=document.getElementById('brands-scroll');
  if(!brands.length){el.innerHTML='<span style="color:var(--muted);font-size:13px">Detecting brands…</span>';return;}
  el.innerHTML=brands.map(b=>
    `<div class="brand-pill">
      <span class="brand-name">${esc(b.name)}</span>
      <span class="brand-tag">${esc(b.tag)}</span>
    </div>`
  ).join('');
}

function renderBreaking(items){
  const b=document.getElementById('breaking'),l=document.getElementById('breaking-items');
  if(!items.length){b.classList.remove('show');return;}
  b.classList.add('show');
  l.innerHTML=items.map(i=>
    `<span class="breaking-item" data-id="${esc(i.id)}">${esc(i.title.substring(0,70))}${i.title.length>70?'…':''}</span>`
  ).join('');
  l.onclick=function(e){
    const item=e.target.closest('.breaking-item');
    if(item&&item.dataset.id) openSignalFromTicker(item.dataset.id);
  };
}

function renderAllPanes(data){
  const g=data.grouped||{};
  // Overview: chronological (newest first), all sources
  const top=(data.chrono||data.signals);
  const gh=(g['Open Source']||[]).filter(i=>i.source==='GitHub Trending');
  const rd=(data.chrono||data.signals).filter(i=>i.id&&i.id.startsWith('reddit'));
  document.getElementById('pane-top').innerHTML   =sigList(top);
  document.getElementById('pane-ai').innerHTML    =sigList(g['AI & Tech']||[]);
  document.getElementById('pane-apple').innerHTML =sigList(g['Apple']||[]);
  document.getElementById('pane-geo').innerHTML   =sigList(g['Geopolitics']||[]);
  document.getElementById('pane-oss').innerHTML   =sigList(g['Open Source']||[]);
  document.getElementById('pane-github').innerHTML=sigList(gh);
  document.getElementById('pane-reddit').innerHTML=sigList(rd);
}

function sigList(items){
  if(!items.length)return`<div class="empty"><strong>No signals yet</strong><p>Sources poll every 10 minutes.</p></div>`;
  return`<div class="signal-list">${items.map(sigRow).join('')}</div>`;
}

function sc(s){s=Math.round(s||1);return s>=9?'s10':s>=8?'s8':s>=7?'s7':s>=6?'s6':s>=5?'s5':'s4'}

function ago(pub){
  if(!pub)return'';
  try{
    const d=(Date.now()-new Date((pub.includes('Z')||pub.includes('+'))?pub:pub+'Z'))/1000;
    if(d<60)return'just now';if(d<3600)return Math.round(d/60)+'m ago';
    if(d<86400)return Math.round(d/3600)+'h ago';return Math.round(d/86400)+'d ago';
  }catch{return''}
}

function sigRow(item){
  const s=item.score||1;
  const t=ago(item.published);
  const eng=item.hn_score?` · ▲${item.hn_score}`:item.reddit_score?` · ▲${item.reddit_score}`:item.stars_today>0?` · +${item.stars_today}⭐`:'';
  const clust=item.cluster_count>1?`<span class="cluster-badge">⬡ ${item.cluster_count} sources</span>`:'';
  const hasSummary=!!(item.summary_extracted||item.summary);
  const sumText=(item.summary_extracted||item.summary||'').substring(0,200);
  // Encode id safely for data attributes (no JS injection risk)
  return`
<div class="signal-row" id="row-${esc(item.id)}" data-id="${esc(item.id)}" data-url="${esc(item.url)}">
  <div class="sig-score"><div class="score-badge ${sc(s)}">${s}</div></div>
  <div class="sig-body">
    <div class="sig-title">${esc(item.title)}</div>
    ${sumText?`<div class="sig-summary">${esc(sumText)}</div>`:''}
    <div class="sig-meta">
      <span class="sig-source">${esc(item.source)}</span>
      ${clust}
      <span class="sig-domain">${esc(item.domain||'')}${eng}</span>
      <span class="sig-time">${t}</span>
    </div>
    <div class="sig-expand">
        <div class="expand-actions">
        <a href="${esc(item.url)}" target="_blank" rel="noopener"><button class="btn btn-open-link">↗ Open</button></a>
        <button class="btn btn-tldr">⚡ Get TL;DR</button>
        ${hasSummary?'<button class="btn btn-full-summary">📄 Full summary</button>':''}
      </div>
      ${hasSummary?`<div class="article-box" id="art-${esc(item.id)}">${esc(item.summary||'')}</div>`:''}
      <div class="claude-panel" id="cp-${esc(item.id)}">
        <div class="claude-label">✦ Analysis</div>
        <div class="claude-body" id="cb-${esc(item.id)}"></div>
      </div>
    </div>
  </div>
</div>`;
}

// ── Delegated event handling (no inline onclick — works with any id chars) ──────
document.addEventListener('click', function(e){
  // Don't toggle when clicking a link or button inside the row
  const openBtn=e.target.closest('.btn-open-link');
  if(openBtn) return; // let <a> handle it

  const tldrBtn=e.target.closest('.btn-tldr');
  if(tldrBtn){e.stopPropagation();const row=tldrBtn.closest('.signal-row');if(row)openTldr(row.dataset.id);return;}

  const sumBtn=e.target.closest('.btn-full-summary');
  if(sumBtn){e.stopPropagation();const row=sumBtn.closest('.signal-row');if(row)toggleArt(row.dataset.id,sumBtn);return;}

  const row=e.target.closest('.signal-row');
  if(row) toggleRow(row.dataset.id);
});

function toggleRow(id){
  if(!id)return;
  const r=document.getElementById('row-'+id);if(!r)return;
  const was=r.classList.contains('expanded');
  // Collapse siblings
  r.closest('.signal-list')?.querySelectorAll('.signal-row.expanded').forEach(x=>{if(x!==r)x.classList.remove('expanded')});
  r.classList.toggle('expanded',!was);
}
function focusSig(id){
  const r=document.getElementById('row-'+id);
  if(r){r.classList.add('expanded');r.scrollIntoView({behavior:'smooth',block:'center'});}
}
function toggleArt(id,btn){
  const el=document.getElementById('art-'+id);if(!el)return;
  const s=el.classList.toggle('show');btn.textContent=s?'📄 Hide':'📄 Full summary';
}
function openTldr(id){
  if(!id)return;
  const all=(_data?.signals||[]).concat(_data?.chrono||[]);
  const sig=all.find(s=>s.id===id);
  if(!sig)return;
  const body=(sig.summary_extracted||sig.summary||'').substring(0,800);
  const prompt='Give me a TL;DR of this article:\n\nTitle: '+sig.title+'\nSource: '+sig.source+(body?'\n\n'+body:'')+(sig.url?'\n\nURL: '+sig.url:'');
  window.open('https://chatgpt.com/?q='+encodeURIComponent(prompt),'_blank');
}

// ── Fetch Now button ────────────────────────────────────────────────────────────
async function fetchNow(btn){
  btn.disabled=true;btn.textContent='Fetching…';
  try{
    await fetch('/api/fetch-now',{method:'POST'});
    // Poll until total changes or 30s
    const before=_stats?.total||0;
    let waited=0;
    const iv=setInterval(async()=>{
      waited+=2;
      await loadAll();
      if(_stats?.total!==before||waited>=30){clearInterval(iv);btn.disabled=false;btn.textContent='⟳ Fetch Now';}
    },2000);
  }catch{btn.disabled=false;btn.textContent='⟳ Fetch Now';}
}

// ── Weekly Brief (algo) ────────────────────────────────────────────────────────
async function renderBriefPane(){
  const pane=document.getElementById('pane-brief');
  pane.innerHTML='<div class="brief-wrap"><div style="padding:40px 0;text-align:center"><span class="spin" style="width:18px;height:18px"></span></div></div>';
  try{
    const r=await fetch('/api/weekly-brief');
    const d=await r.json();
    _brief=d;
    let html='<div class="brief-wrap">';
    // Header
    const t=new Date(d.generated_at+'Z').toLocaleString();
    html+=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <div style="font-size:17px;font-weight:700">Intelligence Brief</div>
      <div style="font-size:11px;color:var(--muted)">Algorithmic · ${t} · ${d.signal_count} signals</div>
    </div>`;

    // Act tier
    if(d.act_tier?.length){
      html+=`<div class="brief-card"><h3>🔴 Act Now — Tier 8–10</h3>`;
      d.act_tier.forEach(i=>{
        html+=`<div class="brief-row">
          <div class="brief-score"><div class="score-badge ${sc(i.score)}">${i.score}</div></div>
          <div>
            <div class="brief-title"><a href="${esc(i.url)}" target="_blank">${esc(i.title)}</a></div>
            <div class="brief-meta">${esc(i.source)}</div>
            ${i.summary?`<div class="brief-sum">${esc(i.summary)}</div>`:''}
          </div>
        </div>`;
      });
      html+=`</div>`;
    }

    // Watch tier
    if(d.watch_tier?.length){
      html+=`<div class="brief-card"><h3>🟡 Watch Closely — Tier 5–7</h3>`;
      d.watch_tier.slice(0,8).forEach(i=>{
        html+=`<div class="brief-row">
          <div class="brief-score"><div class="score-badge ${sc(i.score)}">${i.score}</div></div>
          <div>
            <div class="brief-title"><a href="${esc(i.url)}" target="_blank">${esc(i.title)}</a></div>
            <div class="brief-meta">${esc(i.source)}</div>
          </div>
        </div>`;
      });
      html+=`</div>`;
    }

    // Cross-source clusters
    if(d.top_clusters?.length){
      html+=`<div class="brief-card"><h3>⬡ Cross-Source Clusters</h3>`;
      d.top_clusters.forEach(c=>{
        html+=`<div class="cluster-row">
          <div class="cluster-num">${c.cluster_count}</div>
          <div>
            <div class="brief-title">${esc(c.title)}</div>
            <div class="brief-meta">${esc((c.cluster_sources||[]).join(' · '))}</div>
          </div>
        </div>`;
      });
      html+=`</div>`;
    }

    // Velocity movers
    if(d.velocity_movers?.filter(v=>v.velocity>0).length){
      html+=`<div class="brief-card"><h3>⚡ Velocity Movers</h3>`;
      d.velocity_movers.filter(v=>v.velocity>0).forEach(v=>{
        html+=`<div class="cluster-row">
          <div class="vel-num">+${Math.round(v.velocity)}/h</div>
          <div>
            <div class="brief-title">${esc(v.title)}</div>
            <div class="brief-meta">Score ${v.score}/10</div>
          </div>
        </div>`;
      });
      html+=`</div>`;
    }

    // Source breakdown
    if(d.source_breakdown){
      const entries=Object.entries(d.source_breakdown).sort((a,b)=>b[1]-a[1]).slice(0,8);
      html+=`<div class="brief-card"><h3>📡 Source Breakdown</h3>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:4px">`;
      entries.forEach(([src,cnt])=>{
        html+=`<div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;
          padding:4px 10px;font-size:12px"><strong>${cnt}</strong> <span style="color:var(--muted)">${esc(src)}</span></div>`;
      });
      html+=`</div></div>`;
    }

    html+='</div>';
    pane.innerHTML=html;
  }catch(e){
    pane.innerHTML=`<div class="brief-wrap"><p style="color:var(--red)">Error: ${e.message}</p></div>`;
  }
}

function renderMd(text){
  if(!text)return'';
  return text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/^### (.+)$/gm,'<h3 style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:14px 0 6px">$1</h3>')
    .replace(/^## (.+)$/gm,'<h2 style="font-size:15px;font-weight:700;margin:18px 0 8px">$1</h2>')
    .replace(/^---$/gm,'<hr style="border:none;border-top:1px solid var(--border);margin:14px 0">')
    .replace(/^- (.+)$/gm,'<li>$1</li>')
    .replace(/(<li>.*?<\/li>\n?)+/gs,'<ul style="padding-left:18px;margin-bottom:10px">$&</ul>')
    .replace(/\n\n+/g,'</p><p>').replace(/^/,'<p>').replace(/$/,'</p>')
    .replace(/<p><\/p>/g,'').replace(/<p>(<[hul])/g,'$1').replace(/(<\/[hul][^>]*>)<\/p>/g,'$1');
}

function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function init(){
  await loadAll();
  document.getElementById('loading').classList.add('out');
  setTimeout(()=>document.getElementById('loading').style.display='none',400);
  setInterval(loadAll,10*60*1000); // refresh every 10 min
}
init();
</script>
</body>
</html>"""

@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    load_signals()
    scheduler = BackgroundScheduler(daemon=True)
    # Fast: HN + Reddit every 10 min
    scheduler.add_job(run_fast,  IntervalTrigger(minutes=10), id='fast',  next_run_time=datetime.now())
    # Full: all sources every 30 min
    scheduler.add_job(run_full,  IntervalTrigger(minutes=30), id='full',  next_run_time=datetime.now())
    # ArXiv hourly
    scheduler.add_job(run_arxiv, IntervalTrigger(hours=1),    id='arxiv', next_run_time=datetime.now())
    scheduler.start()
    port = int(CFG.get('port', 8765))
    log.info(f'Signal Dashboard → http://localhost:{port}')
    log.info(f'Sources: HN Algolia, Reddit × {len(SUBREDDITS)}, RSS × {len(RSS_FEEDS)}, GitHub, ArXiv')
    host = os.environ.get('SIGNAL_HOST', '0.0.0.0')
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    finally:
        scheduler.shutdown()
