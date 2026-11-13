import email.utils
import html
import json
import re
import subprocess
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path


NETWORK_NAMES = (
    (('fox news', 'fox'), 'FOX News'),
    (('cnn',), 'CNN'),
    (('bbc',), 'BBC News'),
    (('abc news',), 'ABC News'),
    (('nbc',), 'NBC News'),
    (('cbs',), 'CBS News'),
    (('msnbc',), 'MSNBC'),
    (('npr',), 'NPR'),
    (('reuters',), 'Reuters'),
    (('associated press', 'ap news'), 'Associated Press'),
    (('al jazeera',), 'Al Jazeera'),
    (('sky news',), 'Sky News'),
    (('france 24',), 'France 24'),
    (('dw news', 'deutsche welle'), 'DW News'),
    (('cbc',), 'CBC News'),
    (('ctv news',), 'CTV News'),
    (('univision',), 'Univision'),
)
PUBLISHER_FEEDS = {
    'FOX News': 'https://moxie.foxnews.com/google-publisher/latest.xml',
}


def display_network_name(requested, source=''):
    if str(requested).casefold() in ('my current location', 'current location'):
        return 'your current location'
    for candidate in (source, requested):
        value = str(candidate or '').strip()
        folded = value.casefold()
        for aliases, display in NETWORK_NAMES:
            if any(re.search(r'\b' + re.escape(alias) + r'\b', folded) for alias in aliases):
                return display
        if candidate is source and value:
            return value
    return str(requested).strip().title()


class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.paragraphs = []
        self.body_paragraphs = []
        self.paragraph = None
        self.body_elements = []
        self.skip_depth = 0
        self.article_depth = 0
        self.started_article = False
        self.jsonld_depth = 0
        self.jsonld = []
        self.jsonld_blocks = []
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        attrs = {str(key).lower(): str(value or '') for key, value in attrs}
        if tag == 'meta':
            key = (attrs.get('property') or attrs.get('name') or '').lower()
            value = attrs.get('content', '').strip()
            if key and value:
                self.meta.setdefault(key, value)
        if tag == 'script' and 'ld+json' in attrs.get('type', '').lower():
            self.jsonld_depth += 1
            self.jsonld = []
            return
        if tag in ('script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside', 'form'):
            self.skip_depth += 1
        if tag in ('article', 'main'):
            self.article_depth += 1
            self.started_article = True
        if tag in ('article', 'main', 'div', 'section'):
            labels = ' '.join((attrs.get('class', ''), attrs.get('id', '')))
            is_body = (
                attrs.get('itemprop', '').lower() == 'articlebody'
                or re.search(r'\b(?:article|story)[-_]+(?:body|content)\b', labels, re.I)
            )
            self.body_elements.append((tag, bool(is_body)))
        if tag == 'p':
            self.paragraph = []
        if tag == 'br' and self.paragraph is not None:
            self.paragraph.append(' ')
        if tag in ('br', 'div', 'section', 'h1', 'h2', 'h3'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag == 'script' and self.jsonld_depth:
            self.jsonld_depth -= 1
            if self.jsonld:
                self.jsonld_blocks.append(''.join(self.jsonld))
            self.jsonld = []
            return
        if tag in ('script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside', 'form'):
            self.skip_depth = max(0, self.skip_depth - 1)
        if tag in ('article', 'main'):
            self.article_depth = max(0, self.article_depth - 1)
        if tag == 'p' and self.paragraph is not None:
            value = re.sub(r'\s+', ' ', ''.join(self.paragraph)).strip()
            if value:
                self.paragraphs.append(value)
                if any(is_body for _, is_body in self.body_elements):
                    self.body_paragraphs.append(value)
            self.paragraph = None
        if tag in ('article', 'main', 'div', 'section'):
            for index in range(len(self.body_elements) - 1, -1, -1):
                if self.body_elements[index][0] == tag:
                    del self.body_elements[index:]
                    break
        if tag in ('div', 'section'):
            self.parts.append(' ')

    def handle_data(self, data):
        if self.jsonld_depth:
            self.jsonld.append(data)
            return
        if not self.skip_depth and (
            not self.started_article or self.article_depth
            or any(is_body for _, is_body in self.body_elements)
        ):
            text = re.sub(r'\s+', ' ', html.unescape(data)).strip()
            if text:
                self.parts.append(text)
            if self.paragraph is not None:
                self.paragraph.append(data)


def _jsonld_article_bodies(blocks):
    bodies = []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.casefold() == 'articlebody' and isinstance(child, str):
                    text = re.sub(r'\s+', ' ', html.unescape(child)).strip()
                    if text:
                        bodies.append(text)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for block in blocks:
        try:
            visit(json.loads(block))
        except (json.JSONDecodeError, TypeError):
            continue
    return bodies


def _plain_text(markup):
    parser = ArticleText()
    parser.feed(markup)
    return ' '.join(' '.join(parser.parts).split())


def _text_key(value):
    return ' '.join(re.findall(r'\w+', value.casefold()))


def _news_pause_tags(text):
    text = re.sub(r',\s+', lambda match: ',\\pause=250\\ ', text)
    return re.sub(r'\.\s+', lambda match: '.\\pause=500\\ ', text)


def _story_details(value, title, publisher):
    if title and value[:len(title)].casefold() == title.casefold():
        remainder = value[len(title):]
        if not remainder or not remainder[0].isalnum():
            value = remainder.lstrip(' :.-–—|')
    key = _text_key(value)
    title_key = _text_key(title)
    publisher_key = _text_key(publisher)
    headline_only = {title_key, publisher_key, f'{title_key} {publisher_key}'}
    if key in headline_only or len(key.split()) < 3:
        return ''
    return value


def _article_headline(item, dispatcher):
    publisher_suffix = r'\s+[-–—|]\s+' + re.escape(dispatcher) + r'$'
    return re.sub(publisher_suffix, '', _plain_text(item['title']), flags=re.I)


def _article_text(item, dispatcher, parser, title):
    body_candidates = _jsonld_article_bodies(parser.jsonld_blocks)
    body_candidates.append(parser.meta.get('article:body', ''))
    paragraphs = parser.body_paragraphs or parser.paragraphs
    body_candidates.append(' '.join(dict.fromkeys(paragraphs)))
    body_candidates.append(_plain_text(item['content']))
    feed_description = _plain_text(item['description'])
    if len(feed_description) >= 280 or feed_description.count('\n') >= 2:
        body_candidates.append(feed_description)
    body = next((details for value in body_candidates
                 if (details := _story_details(value, title, dispatcher))), '')
    descriptions = (item['description'], parser.meta.get('og:description', ''),
                    parser.meta.get('description', ''))
    description = next((details for value in descriptions
                        if (details := _story_details(_plain_text(value), title, dispatcher))), '')
    if description and _text_key(description) in _text_key(body):
        description = ''
    if not body:
        return None
    if title and title[-1] not in '.!?':
        title += '.'
    if description and description[-1] not in '.!?':
        description += '.'
    spoken_text = ' '.join(part for part in (title, description, body) if part)
    return {'title': title, 'text': _news_pause_tags(spoken_text)}


def _get(url, timeout=12, limit=12_000_000, publisher_page=False):
    request = urllib.request.Request(
        url, headers={'User-Agent': 'MarioAssistant/1.0', 'Accept': '*/*'}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if publisher_page and urllib.parse.urlsplit(response.geturl()).hostname in (
            'news.google.com', 'consent.google.com', 'accounts.google.com',
        ):
            raise ValueError('The news link did not reach the publisher')
        chunks = []
        length = 0
        while True:
            chunk = response.read(min(65536, limit + 1 - length))
            if not chunk:
                break
            length += len(chunk)
            if length > limit:
                raise ValueError('News response exceeded the size limit')
            chunks.append(chunk)
        return b''.join(chunks)


def _feed_items(url):
    root = ET.fromstring(_get(url))
    items = []
    for item in root.iter():
        if item.tag.rsplit('}', 1)[-1].lower() not in ('item', 'entry'):
            continue
        fields = {}
        for child in item.iter():
            key = child.tag.rsplit('}', 1)[-1].lower()
            value = ' '.join(''.join(child.itertext()).split())
            if value:
                fields.setdefault(key, []).append(value)
        first = lambda *names: next(
            (values[0] for name in names if (values := fields.get(name))), ''
        )
        link = first('link')
        if not link:
            link_node = next((node for node in item if node.tag.rsplit('}', 1)[-1].lower() == 'link'), None)
            if link_node is not None:
                link = link_node.attrib.get('href', '')
        enclosure = next((node.attrib.get('url') for node in item.iter()
                          if node.tag.rsplit('}', 1)[-1].lower() == 'enclosure'
                          and node.attrib.get('url')), '')
        published = first('pubdate', 'published', 'updated', 'date')
        try:
            timestamp = email.utils.parsedate_to_datetime(published).timestamp()
        except (TypeError, ValueError, OverflowError):
            timestamp = 0
        items.append({
            'title': first('title', 'headline'),
            'description': ' '.join(dict.fromkeys(
                value for name in ('description', 'summary', 'abstract', 'subtitle', 'media:description')
                for value in fields.get(name, [])
            )),
            'content': ' '.join(dict.fromkeys(
                value for name in ('encoded', 'content', 'fulltext', 'articlebody', 'body', 'text')
                for value in fields.get(name, [])
            )),
            'link': link,
            'audio': enclosure,
            'source': first('source', 'creator', 'author'),
            'timestamp': timestamp,
        })
    return items


def _location(city='', country=''):
    city = city.strip()
    country = country.strip().upper()
    if country and not re.fullmatch(r'[A-Z]{2}', country):
        raise ValueError('The saved country must be a two-letter country code')
    if city and country:
        return city, country
    data = json.loads(_get('https://ipapi.co/json/', limit=512_000))
    detected_country = str(data.get('country_code') or '').strip().upper()
    if data.get('error') or not re.fullmatch(r'[A-Z]{2}', detected_country):
        raise LookupError('Could not determine the country for local news')
    if country and country != detected_country:
        raise LookupError('Set a city for the saved country to look up local news')
    city = city or str(data.get('city') or '').strip()
    if not city:
        raise LookupError('Could not determine the city for local news')
    return city, detected_country


def _place_key(text):
    decomposed = unicodedata.normalize('NFKD', text)
    return _text_key(''.join(char for char in decomposed if not unicodedata.combining(char)))


def _mentions_city(city, *values):
    city_key = _place_key(city)
    return bool(city_key) and any(
        f' {city_key} ' in f' {_place_key(value)} ' for value in values
    )


def _news_language(country):
    if country in {'AR', 'BO', 'CL', 'CO', 'CR', 'DO', 'EC', 'GT', 'HN',
                   'MX', 'NI', 'PA', 'PE', 'PY', 'SV', 'UY', 'VE'}:
        return 'es-419', 'noticias'
    if country == 'ES':
        return 'es', 'noticias'
    return 'en', 'news'


def _latest_article(network, is_location, country='', cancelled=None):
    region = country.upper() if country else 'US'
    language, news_term = _news_language(region) if is_location else ('en', 'news')
    query = f'"{network}" {news_term}' if is_location else f'{network} news'
    params = {'q': query, 'hl': language, 'gl': region, 'ceid': f'{region}:{language}'}
    feed_url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode(params)
    publisher_feed = None if is_location else PUBLISHER_FEEDS.get(display_network_name(network))
    items = []
    if publisher_feed:
        try:
            items = _feed_items(publisher_feed)
        except (OSError, ValueError, ET.ParseError):
            pass
    if not items:
        items = _feed_items(feed_url)
        if not is_location:
            items = [item for item in items if network.casefold() in item['source'].casefold()]
    skipped = []
    latest_headline = None
    for item in sorted(items, key=lambda value: value['timestamp'], reverse=True):
        if cancelled is not None and cancelled():
            return None
        dispatcher = display_network_name(network, item['source'])
        title = _article_headline(item, dispatcher)
        if not title:
            continue
        parser = ArticleText()
        is_video = urllib.parse.urlsplit(item['link']).path.startswith('/video/')
        if item['link'] and not is_video:
            try:
                page = _get(item['link'], timeout=8, limit=8_000_000,
                            publisher_page=True).decode('utf-8', errors='replace')
                parser.feed(page)
            except (OSError, ValueError):
                pass
        story = None if is_video else _article_text(item, dispatcher, parser, title)
        if is_location and not _mentions_city(
            network, title, item['source'], _plain_text(item['description']),
            story['text'] if story else '',
        ):
            continue
        if latest_headline is None:
            latest_headline = {'kind': 'article', 'network': dispatcher,
                               'timestamp': item['timestamp'], 'title': title,
                               'headlines_only': True}
        if story:
            story['text'] = ' '.join([
                _news_pause_tags(' '.join(skipped)), story['text'],
            ]).strip()
            return {'kind': 'article', 'network': dispatcher,
                    'timestamp': item['timestamp'],
                    'language': 'es' if is_location and country in {
                        'AR', 'BO', 'CL', 'CO', 'CR', 'DO', 'EC', 'GT', 'HN',
                        'MX', 'NI', 'PA', 'PE', 'PY', 'SV', 'UY', 'VE', 'ES',
                    } else 'en', **story}
        separator = ' ' if title[-1] in '.!?' else '. '
        skipped.append(f'{title}{separator}From {dispatcher}.')
    if latest_headline is not None:
        latest_headline['text'] = ' '.join([
            *skipped, "I couldn't find an article with a readable body.",
        ])
        latest_headline['text'] = _news_pause_tags(latest_headline['text'])
    return latest_headline


def _latest_broadcast(network, local_country=''):
    params = {'term': f'{network} news', 'entity': 'podcast', 'limit': 15}
    if local_country:
        _, news_term = _news_language(local_country)
        params.update(term=f'{network} {news_term}', country=local_country, media='podcast')
    query = urllib.parse.urlencode(params)
    results = json.loads(_get('https://itunes.apple.com/search?' + query).decode('utf-8'))
    feeds = []
    for result in results.get('results', []):
        feed_url = result.get('feedUrl')
        name = result.get('collectionName') or result.get('trackName') or ''
        publisher = result.get('artistName') or ''
        if local_country:
            genres = [result.get('primaryGenreName') or '', *(result.get('genres') or [])]
            if not any(re.search(r'\b(?:news|noticias|actualidad)\b', genre, re.I)
                       for genre in [name, *genres]):
                continue
        if feed_url:
            feeds.append((name, publisher, feed_url))
    candidates = []
    for show_name, publisher, feed_url in feeds[:8]:
        try:
            for item in _feed_items(feed_url):
                if item['audio'] and (
                    not local_country or _mentions_city(network, show_name, publisher, item['title'])
                ):
                    candidates.append((show_name, item))
        except Exception:
            continue
    if not candidates:
        return None
    show_name, item = max(candidates, key=lambda pair: pair[1]['timestamp'])
    dispatcher = display_network_name(network, show_name)
    return {'kind': 'broadcast', 'network': dispatcher, 'title': item['title'],
            'timestamp': item['timestamp'], 'audio_url': item['audio'],
            'show': show_name}


def latest_news(network, mode, ffmpeg, temporary_folder, cancelled=None, *, city='', country=''):
    is_location = network.casefold() in ('my current location', 'current location')
    if is_location:
        network, country = _location(city, country)
    else:
        network = network.strip()
        if not network:
            raise ValueError('A news network is required')
        country = ''
    candidates = []
    failures = []
    if mode in ('auto', 'text'):
        try:
            article = _latest_article(network, is_location, country, cancelled)
            if article is not None:
                candidates.append(article)
        except Exception as exc:
            failures.append(exc)
    if cancelled is not None and cancelled():
        return None
    if mode in ('auto', 'audio'):
        try:
            broadcast = _latest_broadcast(network, country if is_location else '')
            if broadcast is not None:
                candidates.append(broadcast)
        except Exception as exc:
            failures.append(exc)
    if not candidates:
        if failures and mode != 'auto':
            raise failures[0]
        requested = 'article' if mode == 'text' else 'broadcast' if mode == 'audio' else 'article or broadcast'
        detail = f': {failures[0]}' if failures else ''
        raise LookupError(f'No latest {requested} was found from {network}{detail}')
    selected = max(candidates, key=lambda item: (
        not item.get('headlines_only', False), item['timestamp'],
    ))
    if selected['kind'] == 'broadcast':
        folder = Path(temporary_folder)
        folder.mkdir(parents=True, exist_ok=True)
        downloaded = folder / 'latest-news-broadcast.bin'
        decoded = folder / 'latest-news-broadcast.wav'
        request = urllib.request.Request(
            selected['audio_url'], headers={'User-Agent': 'MarioAssistant/1.0', 'Accept': '*/*'}
        )
        with urllib.request.urlopen(request, timeout=20) as response, downloaded.open('wb') as output:
            total = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > 120_000_000:
                    raise ValueError('News broadcast exceeded the download size limit')
                output.write(chunk)
        subprocess.run(
            [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(downloaded),
             '-vn', '-ac', '2', '-ar', '44100', '-c:a', 'pcm_s16le', str(decoded)],
            check=True, capture_output=True, timeout=240,
        )
        downloaded.unlink(missing_ok=True)
        selected['audio_path'] = str(decoded)
    if country:
        selected['country'] = country
    return selected
