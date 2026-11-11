from fastapi import FastAPI, APIRouter, UploadFile, File, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, date
from datetime import time as dtime
from typing import Optional, List
import csv, io, json, os, re, sqlite3, statistics, time, hmac, threading, contextvars, uuid
import urllib.request, urllib.parse, urllib.error

BASE = Path(__file__).parent
DATA_DIR = Path(os.getenv('DATA_DIR') or (BASE / 'data'))
DB_PATH = DATA_DIR / 'wos_voc.sqlite3'

app = FastAPI(title='WOS VoC')
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')


# ----------------------------------------------------------------------------
# Доступ (необязательный): если на сервере задан APP_ACCESS_KEY, все /api/*
# кроме /api/status и /api/sample требуют заголовок X-Access-Key.
# ----------------------------------------------------------------------------
def access_key():
    return os.getenv('APP_ACCESS_KEY', '').strip()


def require_access(x_access_key: str = Header(default='')):
    key = access_key()
    if key and not hmac.compare_digest(x_access_key.encode('utf-8'), key.encode('utf-8')):
        raise HTTPException(401, 'Нужен ключ доступа.')


api = APIRouter(prefix='/api', dependencies=[Depends(require_access)])


# ----------------------------------------------------------------------------
# База данных (SQLite)
# ----------------------------------------------------------------------------
SCHEMA = '''
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  marketplace TEXT NOT NULL,
  external_id TEXT NOT NULL,
  sku TEXT,
  article TEXT,
  name TEXT NOT NULL,
  brand TEXT,
  category TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(marketplace, external_id)
);
CREATE TABLE IF NOT EXISTS collections(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_products(
  collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  PRIMARY KEY(collection_id, product_id)
);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  marketplace TEXT NOT NULL,
  external_id TEXT NOT NULL,
  product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
  product_name TEXT,
  rating REAL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  UNIQUE(marketplace, external_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_product ON reviews(product_id, created_at);
'''
_schema_ready = False


def utcnow_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


@contextmanager
def db():
    global _schema_ready
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        if not _schema_ready:
            conn.executescript(SCHEMA)
            conn.commit()
            _schema_ready = True
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Даты и период
# ----------------------------------------------------------------------------
def parse_dt(value):
    """Разбирает ISO-дату в UTC. Возвращает None, если дату понять нельзя."""
    if not value:
        return None
    s = str(value).strip().replace('Z', '+00:00')
    s = re.sub(r'\.(\d+)', lambda m: '.' + (m.group(1) + '000000')[:6], s)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def iso(d):
    return d.strftime('%Y-%m-%dT%H:%M:%SZ')


def period_bounds(date_from, date_to):
    today = datetime.now(timezone.utc).date()
    try:
        d_to = date.fromisoformat(date_to) if date_to else today
        d_from = date.fromisoformat(date_from) if date_from else d_to - timedelta(days=29)
    except ValueError:
        raise HTTPException(400, 'Дата должна быть в формате ГГГГ-ММ-ДД.')
    if d_from > d_to:
        raise HTTPException(400, 'Дата начала периода позже даты окончания.')
    if (d_to - d_from).days > 366:
        raise HTTPException(400, 'Период не должен быть длиннее 366 дней.')
    start = datetime.combine(d_from, dtime.min, tzinfo=timezone.utc)
    end = datetime.combine(d_to, dtime.max.replace(microsecond=0), tzinfo=timezone.utc)
    return start, end, d_from.isoformat(), d_to.isoformat()


# ----------------------------------------------------------------------------
# Коннекторы маркетплейсов (официальные seller API)
# ВАЖНО: без токенов продавца они не проверены на реальных данных.
# ----------------------------------------------------------------------------
class ConnectorError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


@app.exception_handler(ConnectorError)
async def connector_error_handler(request, exc):
    return JSONResponse({'detail': exc.message}, status_code=exc.status)


MARKETPLACES = {'wildberries': 'Wildberries', 'ozon': 'Ozon'}
WB_CONTENT = 'https://content-api.wildberries.ru'
WB_FEEDBACKS = 'https://feedbacks-api.wildberries.ru'
OZON_API = 'https://api-seller.ozon.ru'


CREDS = contextvars.ContextVar('wos_client_creds', default=None)


def wb_token():
    c = CREDS.get()
    if c is not None:  # ключ клиента из формы на сайте; ключи сервера не используются
        return c.get('wb', '')
    return os.getenv('WB_API_TOKEN', '').strip()


def ozon_creds():
    c = CREDS.get()
    if c is not None:
        return c.get('oz_id', ''), c.get('oz_key', '')
    return os.getenv('OZON_CLIENT_ID', '').strip(), os.getenv('OZON_API_KEY', '').strip()


def is_configured(mp):
    if mp == 'wildberries':
        return bool(wb_token())
    if mp == 'ozon':
        cid, key = ozon_creds()
        return bool(cid and key)
    return False


def require_configured(mp):
    if mp == 'wildberries' and not is_configured(mp):
        raise ConnectorError('Wildberries не подключён: на сервере не задан WB_API_TOKEN.', 503)
    if mp == 'ozon' and not is_configured(mp):
        raise ConnectorError('Ozon не подключён: на сервере не заданы OZON_CLIENT_ID и OZON_API_KEY.', 503)


def _http_error_text(code):
    if code in (401, 403):
        return 'API маркетплейса отклонил ключ (%s): токен недействителен, просрочен или не имеет нужных прав.' % code
    if code == 429:
        return 'Превышен лимит запросов API маркетплейса (429). Повторите через минуту.'
    if code == 400:
        return 'API маркетплейса отклонил запрос (400). Возможно, изменился формат метода.'
    if code == 404:
        return 'Метод API не найден (404). Возможно, он изменился или недоступен для вашего аккаунта.'
    if code >= 500:
        return 'Сервер маркетплейса временно недоступен (%s).' % code
    return 'API маркетплейса вернул ошибку %s.' % code


def http_json(method, url, headers, body=None, params=None, timeout=45):
    if params:
        url += '?' + urllib.parse.urlencode(params)
    data = json.dumps(body).encode('utf-8') if body is not None else None
    h = {'Accept': 'application/json'}
    h.update(headers)
    if data is not None:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ConnectorError(_http_error_text(e.code), 429 if e.code == 429 else 502)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectorError('Нет соединения с API маркетплейса: ' + type(e).__name__, 504)
    try:
        return json.loads(raw.decode('utf-8'))
    except ValueError:
        raise ConnectorError('API маркетплейса вернул ответ не в формате JSON.', 502)


# ---- Wildberries ------------------------------------------------------------
def wb_fetch_products(max_items=20000):
    require_configured('wildberries')
    headers = {'Authorization': wb_token()}
    cursor = {'limit': 100}
    out = []
    while True:
        data = http_json('POST', WB_CONTENT + '/content/v2/get/cards/list', headers,
                         {'settings': {'cursor': cursor, 'filter': {'withPhoto': -1}}},
                         params={'locale': 'ru'})
        if isinstance(data, dict) and data.get('error'):
            raise ConnectorError('Wildberries: ' + str(data.get('errorText') or 'ошибка запроса'), 502)
        cards = (data or {}).get('cards') or []
        for c in cards:
            nm = c.get('nmID')
            if not nm:
                continue
            out.append({'external_id': str(nm), 'sku': None, 'article': str(c.get('vendorCode') or ''),
                        'name': str(c.get('title') or c.get('vendorCode') or ('Артикул ' + str(nm))),
                        'brand': str(c.get('brand') or ''), 'category': str(c.get('subjectName') or '')})
        cur = (data or {}).get('cursor') or {}
        if len(cards) < 100 or not cur.get('updatedAt') or len(out) >= max_items:
            break
        cursor = {'limit': 100, 'updatedAt': cur['updatedAt'], 'nmID': cur.get('nmID')}
        time.sleep(0.7)
    return out


def wb_review_text(f):
    parts = []
    if str(f.get('pros') or '').strip():
        parts.append('Достоинства: ' + str(f['pros']).strip())
    if str(f.get('cons') or '').strip():
        parts.append('Недостатки: ' + str(f['cons']).strip())
    if str(f.get('text') or '').strip():
        parts.append(str(f['text']).strip())
    return ' '.join(parts)


def wb_fetch_reviews(nm_ids, start, end, page=1000, max_pages=190):
    """Отзывы WB за период по набору nmId. Три потока: без ответа, с ответом, архив."""
    require_configured('wildberries')
    headers = {'Authorization': wb_token()}
    streams = [('/api/v1/feedbacks', {'isAnswered': 'false'}),
               ('/api/v1/feedbacks', {'isAnswered': 'true'}),
               ('/api/v1/feedbacks/archive', {})]
    found = {}
    for path, extra in streams:
        skip = 0
        for _ in range(max_pages):
            params = {'take': page, 'skip': skip, 'order': 'dateDesc'}
            params.update(extra)
            if 'archive' not in path:
                params['dateFrom'] = int(start.timestamp())
                params['dateTo'] = int(end.timestamp())
            data = http_json('GET', WB_FEEDBACKS + path, headers, params=params)
            if isinstance(data, dict) and data.get('error'):
                raise ConnectorError('Wildberries: ' + str(data.get('errorText') or 'ошибка запроса'), 502)
            feedbacks = (((data or {}).get('data')) or {}).get('feedbacks') or []
            reached_start = False
            for f in feedbacks:
                created = parse_dt(f.get('createdDate'))
                if created is None:
                    continue
                if created < start:
                    reached_start = True
                    continue
                if created > end:
                    continue
                nm = str((f.get('productDetails') or {}).get('nmId') or '')
                if nm not in nm_ids:
                    continue
                text = wb_review_text(f)
                if not text or not f.get('id'):
                    continue
                rating = f.get('productValuation')
                found[str(f['id'])] = {'id': str(f['id']), 'key': nm, 'rating': float(rating) if rating else None,
                                       'text': text, 'created': created}
            if reached_start or len(feedbacks) < page:
                break
            skip += page
            time.sleep(0.4)
        time.sleep(0.4)
    return list(found.values())


# ---- Ozon -------------------------------------------------------------------
def ozon_headers():
    cid, key = ozon_creds()
    return {'Client-Id': cid, 'Api-Key': key}


def ozon_fetch_products(max_items=20000):
    require_configured('ozon')
    headers = ozon_headers()
    listed, last_id = [], ''
    while True:
        data = http_json('POST', OZON_API + '/v3/product/list', headers,
                         {'filter': {'visibility': 'ALL'}, 'last_id': last_id, 'limit': 1000})
        res = (data or {}).get('result') or {}
        page = res.get('items') or []
        listed.extend(page)
        last_id = res.get('last_id') or ''
        if not page or not last_id or len(listed) >= max_items:
            break
        time.sleep(0.3)
    ids = [i['product_id'] for i in listed if i.get('product_id')]
    out = []
    for n in range(0, len(ids), 100):
        data = http_json('POST', OZON_API + '/v3/product/info/list', headers, {'product_id': ids[n:n + 100]})
        items = (data or {}).get('items')
        if items is None:
            items = ((data or {}).get('result') or {}).get('items') or []
        for it in items:
            pid = it.get('id') or it.get('product_id')
            if not pid:
                continue
            sku = it.get('sku') or next((s.get('sku') for s in (it.get('sources') or []) if s.get('sku')), None)
            out.append({'external_id': str(pid), 'sku': str(sku) if sku else None,
                        'article': str(it.get('offer_id') or ''),
                        'name': str(it.get('name') or it.get('offer_id') or ('Товар ' + str(pid))),
                        'brand': '', 'category': ''})
        time.sleep(0.3)
    return out


def ozon_fetch_reviews(skus, start, end, max_pages=500):
    """Отзывы Ozon за период по набору SKU. Метод требует подписку Premium Plus."""
    require_configured('ozon')
    headers = ozon_headers()
    found, last_id = {}, ''
    for _ in range(max_pages):
        data = http_json('POST', OZON_API + '/v1/review/list', headers,
                         {'limit': 100, 'last_id': last_id, 'sort_dir': 'DESC', 'status': 'ALL'})
        reviews = (data or {}).get('reviews') or []
        reached_start = False
        for r in reviews:
            created = parse_dt(r.get('published_at'))
            if created is None:
                continue
            if created < start:
                reached_start = True
                continue
            if created > end:
                continue
            sku = str(r.get('sku') or '')
            text = str(r.get('text') or '').strip()
            if sku not in skus or not text or not r.get('id'):
                continue
            rating = r.get('rating')
            found[str(r['id'])] = {'id': str(r['id']), 'key': sku, 'rating': float(rating) if rating else None,
                                   'text': text, 'created': created}
        last_id = (data or {}).get('last_id') or ''
        if reached_start or not reviews or not (data or {}).get('has_next') or not last_id:
            break
        time.sleep(0.3)
    return list(found.values())


PRODUCT_FETCHERS = {'wildberries': wb_fetch_products, 'ozon': ozon_fetch_products}
REVIEW_FETCHERS = {'wildberries': (wb_fetch_reviews, 'external_id'), 'ozon': (ozon_fetch_reviews, 'sku')}


# ----------------------------------------------------------------------------
# Анализ отзывов
# ----------------------------------------------------------------------------
SAMPLE = [
    {"source": "Wildberries · ДЕМО", "product": "Платье миди базовое", "rating": 5, "date": "2026-09-12", "text": "Ткань приятная, цвет красивый, хорошо сидит по фигуре."},
    {"source": "Wildberries · ДЕМО", "product": "Платье миди базовое", "rating": 3, "date": "2026-09-14", "text": "На мой рост длинновато, размер соответствует, но ткань тоньше чем ожидала."},
    {"source": "Ozon · ДЕМО", "product": "Футболка oversize", "rating": 4, "date": "2026-09-15", "text": "Хороший хлопок, после стирки форму сохранила. Цвет немного отличается от фото."},
    {"source": "Ozon · ДЕМО", "product": "Футболка oversize", "rating": 2, "date": "2026-09-17", "text": "Размер очень большой, швы неровные, пришлось вернуть."},
    {"source": "Wildberries · ДЕМО", "product": "Джинсы straight", "rating": 5, "date": "2026-09-19", "text": "Посадка удобная, джинсы качественные, цвет как на фото."},
    {"source": "Ozon · ДЕМО", "product": "Джинсы straight", "rating": 3, "date": "2026-09-20", "text": "Талия узкая, длина подошла. Хотелось бы больше информации по длине изделия."},
    {"source": "Wildberries · ДЕМО", "product": "Кардиган базовый", "rating": 4, "date": "2026-09-22", "text": "Мягкий и приятный материал, но катышки появились после нескольких носок."},
    {"source": "Ozon · ДЕМО", "product": "Кардиган базовый", "rating": 1, "date": "2026-09-24", "text": "Цвет хороший, но швы разошлись и размер оказался меньше заявленного."},
    {"source": "Wildberries · ДЕМО", "product": "Юбка плиссе", "rating": 5, "date": "2026-09-25", "text": "Отлично выглядит, аккуратно сшита, упаковка хорошая."},
    {"source": "Ozon · ДЕМО", "product": "Юбка плиссе", "rating": 2, "date": "2026-09-26", "text": "Ткань электризуется, длина не совпала с описанием."},
]

THEMES = {
    'Размер и посадка': ['размер', 'маломер', 'большемер', 'посадк', 'талия', 'рост', 'длин', 'коротк', 'широк', 'узк', 'свободн'],
    'Материал и ткань': ['ткан', 'материал', 'хлопок', 'синтет', 'мягк', 'тонк', 'плотн', 'электриз', 'катыш'],
    'Качество пошива': ['шов', 'шв', 'строч', 'нитк', 'разош', 'пошив', 'дыр', 'качество'],
    'Цвет и фото': ['цвет', 'фото', 'оттенок', 'выглядит', 'картинк'],
    'Описание товара': ['описан', 'информац', 'характерист', 'таблиц', 'длина изделия'],
    'Доставка и упаковка': ['достав', 'упаков', 'короб', 'пакет'],
    'Комфорт и внешний вид': ['удоб', 'красив', 'приятн', 'выгляд', 'стиль', 'сидит', 'аккурат'],
}
POS = ['приятн', 'хорош', 'качеств', 'красив', 'удоб', 'отличн', 'мягк', 'подош', 'сохрани', 'аккурат', 'рекоменд', 'люблю']
NEG = ['плох', 'разош', 'дыр', 'вернул', 'не подош', 'тоньше', 'отличается', 'катыш', 'электриз', 'узк', 'маломер', 'не совпал', 'ожидал', 'неудоб', 'брак']
PROBLEM_THEMES = ['Размер и посадка', 'Материал и ткань', 'Качество пошива', 'Цвет и фото', 'Описание товара']
THEME_ACTIONS = {
    'Размер и посадка': ('Уточнить размерную сетку и посадку', 'Проверить мерки изделия, посадку по росту и указать параметры модели.'),
    'Материал и ткань': ('Проверить описание состава и свойства ткани', 'Добавить точный состав, плотность и рекомендации по уходу, если это подтверждено характеристиками изделия.'),
    'Качество пошива': ('Проверить качество пошива по проблемным моделям', 'Сгруппировать претензии по артикулам и проверить повторяемость дефектов.'),
    'Цвет и фото': ('Сверить фото с реальным оттенком', 'Проверить цветопередачу и добавить фото при нейтральном освещении.'),
    'Описание товара': ('Закрыть пробелы в карточке товара', 'Добавить недостающие измерения и таблицу параметров.'),
}


def norm(row):
    low = {str(k).strip().lower(): v for k, v in row.items() if k is not None}

    def pick(*keys):
        for k in keys:
            if k in low and low[k] not in (None, ''):
                return low[k]
        return ''
    text = str(pick('text', 'review', 'review_text', 'отзыв', 'текст', 'comment', 'комментарий', 'body')).strip()
    if not text:
        return None
    rating_raw = pick('rating', 'score', 'оценка', 'звезды', 'звёзды')
    try:
        rating = float(str(rating_raw).replace(',', '.')) if rating_raw not in ('', None) else None
    except ValueError:
        rating = None
    return {'source': str(pick('source', 'marketplace', 'источник', 'площадка')) or 'Импортированный файл',
            'product': str(pick('product', 'product_name', 'товар', 'название', 'offer_name', 'sku')) or 'Не указан',
            'rating': rating, 'date': str(pick('date', 'created_at', 'дата', 'published_at')), 'text': text,
            'url': str(pick('url', 'link', 'ссылка', 'review_url')), 'is_demo': bool(low.get('is_demo'))}


def classify(text, rating=None):
    t = text.lower()
    found = [theme for theme, words in THEMES.items() if any(w in t for w in words)]
    if not found:
        found = ['Общее впечатление']
    if rating is not None and 1 <= rating <= 5:
        sentiment = 'Позитивный' if rating >= 4 else ('Негативный' if rating <= 2 else 'Смешанный / нейтральный')
    else:
        pos = sum(t.count(w) for w in POS)
        neg = sum(t.count(w) for w in NEG)
        sentiment = 'Позитивный' if pos > neg else ('Негативный' if neg > pos else 'Смешанный / нейтральный')
    return found, sentiment


def ai_analyze(rows, result):
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        result['ai_error'] = 'ANTHROPIC_API_KEY не настроен; показан локальный анализ по ключевым словам.'
        return
    try:
        sample = [{'product': r['product'], 'rating': r['rating'], 'text': r['text'][:600], 'source': r['source']} for r in rows[:120]]
        prompt = {'task': 'Проанализируй отзывы покупателей fashion-бренда. Верни краткие русскоязычные выводы только по данным; не выдумывай факты. Проценты должны опираться на предоставленную выборку. Ответ: только JSON без пояснений, формат: summary (строка), strengths (массив строк), risks (массив строк), actions (массив объектов priority,title,detail).',
                  'sample_size': len(sample), 'total_reviews': len(rows), 'sample': sample,
                  'calculated_metrics': {'total': result['total'], 'themes': result['themes'], 'sentiment': result['sentiment']}}
        body = json.dumps({'model': os.getenv('ANTHROPIC_MODEL', 'claude-haiku-5-5'), 'max_tokens': 2000,
                           'messages': [{'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)}]}).encode('utf-8')
        req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=body, method='POST',
                                     headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as response:
            ai = json.loads(response.read().decode('utf-8'))
        text_out = ''.join(c.get('text', '') for c in ai.get('content', []) if c.get('type') == 'text')
        start, end = text_out.find('{'), text_out.rfind('}')
        if start != -1 and end > start:
            result['ai'] = json.loads(text_out[start:end + 1])
            result['ai_mode'] = 'claude'
    except Exception:
        result['ai_error'] = 'AI не ответил; показан локальный анализ. Проверьте ключ, модель и подключение.'


def report(rows, use_ai=False, brand=None, rows_limit=200):
    rows = [norm(r) for r in rows]
    rows = [r for r in rows if r]
    if not rows:
        raise HTTPException(400, 'Не найдено отзывов. Проверьте колонку с текстом отзыва.')
    total = len(rows)
    themes, sentiment, ratings = Counter(), Counter(), []
    issues = Counter()
    product_issues, product_total, product_ratings = {}, Counter(), {}
    examples = {}
    tagged = []
    for r in rows:
        cats, sent = classify(r['text'], r['rating'])
        tagged.append((r, cats, sent))
        sentiment[sent] += 1
        product_total[r['product']] += 1
        if r['rating'] is not None and 1 <= r['rating'] <= 5:
            ratings.append(r['rating'])
            product_ratings.setdefault(r['product'], []).append(r['rating'])
        for c in cats:
            themes[c] += 1
            if sent != 'Позитивный' and c in PROBLEM_THEMES:
                issues[c] += 1
                product_issues.setdefault(r['product'], Counter())[c] += 1
    # Примеры: сначала негативные и смешанные, чтобы цитаты показывали проблемы.
    for r, cats, sent in sorted(tagged, key=lambda x: x[2] == 'Позитивный'):
        for c in cats:
            lst = examples.setdefault(c, [])
            if len(lst) < 3:
                lst.append({'product': r['product'], 'text': r['text'], 'source': r['source'], 'rating': r['rating'], 'url': r['url']})
    theme_list = [{'name': k, 'count': v, 'percent': round(v / total * 100)} for k, v in themes.most_common()]
    positive, negative, mixed = sentiment['Позитивный'], sentiment['Негативный'], sentiment['Смешанный / нейтральный']
    products = []
    for name, counter in product_issues.items():
        top = counter.most_common(1)[0]
        pr = product_ratings.get(name)
        products.append({'name': name, 'mentions': sum(counter.values()), 'top_issue': top[0], 'reviews': product_total[name],
                         'avg_rating': round(statistics.mean(pr), 2) if pr else None, 'repeated': top[1] >= 2})
    products.sort(key=lambda x: x['mentions'], reverse=True)
    recs = []
    for theme, cnt in issues.most_common():
        pct = round(cnt / total * 100)
        priority = 'Высокий' if (pct >= 15 and cnt >= 3) else ('Средний' if cnt >= 2 else 'Наблюдать')
        title, action = THEME_ACTIONS[theme]
        recs.append({'priority': priority, 'title': title,
                     'detail': f"Тема «{theme}» встречается в {cnt} из {total} отзывов с оценкой ниже 4 или негативным текстом ({pct}% выборки). {action}"})
    if not recs:
        recs = [{'priority': 'Проверка', 'title': 'Повторяющихся проблем не найдено',
                 'detail': 'В этой выборке нет заметных жалоб по размеру, ткани, пошиву, цвету или описанию. Проверьте другой период или расширьте коллекцию.'}]
    notes = []
    if total < 30:
        notes.append(f'Выборка небольшая ({total} отзывов): выводы предварительные.')
    result = {'brand': brand or 'Выборка отзывов', 'total': total,
              'avg_rating': round(statistics.mean(ratings), 2) if ratings else None, 'rated_count': len(ratings),
              'sentiment': {'positive': positive, 'positive_pct': round(positive / total * 100),
                            'negative': negative, 'negative_pct': round(negative / total * 100),
                            'mixed': mixed, 'mixed_pct': round(mixed / total * 100)},
              'themes': theme_list, 'products': products[:12], 'recommendations': recs, 'examples': examples,
              'sources': dict(Counter(r['source'] for r in rows)), 'rows': rows[:rows_limit], 'rows_total': total,
              'notes': notes, 'ai_mode': 'rules', 'demo': any(r['is_demo'] for r in rows)}
    if use_ai:
        ai_analyze(rows, result)
    return result


# ----------------------------------------------------------------------------
# Работа с товарами, коллекциями и отзывами
# ----------------------------------------------------------------------------
def get_collection(conn, cid):
    row = conn.execute('SELECT * FROM collections WHERE id=?', (cid,)).fetchone()
    if not row:
        raise HTTPException(404, 'Коллекция не найдена.')
    return row


def collection_products(conn, cid):
    rows = conn.execute('''SELECT p.id, p.marketplace, p.external_id, p.sku, p.article, p.name, p.brand
                           FROM products p JOIN collection_products cp ON cp.product_id=p.id
                           WHERE cp.collection_id=? ORDER BY p.marketplace, p.name''', (cid,)).fetchall()
    return [dict(r) for r in rows]


def save_products(conn, marketplace, items):
    now = utcnow_iso()
    for it in items:
        conn.execute('''INSERT INTO products(marketplace, external_id, sku, article, name, brand, category, updated_at)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(marketplace, external_id) DO UPDATE SET
                          sku=COALESCE(excluded.sku, products.sku), article=excluded.article, name=excluded.name,
                          brand=excluded.brand, category=excluded.category, updated_at=excluded.updated_at''',
                     (marketplace, it['external_id'], it.get('sku'), it.get('article'), it['name'],
                      it.get('brand'), it.get('category'), now))


def refresh_reviews(conn, cid, start, end):
    """Тянет отзывы из API для товаров коллекции и сохраняет без дублей.
    Сначала все сетевые запросы, потом запись в БД (чтобы не держать блокировку БД на время сети)."""
    prods = collection_products(conn, cid)
    summary, fetched = {}, {}
    for mp, (fetcher, keyfield) in REVIEW_FETCHERS.items():
        mp_prods = [p for p in prods if p['marketplace'] == mp]
        if not mp_prods:
            continue
        info = {'products': len(mp_prods), 'fetched': 0, 'new': 0, 'error': None}
        summary[mp] = info
        pmap = {str(p[keyfield]): p for p in mp_prods if p[keyfield]}
        if not pmap:
            info['error'] = 'У товаров нет идентификатора SKU. Обновите список товаров Ozon.'
            continue
        try:
            items = fetcher(set(pmap), start, end)
        except ConnectorError as e:
            info['error'] = e.message
            continue
        info['fetched'] = len(items)
        fetched[mp] = (items, pmap)
    now = utcnow_iso()
    for mp, (items, pmap) in fetched.items():
        for it in items:
            prod = pmap[it['key']]
            cur = conn.execute('''INSERT INTO reviews(marketplace, external_id, product_id, product_name, rating, text, created_at, fetched_at)
                                  VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(marketplace, external_id) DO NOTHING''',
                               (mp, it['id'], prod['id'], prod['name'], it['rating'], it['text'], iso(it['created']), now))
            summary[mp]['new'] += cur.rowcount if cur.rowcount > 0 else 0
    return summary


def collection_review_rows(conn, cid, start, end):
    rows = conn.execute('''SELECT r.marketplace, r.product_name, r.rating, r.text, r.created_at
                           FROM reviews r JOIN collection_products cp ON cp.product_id=r.product_id
                           WHERE cp.collection_id=? AND r.created_at>=? AND r.created_at<=?
                           ORDER BY r.created_at DESC''', (cid, iso(start), iso(end))).fetchall()
    return [{'source': MARKETPLACES.get(r['marketplace'], r['marketplace']), 'product': r['product_name'] or 'Не указан',
             'rating': r['rating'], 'date': r['created_at'][:10], 'text': r['text'], 'url': ''} for r in rows]


# ----------------------------------------------------------------------------
# Модели запросов
# ----------------------------------------------------------------------------
class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class AddProductsIn(BaseModel):
    product_ids: List[int] = Field(min_length=1, max_length=2000)


class AnalyzeIn(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    use_ai: bool = True
    refresh: bool = True


# ----------------------------------------------------------------------------
# Маршруты: статус и главная
# ----------------------------------------------------------------------------
@app.get('/', response_class=HTMLResponse)
def index():
    return (BASE / 'static' / 'index.html').read_text(encoding='utf-8')


@app.get('/api/status')
def status():
    connectors = {}
    with db() as conn:
        for mp, label in MARKETPLACES.items():
            row = conn.execute('SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM products WHERE marketplace=?', (mp,)).fetchone()
            configured = is_configured(mp)
            connectors[mp] = {'label': label, 'configured': configured, 'products': row['n'], 'last_sync': row['last'],
                              'status': 'not_configured' if not configured else ('synced' if row['n'] else 'configured_unverified'),
                              'hint': None if configured else ('Задайте WB_API_TOKEN на сервере.' if mp == 'wildberries'
                                                               else 'Задайте OZON_CLIENT_ID и OZON_API_KEY на сервере.')}
    return {'analysis': 'local rules + optional Claude', 'ai_configured': bool(os.getenv('ANTHROPIC_API_KEY')),
            'access_required': bool(access_key()), 'self_serve': True, 'connectors': connectors}


@app.get('/api/sample')
def sample():
    return report([dict(r, is_demo=True) for r in SAMPLE], brand='Демо-выборка (синтетические примеры)')


# ----------------------------------------------------------------------------
# Маршруты: товары
# ----------------------------------------------------------------------------
@api.post('/marketplaces/{mp}/sync-products')
def sync_products(mp: str):
    if mp not in MARKETPLACES:
        raise HTTPException(404, 'Неизвестный маркетплейс.')
    require_configured(mp)
    items = PRODUCT_FETCHERS[mp]()
    with db() as conn:
        save_products(conn, mp, items)
        total = conn.execute('SELECT COUNT(*) AS n FROM products WHERE marketplace=?', (mp,)).fetchone()['n']
    return {'marketplace': mp, 'fetched': len(items), 'total_in_db': total}


@api.get('/products')
def list_products(marketplace: str = '', q: str = '', collection_id: int = 0, limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    where, args = [], []
    if marketplace:
        where.append('p.marketplace=?')
        args.append(marketplace)
    if q.strip():
        like = '%' + q.strip().replace('%', '').replace('_', '') + '%'
        where.append('(p.name LIKE ? OR p.article LIKE ? OR p.external_id LIKE ?)')
        args += [like, like, like]
    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    with db() as conn:
        total = conn.execute(f'SELECT COUNT(*) AS n FROM products p {clause}', args).fetchone()['n']
        rows = conn.execute(f'''SELECT p.id, p.marketplace, p.external_id, p.article, p.name, p.brand,
                                  EXISTS(SELECT 1 FROM collection_products cp WHERE cp.collection_id=? AND cp.product_id=p.id) AS in_collection
                                FROM products p {clause} ORDER BY p.name LIMIT ? OFFSET ?''', [collection_id] + args + [limit, offset]).fetchall()
    return {'items': [dict(r, in_collection=bool(r['in_collection'])) for r in rows], 'total': total}


# ----------------------------------------------------------------------------
# Маршруты: коллекции
# ----------------------------------------------------------------------------
@api.get('/collections')
def list_collections():
    with db() as conn:
        rows = conn.execute('''SELECT c.id, c.name, c.created_at,
                                 (SELECT COUNT(*) FROM collection_products cp WHERE cp.collection_id=c.id) AS products_count
                               FROM collections c ORDER BY c.name''').fetchall()
    return {'items': [dict(r) for r in rows]}


@api.post('/collections')
def create_collection(body: CollectionIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, 'Введите название коллекции.')
    with db() as conn:
        try:
            cur = conn.execute('INSERT INTO collections(name, name_key, created_at) VALUES(?,?,?)', (name, name.casefold(), utcnow_iso()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Коллекция с таким названием уже есть.')
        return {'id': cur.lastrowid, 'name': name, 'products_count': 0}


@api.get('/collections/{cid}')
def get_collection_details(cid: int):
    with db() as conn:
        col = get_collection(conn, cid)
        return {'id': col['id'], 'name': col['name'], 'products': collection_products(conn, cid)}


@api.delete('/collections/{cid}')
def delete_collection(cid: int):
    with db() as conn:
        get_collection(conn, cid)
        conn.execute('DELETE FROM collections WHERE id=?', (cid,))
    return {'deleted': cid}


@api.post('/collections/{cid}/products')
def add_collection_products(cid: int, body: AddProductsIn):
    with db() as conn:
        get_collection(conn, cid)
        ids = sorted(set(body.product_ids))
        marks = ','.join('?' * len(ids))
        existing = {r['id'] for r in conn.execute(f'SELECT id FROM products WHERE id IN ({marks})', ids).fetchall()}
        if not existing:
            raise HTTPException(400, 'Товары не найдены. Сначала загрузите список товаров маркетплейса.')
        added = 0
        for pid in existing:
            cur = conn.execute('INSERT OR IGNORE INTO collection_products(collection_id, product_id) VALUES(?,?)', (cid, pid))
            added += cur.rowcount if cur.rowcount > 0 else 0
        return {'added': added, 'skipped': len(ids) - added}


@api.delete('/collections/{cid}/products/{pid}')
def remove_collection_product(cid: int, pid: int):
    with db() as conn:
        get_collection(conn, cid)
        conn.execute('DELETE FROM collection_products WHERE collection_id=? AND product_id=?', (cid, pid))
    return {'removed': pid}


@api.post('/collections/{cid}/analyze')
def analyze_collection(cid: int, body: AnalyzeIn):
    start, end, d_from, d_to = period_bounds(body.date_from, body.date_to)
    with db() as conn:
        col = get_collection(conn, cid)
        n_products = conn.execute('SELECT COUNT(*) AS n FROM collection_products WHERE collection_id=?', (cid,)).fetchone()['n']
        if not n_products:
            raise HTTPException(400, 'В коллекции нет товаров. Добавьте товары и повторите.')
        fetch = refresh_reviews(conn, cid, start, end) if body.refresh else {}
        rows = collection_review_rows(conn, cid, start, end)
        collection_name = col['name']
    errors = [f"{MARKETPLACES[mp]}: {info['error']}" for mp, info in fetch.items() if info.get('error')]
    if not rows:
        reason = ' '.join(errors) if errors else f'За период {d_from} — {d_to} отзывов по товарам коллекции не найдено.'
        raise HTTPException(404, reason)
    result = report(rows, use_ai=body.use_ai, brand=collection_name)
    result['notes'].append('Учитываются только отзывы с текстом; оценки без текста в отчёт не попадают.')
    result.update({'period': {'from': d_from, 'to': d_to}, 'collection': {'id': cid, 'name': collection_name, 'products': n_products},
                   'fetch': fetch, 'fetch_errors': errors, 'origin': 'marketplace_api' if body.refresh else 'saved'})
    return result


# ----------------------------------------------------------------------------
# Маршруты: старые (совместимость) — анализ переданных отзывов и импорт файла
# ----------------------------------------------------------------------------
@api.post('/analyze')
def analyze(payload: dict):
    rows = payload.get('reviews', [])
    if not isinstance(rows, list):
        raise HTTPException(400, 'Ожидался массив отзывов.')
    return report(rows, bool(payload.get('use_ai', True)), brand=payload.get('brand'))


@api.post('/import')
async def import_file(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(413, 'Файл больше 12 МБ.')
    name = (file.filename or '').lower()
    if name.endswith('.csv'):
        decoded = None
        for enc in ('utf-8-sig', 'utf-8', 'cp1251'):
            try:
                decoded = data.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise HTTPException(400, 'Не удалось прочитать CSV. Сохраните файл в UTF-8.')
        try:
            dialect = csv.Sniffer().sniff(decoded[:4096], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(io.StringIO(decoded), dialect=dialect))
    elif name.endswith('.json'):
        try:
            rows = json.loads(data.decode('utf-8-sig'))
            rows = rows if isinstance(rows, list) else rows.get('reviews', [])
        except Exception:
            raise HTTPException(400, 'Некорректный JSON.')
    elif name.endswith(('.xlsx', '.xls')):
        raise HTTPException(400, 'XLSX пока не включён. Сохраните таблицу как CSV UTF-8 и загрузите снова.')
    else:
        raise HTTPException(400, 'Поддерживаются CSV и JSON.')
    return report(rows, use_ai=True, brand='Импорт: ' + (file.filename or 'файл'))


# ----------------------------------------------------------------------------
# Самостоятельный отчёт: клиент вводит СВОЙ ключ API на сайте и получает отчёт.
# Ключ хранится только в памяти фонового потока на время задачи, не пишется в БД и логи.
# ----------------------------------------------------------------------------
LIVE_JOBS = {}
LIVE_HITS = {}
LIVE_LOCK = threading.Lock()
LIVE_MAX_RUNNING = 3
LIVE_PER_HOUR = 6
LIVE_TTL = 1800


class LiveIn(BaseModel):
    marketplace: str
    wb_token: Optional[str] = None
    ozon_client_id: Optional[str] = None
    ozon_api_key: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    brand: Optional[str] = None


def _clean_secret(v, label, min_len):
    v = (v or '').strip()
    if len(v) < min_len:
        raise HTTPException(400, f'Вставьте {label}.')
    if len(v) > 4000 or re.search(r'\s', v):
        raise HTTPException(400, f'{label[0].upper()}{label[1:]} вставлен неверно: в нём не должно быть пробелов и переносов.')
    return v


def _live_set(jid, **kw):
    with LIVE_LOCK:
        if jid in LIVE_JOBS:
            LIVE_JOBS[jid].update(kw)


def _live_worker(jid, mp, creds, start, end, d_from, d_to, brand):
    CREDS.set(creds)
    try:
        label = MARKETPLACES[mp]
        _live_set(jid, progress=f'{label}: загружаем список товаров…')
        items = PRODUCT_FETCHERS[mp](5000)
        if not items:
            raise ConnectorError('В кабинете не найдено товаров. Проверьте, что ключ от нужного кабинета.', 404)
        fetcher, keyfield = REVIEW_FETCHERS[mp]
        pmap = {str(p[keyfield]): p for p in items if p.get(keyfield)}
        if not pmap:
            raise ConnectorError('У товаров нет идентификаторов для поиска отзывов.', 502)
        _live_set(jid, progress=f'Товаров: {len(items)}. Загружаем отзывы за период…')
        revs = fetcher(set(pmap), start, end)
        rows = [{'source': label, 'product': pmap[r['key']]['name'], 'rating': r['rating'],
                 'date': r['created'].strftime('%Y-%m-%d'), 'text': r['text'], 'url': ''} for r in revs]
        if not rows:
            raise ConnectorError(f'За период {d_from} — {d_to} отзывов с текстом не найдено. Расширьте период.', 404)
        _live_set(jid, progress=f'Отзывов: {len(rows)}. Анализируем…')
        result = report(rows, use_ai=bool(os.getenv('ANTHROPIC_API_KEY')), brand=brand or f'Кабинет {label}')
        result['notes'].append('Учитываются только отзывы с текстом; оценки без текста в отчёт не попадают.')
        result.update({'period': {'from': d_from, 'to': d_to}, 'origin': 'live',
                       'fetch': {mp: {'products': len(items), 'fetched': len(rows), 'new': len(rows)}}, 'fetch_errors': []})
        _live_set(jid, status='done', result=result, progress='Готово')
    except ConnectorError as e:
        _live_set(jid, status='error', error=e.message)
    except HTTPException as e:
        _live_set(jid, status='error', error=str(e.detail))
    except Exception:
        _live_set(jid, status='error', error='Не удалось сформировать отчёт. Попробуйте ещё раз позже.')
    finally:
        CREDS.set(None)


def live_start(body, ip='anon', run_async=True):
    mp = (body.marketplace or '').strip()
    if mp == 'wildberries':
        creds = {'wb': _clean_secret(body.wb_token, 'токен Wildberries', 20)}
    elif mp == 'ozon':
        creds = {'oz_id': _clean_secret(body.ozon_client_id, 'Client-Id Ozon', 3),
                 'oz_key': _clean_secret(body.ozon_api_key, 'Api-Key Ozon', 20)}
    else:
        raise HTTPException(400, 'Выберите Wildberries или Ozon.')
    start, end, d_from, d_to = period_bounds(body.date_from, body.date_to)
    brand = (body.brand or '').strip()[:80]
    now = time.time()
    with LIVE_LOCK:
        for k in [k for k, j in LIVE_JOBS.items() if now - j['created'] > LIVE_TTL]:
            del LIVE_JOBS[k]
        hits = [t for t in LIVE_HITS.get(ip, []) if now - t < 3600]
        if len(hits) >= LIVE_PER_HOUR:
            raise HTTPException(429, 'Слишком много запусков. Попробуйте через час.')
        if sum(1 for j in LIVE_JOBS.values() if j['status'] == 'running') >= LIVE_MAX_RUNNING:
            raise HTTPException(429, 'Сервер сейчас занят другими отчётами. Повторите через пару минут.')
        hits.append(now)
        LIVE_HITS[ip] = hits
        jid = uuid.uuid4().hex
        LIVE_JOBS[jid] = {'status': 'running', 'progress': 'Запускаем…', 'created': now, 'result': None, 'error': None}
    args = (jid, mp, creds, start, end, d_from, d_to, brand)
    if run_async:
        threading.Thread(target=_live_worker, args=args, daemon=True).start()
    else:
        _live_worker(*args)
    return {'job': jid}


def live_get(jid):
    with LIVE_LOCK:
        j = LIVE_JOBS.get(jid)
        if not j:
            raise HTTPException(404, 'Задача не найдена или устарела. Запустите отчёт заново.')
        out = {'status': j['status'], 'progress': j['progress'], 'error': j['error'], 'result': j['result']}
    return out


@app.post('/api/live/start')
def live_start_route(body: LiveIn, x_forwarded_for: str = Header(default='')):
    ip = x_forwarded_for.split(',')[0].strip() if x_forwarded_for else 'anon'
    return JSONResponse(live_start(body, ip or 'anon'), headers={'Cache-Control': 'no-store'})


@app.get('/api/live/{job_id}')
def live_get_route(job_id: str):
    return JSONResponse(live_get(job_id), headers={'Cache-Control': 'no-store'})


app.include_router(api)
