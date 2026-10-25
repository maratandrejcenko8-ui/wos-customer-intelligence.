from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from collections import Counter
import csv, io, json, os, re, statistics, urllib.request, urllib.parse
from html.parser import HTMLParser

BASE = Path(__file__).parent
app = FastAPI(title='WOS Customer Intelligence')
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')

SAMPLE = [
 {"source":"Wildberries (пример)","product":"Платье миди базовое","rating":5,"date":"2026-09-12","text":"Ткань приятная, цвет красивый, хорошо сидит по фигуре.","url":"https://www.wildberries.ru/"},
 {"source":"Wildberries (пример)","product":"Платье миди базовое","rating":3,"date":"2026-09-14","text":"На мой рост длинновато, размер соответствует, но ткань тоньше чем ожидала.","url":"https://www.wildberries.ru/"},
 {"source":"Ozon (пример)","product":"Футболка oversize","rating":4,"date":"2026-09-15","text":"Хороший хлопок, после стирки форму сохранила. Цвет немного отличается от фото.","url":"https://www.ozon.ru/"},
 {"source":"Ozon (пример)","product":"Футболка oversize","rating":2,"date":"2026-09-17","text":"Размер очень большой, швы неровные, пришлось вернуть.","url":"https://www.ozon.ru/"},
 {"source":"Яндекс Маркет (пример)","product":"Джинсы straight","rating":5,"date":"2026-09-19","text":"Посадка удобная, джинсы качественные, цвет как на фото.","url":"https://market.yandex.ru/"},
 {"source":"Яндекс Маркет (пример)","product":"Джинсы straight","rating":3,"date":"2026-09-20","text":"Талия узкая, длина подошла. Хотелось бы больше информации по длине изделия.","url":"https://market.yandex.ru/"},
 {"source":"Демо-данные","product":"Кардиган базовый","rating":4,"date":"2026-09-22","text":"Мягкий и приятный материал, но катышки появились после нескольких носок.","url":""},
 {"source":"Демо-данные","product":"Кардиган базовый","rating":1,"date":"2026-09-24","text":"Цвет хороший, но швы разошлись и размер оказался меньше заявленного.","url":""},
 {"source":"Демо-данные","product":"Юбка плиссе","rating":5,"date":"2026-09-25","text":"Отлично выглядит, аккуратно сшита, упаковка хорошая.","url":""},
 {"source":"Демо-данные","product":"Юбка плиссе","rating":2,"date":"2026-09-26","text":"Ткань электризуется, длина не совпала с описанием.","url":""},
]

THEMES = {
 'Размер и посадка':['размер','маломер','большемер','посадк','талия','рост','длин','коротк','широк','узк','свободн'],
 'Материал и ткань':['ткан','материал','хлопок','синтет','мягк','тонк','плотн','электриз','катыш'],
 'Качество пошива':['шов','шв','строч','нитк','разош','пошив','дыр','качество'],
 'Цвет и фото':['цвет','фото','оттенок','выглядит','картинк'],
 'Описание товара':['описан','информац','характерист','таблиц','длина изделия'],
 'Доставка и упаковка':['достав','упаков','короб','пакет'],
 'Комфорт и внешний вид':['удоб','красив','приятн','выгляд','стиль','сидит','аккурат'],
}
POS = ['приятн','хорош','качеств','красив','удоб','отличн','мягк','подош','сохрани','аккурат','рекоменд','люблю']
NEG = ['плох','разош','дыр','вернул','не подош','тоньше','отличается','катыш','электриз','узк','маломер','не совпал','ожидал','неудоб','брак']

def norm(row):
    low = {str(k).strip().lower(): v for k,v in row.items() if k is not None}
    def pick(*keys):
        for k in keys:
            if k in low and low[k] not in (None, ''): return low[k]
        return ''
    text = str(pick('text','review','review_text','отзыв','текст','comment','комментарий','body')).strip()
    if not text: return None
    rating_raw = pick('rating','score','оценка','звезды','звёзды')
    try: rating = float(str(rating_raw).replace(',','.')) if rating_raw else None
    except: rating = None
    date = str(pick('date','created_at','дата','published_at'))
    product = str(pick('product','product_name','товар','название','offer_name','sku')) or 'Не указан'
    source = str(pick('source','marketplace','источник','площадка')) or 'Импортированный файл'
    url = str(pick('url','link','ссылка','review_url'))
    return {'source':source,'product':product,'rating':rating,'date':date,'text':text,'url':url}

def classify(text):
    t = text.lower()
    found = [theme for theme, words in THEMES.items() if any(w in t for w in words)]
    if not found: found = ['Общее впечатление']
    pos = sum(t.count(w) for w in POS); neg = sum(t.count(w) for w in NEG)
    sentiment = 'Позитивный' if pos > neg else ('Негативный' if neg > pos else 'Смешанный / нейтральный')
    return found, sentiment

def report(rows, use_ai=False):
    rows = [norm(r) for r in rows]
    rows = [r for r in rows if r]
    if not rows: raise HTTPException(400, 'Не найдено отзывов. Проверьте колонку с текстом отзыва.')
    themes = Counter(); sentiment = Counter(); product_themes = {}; ratings=[]; examples={}
    for r in rows:
        cats, sent = classify(r['text']); sentiment[sent]+=1
        if r['rating'] is not None and 1 <= r['rating'] <= 5: ratings.append(r['rating'])
        for c in cats:
            themes[c]+=1; examples.setdefault(c,[])
            if len(examples[c])<3: examples[c].append({'product':r['product'],'text':r['text'],'source':r['source'],'rating':r['rating'],'url':r['url']})
            product_themes.setdefault(r['product'],Counter())[c]+=1
    total=len(rows)
    theme_list=[{'name':k,'count':v,'percent':round(v/total*100)} for k,v in themes.most_common()]
    positive=sentiment['Позитивный']; negative=sentiment['Негативный']; mixed=sentiment['Смешанный / нейтральный']
    products=[]
    for name, counter in product_themes.items():
        products.append({'name':name,'mentions':sum(counter.values()),'top_issue':next((x for x in counter.most_common() if x[0] in ['Размер и посадка','Материал и ткань','Качество пошива','Цвет и фото','Описание товара']),('Нет явной темы',0))[0]})
    products.sort(key=lambda x:x['mentions'], reverse=True)
    recs=[]
    for t in theme_list:
        if t['name']=='Размер и посадка': recs.append({'priority':'Высокий','title':'Уточнить размерную сетку и посадку','detail':f"Тема встречается в {t['count']} из {total} отзывов ({t['percent']}% от выборки). Проверить мерки изделия, посадку по росту и указать параметры модели."})
        elif t['name']=='Материал и ткань': recs.append({'priority':'Высокий','title':'Проверить описание состава и свойства ткани','detail':f"Тема встречается в {t['count']} из {total} отзывов ({t['percent']}%). Добавить точный состав, плотность и рекомендации по уходу, если подтверждено характеристиками изделия."})
        elif t['name']=='Качество пошива': recs.append({'priority':'Высокий','title':'Проверить качество пошива по проблемным моделям','detail':f"Тема встречается в {t['count']} из {total} отзывов ({t['percent']}%). Сгруппировать претензии по SKU и проверить повторяемость дефектов."})
        elif t['name']=='Цвет и фото': recs.append({'priority':'Средний','title':'Сверить фото с реальным оттенком','detail':f"Тема встречается в {t['count']} из {total} отзывов ({t['percent']}%). Проверить цветопередачу и добавить фото при нейтральном освещении."})
        elif t['name']=='Описание товара': recs.append({'priority':'Средний','title':'Закрыть пробелы в карточке товара','detail':f"Тема встречается в {t['count']} из {total} отзывов ({t['percent']}%). Добавить недостающие измерения и таблицу параметров."})
    if not recs: recs=[{'priority':'Проверка','title':'Сначала увеличить выборку','detail':'В текущих данных мало повторяющихся тематических сигналов. Импортируйте больше отзывов перед выводами.'}]
    result={'brand':'Befree (тестовый бренд)','total':total,'avg_rating':round(statistics.mean(ratings),2) if ratings else None,'rated_count':len(ratings),'sentiment':{'positive':positive,'positive_pct':round(positive/total*100),'negative':negative,'negative_pct':round(negative/total*100),'mixed':mixed,'mixed_pct':round(mixed/total*100)},'themes':theme_list,'products':products[:12],'recommendations':recs,'examples':examples,'sources':dict(Counter(r['source'] for r in rows)),'rows':rows,'ai_mode':'rules'}
    api_key=os.getenv('OPENAI_API_KEY')
    if use_ai and api_key:
        try:
            prompt = {'task':'Проанализируй отзывы покупателей fashion-бренда. Верни краткие русскоязычные выводы, только по данным; не выдумывай факты. Проценты должны опираться на предоставленную выборку. Формат JSON: summary (строка), strengths (массив строк), risks (массив строк), actions (массив объектов priority,title,detail).','sample': [{'product':r['product'],'rating':r['rating'],'text':r['text'],'source':r['source']} for r in rows[:120]], 'calculated_metrics':{'total':total,'themes':theme_list,'sentiment':result['sentiment']}}
            body=json.dumps({'model':os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'input':json.dumps(prompt,ensure_ascii=False),'text':{'format':{'type':'json_object'}}}).encode()
            req=urllib.request.Request('https://api.openai.com/v1/responses',data=body,headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(req,timeout=60) as response: ai=json.loads(response.read().decode())
            text_out=''.join(x.get('text','') for x in ai.get('output',[]) if x.get('type')=='message' for x in x.get('content',[]) if x.get('type')=='output_text')
            if text_out:
                ai_json=json.loads(text_out); result['ai']=ai_json; result['ai_mode']='openai'
        except Exception as e:
            result['ai_error']='AI не ответил; показан локальный анализ. Проверьте ключ, модель и подключение.'
    elif use_ai and not api_key: result['ai_error']='OPENAI_API_KEY не настроен; показан локальный анализ по ключевым словам.'
    return result



class SearchResultParser(HTMLParser):
    """Tolerant parser for ordinary search-result HTML. Does not bypass access controls."""
    def __init__(self, engine='ddg'):
        super().__init__(convert_charrefs=True)
        self.engine=engine
        self.results=[]
        self.current=None
        self.capture=None
        self.buf=[]
        self.depth=0

    def handle_starttag(self, tag, attrs):
        a=dict(attrs); classes=a.get('class','').lower(); href=a.get('href','')
        if self.engine=='ddg':
            if tag=='a' and ('result__a' in classes or ('result-link' in classes and href)):
                self._finish()
                self.current={'title':'','url':href,'snippet':''}
                self.capture='title'; self.buf=[]
            elif self.current and tag in ('a','div','span') and ('result__snippet' in classes or 'result-snippet' in classes):
                self.capture='snippet'; self.buf=[]
        else:
            if tag=='li' and ('b_algo' in classes):
                self._finish()
                self.current={'title':'','url':'','snippet':''}; self.depth=1
            elif self.current and tag=='a' and href and not self.current['url']:
                self.current['url']=href; self.capture='title'; self.buf=[]
            elif self.current and tag in ('p','div') and ('b_caption' in classes or tag=='p'):
                if self.current.get('url') and not self.current.get('snippet'):
                    self.capture='snippet'; self.buf=[]

    def handle_data(self, data):
        if self.current is not None and self.capture:
            self.buf.append(data)

    def handle_endtag(self, tag):
        if self.current is not None and self.capture:
            if self.capture=='title' and tag=='a':
                self.current['title']=' '.join(' '.join(self.buf).split())
                self.capture=None; self.buf=[]
            elif self.capture=='snippet' and tag in ('div','span','p'):
                value=' '.join(' '.join(self.buf).split())
                if len(value)>len(self.current.get('snippet','')):
                    self.current['snippet']=value
                self.capture=None; self.buf=[]
        if self.engine=='bing' and tag=='li' and self.current is not None:
            self._finish()

    def _finish(self):
        if self.current and self.current.get('url') and self.current.get('title'):
            self.results.append(self.current)
        self.current=None; self.capture=None; self.buf=[]

    def close(self):
        super().close(); self._finish()


def _fetch_search(url, engine, timeout=14):
    req=urllib.request.Request(url, headers={
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123 Safari/537.36',
        'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language':'ru-RU,ru;q=0.9,en;q=0.7',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status=getattr(resp,'status',200)
        html=resp.read(2_500_000).decode('utf-8','replace')
    parser=SearchResultParser(engine); parser.feed(html); parser.close()
    return parser.results, status, len(html)


def public_search(query, max_results=8):
    """Try two ordinary public search pages; returns diagnostics and snippets, not full reviews."""
    diagnostics=[]; collected=[]
    providers=[
      ('DuckDuckGo','ddg','https://html.duckduckgo.com/html/?q='+urllib.parse.quote(query)),
      ('Bing','bing','https://www.bing.com/search?q='+urllib.parse.quote(query)+'&count=10'),
    ]
    for name,engine,url in providers:
        try:
            rows,status,size=_fetch_search(url,engine)
            valid=[]
            for row in rows:
                link=row.get('url','').strip()
                title=row.get('title','').strip()
                snippet=row.get('snippet','').strip()
                if link.startswith('//'): link='https:'+link
                if not link.startswith('http'): continue
                # Skip search engine navigation, ad and redirect URLs.
                host=urllib.parse.urlparse(link).netloc.lower()
                if any(x in host for x in ('duckduckgo.com','bing.com')): continue
                if not title: continue
                valid.append({'title':title,'url':link,'snippet':snippet})
            diagnostics.append({'provider':name,'status':'ok','http_status':status,'html_chars':size,'result_links':len(rows),'usable_links':len(valid)})
            collected.extend(valid)
            if len(collected)>=max_results: break
        except Exception as e:
            diagnostics.append({'provider':name,'status':'error','error':type(e).__name__+': '+str(e)[:180]})
    # De-duplicate links
    unique=[]; seen=set()
    for row in collected:
        key=row['url'].split('#')[0]
        if key not in seen:
            seen.add(key); unique.append(row)
    return unique[:max_results], diagnostics


DEMO_REVIEW_TEXTS = [
    ("Wildberries · ДЕМО", "Платье миди базовое", 5, "Материал приятный на ощупь, цвет спокойный, в целом выглядит аккуратно.", "Положительное впечатление"),
    ("Wildberries · ДЕМО", "Платье миди базовое", 4, "Фасон понравился, но хотелось бы точнее видеть длину изделия на модели.", "Описание и длина"),
    ("Ozon · ДЕМО", "Футболка oversize", 4, "Ткань мягкая, посадка свободная. Для выбора размера пригодилась бы таблица замеров.", "Размер и посадка"),
    ("Яндекс Маркет · ДЕМО", "Футболка oversize", 3, "Выглядит нормально, но оттенок на экране кажется немного другим.", "Цвет и фото"),
    ("Wildberries · ДЕМО", "Кардиган базовый", 5, "Кардиган мягкий, вязка смотрится ровно, на каждый день подходит.", "Материал и внешний вид"),
    ("Ozon · ДЕМО", "Кардиган базовый", 2, "После нескольких носок появились катышки. Хотелось бы более подробное описание ухода.", "Материал и уход"),
    ("Яндекс Маркет · ДЕМО", "Брюки прямого кроя", 4, "Сидят удобно, но длина для моего роста оказалась чуть больше ожидаемой.", "Размер и длина"),
    ("Wildberries · ДЕМО", "Брюки прямого кроя", 3, "Ткань приятная, однако пояс показался теснее, чем ожидалось по таблице.", "Посадка и размер"),
    ("Ozon · ДЕМО", "Джинсы straight", 5, "Удобная посадка, швы выглядят аккуратно, цвет универсальный.", "Качество и посадка"),
    ("Яндекс Маркет · ДЕМО", "Джинсы straight", 3, "По длине подошли, но замеры талии в карточке хотелось бы видеть отдельно.", "Описание товара"),
    ("Wildberries · ДЕМО", "Рубашка oversize", 4, "Выглядит как на фото, свободный крой понравился. Материал немного тоньше, чем представляла.", "Материал и фото"),
    ("Ozon · ДЕМО", "Рубашка oversize", 2, "После стирки форма стала немного другой. Не хватило понятной инструкции по уходу.", "Уход за изделием"),
    ("Яндекс Маркет · ДЕМО", "Юбка плиссе", 5, "Аккуратно сшита, хорошо смотрится с базовыми вещами.", "Качество пошива"),
    ("Wildberries · ДЕМО", "Юбка плиссе", 3, "Длина отличается от того, как я представляла по фото. Нужен снимок на модели другого роста.", "Фото и длина"),
    ("Ozon · ДЕМО", "Лонгслив базовый", 4, "Удобная вещь, ткань приятная. Было бы полезно указать плотность материала.", "Описание материала"),
    ("Яндекс Маркет · ДЕМО", "Лонгслив базовый", 2, "Размер оказался меньше ожидаемого. Таблица размеров не помогла понять посадку.", "Размер и посадка"),
    ("Wildberries · ДЕМО", "Жакет свободного кроя", 5, "Смотрится хорошо, плечи сидят аккуратно, фурнитура выглядит качественно.", "Посадка и качество"),
    ("Ozon · ДЕМО", "Жакет свободного кроя", 3, "В целом неплохо, но хотелось бы больше фото подкладки и деталей внутри.", "Фото и детали"),
    ("Яндекс Маркет · ДЕМО", "Топ базовый", 4, "Цвет универсальный, удобно сочетать. Размер соответствует моим ожиданиям.", "Цвет и размер"),
    ("Wildberries · ДЕМО", "Топ базовый", 2, "Ткань тоньше, чем ожидалось по изображениям. Лучше добавить крупный план материала.", "Материал и фото"),
]

def make_demo_reviews(brand: str):
    """Synthetic sample for UI demonstration only; never represent as scraped customer reviews."""
    rows = []
    for i, (source, product, rating, text, _note) in enumerate(DEMO_REVIEW_TEXTS, start=1):
        rows.append({
            "source": source,
            "product": product,
            "rating": rating,
            "date": "Демо-данные",
            "text": text,
            "url": "",
            "record_type": "synthetic_demo",
        })
    return rows


@app.post('/api/discover')
def discover(payload: dict):
    brand = str(payload.get('brand', '')).strip()
    if not brand or len(brand) > 100:
        raise HTTPException(400, 'Укажите название бренда (до 100 символов).')

    sources = payload.get('sources') or ['Wildberries', 'Ozon', 'Яндекс Маркет', 'Другие открытые страницы']
    queries = {
        'Wildberries': f'site:wildberries.ru/catalog {brand} отзывы покупателей',
        'Ozon': f'site:ozon.ru {brand} отзывы покупателей одежда',
        'Яндекс Маркет': f'site:market.yandex.ru {brand} отзывы покупателей',
        'Другие открытые страницы': f'"{brand}" отзывы покупателей одежда'
    }
    found, failures, diagnostics = [], [], []
    for source in sources:
        q = queries.get(source)
        if not q:
            continue
        try:
            items, provider_diag = public_search(q, 8)
            diagnostics.extend([dict(d, source=source) for d in provider_diag])
            for item in items:
                snippet = item.get('snippet', '').strip()
                title = item.get('title', '').strip()
                link = item.get('url', '').strip()
                if len(snippet) < 45 or not link.startswith('http'):
                    continue
                low = (title + ' ' + snippet).lower()
                if not any(w in low for w in ('отзыв', 'покупател', 'размер', 'ткан', 'качество', 'посадк', 'цвет', 'шов', 'достав', 'review', 'customer')):
                    continue
                found.append({'source': source, 'product': title or brand, 'rating': None, 'date': '', 'text': snippet, 'url': link, 'record_type': 'search_snippet'})
        except Exception as e:
            failures.append({'source': source, 'error': type(e).__name__ + ': ' + str(e)[:180]})

    unique, seen = [], set()
    for row in found:
        key = re.sub(r'\W+', ' ', row['text'].lower()).strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(row)

    # This MVP is being prepared for a product demo. If search results are insufficient,
    # return 20 explicitly labeled synthetic examples so the UI can be demonstrated.
    # Do not present these examples as real marketplace reviews.
    if len(unique) < 20:
        demo_rows = make_demo_reviews(brand)
        return {
            'brand': brand,
            'reviews': demo_rows,
            'count': len(demo_rows),
            'demo_mode': True,
            'real_fragments_found': len(unique),
            'failures': failures,
            'diagnostics': diagnostics,
            'notice': f'ДЕМО-РЕЖИМ: показаны 20 синтетических примеров отзывов для демонстрации интерфейса. Это не реальные отзывы покупателей и не результат выгрузки с маркетплейсов. Автоматический поиск нашёл пригодных фрагментов: {len(unique)}. Для реального клиентского отчёта нужны проверяемые источники.',
            'queries': {src: queries[src] for src in sources if src in queries}
        }

    return {
        'brand': brand, 'reviews': unique[:100], 'count': min(len(unique), 100),
        'demo_mode': False, 'real_fragments_found': len(unique), 'failures': failures,
        'diagnostics': diagnostics,
        'notice': 'Результаты поиска — открытые поисковые фрагменты, а не полная выгрузка отзывов. Проверяйте исходные страницы; фрагменты могут не быть полноценными отзывами.',
        'queries': {src: queries[src] for src in sources if src in queries}
    }

@app.get('/', response_class=HTMLResponse)
def index(): return (BASE/'static'/'index.html').read_text(encoding='utf-8')
@app.get('/api/sample')
def sample(): return report(SAMPLE)
@app.post('/api/analyze')
def analyze(payload: dict):
    rows=payload.get('reviews',[])
    if not isinstance(rows,list): raise HTTPException(400,'Ожидался массив отзывов.')
    return report(rows, bool(payload.get('use_ai',True)))
@app.post('/api/import')
async def import_file(file: UploadFile = File(...)):
    data=await file.read()
    if len(data)>12*1024*1024: raise HTTPException(413,'Файл больше 12 МБ.')
    name=(file.filename or '').lower()
    if name.endswith('.csv'):
        decoded=None
        for enc in ('utf-8-sig','utf-8','cp1251'):
            try: decoded=data.decode(enc); break
            except UnicodeDecodeError: pass
        if decoded is None: raise HTTPException(400,'Не удалось прочитать CSV. Сохраните файл в UTF-8.')
        try:
            dialect=csv.Sniffer().sniff(decoded[:4096], delimiters=',;\t')
        except: dialect=csv.excel
        rows=list(csv.DictReader(io.StringIO(decoded), dialect=dialect))
    elif name.endswith('.json'):
        try: rows=json.loads(data.decode('utf-8-sig')); rows=rows if isinstance(rows,list) else rows.get('reviews',[])
        except: raise HTTPException(400,'Некорректный JSON.')
    elif name.endswith(('.xlsx','.xls')):
        raise HTTPException(400,'XLSX пока не включён. Сохраните таблицу как CSV UTF-8 и загрузите снова.')
    else: raise HTTPException(400,'Поддерживаются CSV и JSON.')
    return report(rows, use_ai=True)
@app.get('/api/status')
def status():
    return {'analysis':'local rules + optional OpenAI','openai_configured':bool(os.getenv('OPENAI_API_KEY')),'connectors':{'wildberries':{'status':'requires seller API token','configured':bool(os.getenv('WB_API_TOKEN'))},'ozon':{'status':'requires seller API credentials and account access','configured':bool(os.getenv('OZON_CLIENT_ID') and os.getenv('OZON_API_KEY'))},'yandex_market':{'status':'requires partner API key and business ID','configured':bool(os.getenv('YANDEX_MARKET_API_KEY') and os.getenv('YANDEX_BUSINESS_ID'))}}}
