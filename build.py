"""hogam_site_bundle.json -> 로컬에서 그대로 열리는 정적 사이트로 변환."""
import hashlib, json, os, re, sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from lxml import html as LH

BUNDLE = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/Downloads/hogam_site_bundle.json')
OUT = os.path.dirname(os.path.abspath(__file__))
ORIGIN = 'https://hogam.imweb.me'
SITE_HOSTS = {'hogam.imweb.me', 'ho-gam.com', 'www.ho-gam.com'}
S = requests.Session()
S.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0 Safari/537.36'


def page_file(path):
    """사이트 경로 -> 로컬 파일명 ('/' -> index.html, '/60/?idx=32' -> 60_idx32.html)."""
    u = urlparse(path)
    base = u.path.strip('/').replace('/', '_') or 'index'
    q = parse_qs(u.query)
    if 'mode' in q and base == 'index':
        return f"mode_{q['mode'][0]}.html"
    if 'idx' in q:
        return f"{base}_idx{q['idx'][0]}.html"
    return f'{base}.html'


def page_key(path):
    """링크를 번들 키와 맞추기 위한 정규화 (게시판 q 파라미터 등 무시)."""
    u = urlparse(path)
    q = parse_qs(u.query)
    p = '/' + u.path.strip('/')
    if 'idx' in q:
        return (p, 'idx', q['idx'][0])
    if 'mode' in q:
        return (p, 'mode', q['mode'][0])
    return (p,)


# ---------- 에셋 다운로드 ----------
asset_map = {}  # 절대 URL -> 로컬 상대경로 (assets/...)


def local_asset_path(url):
    u = urlparse(url)
    name = os.path.basename(u.path) or 'file'
    name = re.sub(r'[^A-Za-z0-9._-]', '_', name)[-80:]
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    return f'assets/{u.netloc}/{h}_{name}'


def fetch(url):
    try:
        r = S.get(url, timeout=30)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


CSS_URL = re.compile(r'url\(\s*([\'"]?)([^\'")]+)\1\s*\)')
CSS_IMPORT = re.compile(r'@import\s+([\'"])([^\'"]+)\1')


def rewrite_css(text, css_url, css_local):
    """CSS 안의 url()/@import를 받아서 로컬 경로로 바꿈. css_local: 이 CSS 파일의 로컬 경로(상대경로 계산용)."""
    refs = set()
    for m in CSS_URL.finditer(text):
        refs.add(m.group(2))
    for m in CSS_IMPORT.finditer(text):
        refs.add(m.group(2))
    todo = {}
    for ref in refs:
        if ref.startswith('data:') or ref.startswith('#'):
            continue
        absu = urljoin(css_url, ref)
        if urlparse(absu).scheme not in ('http', 'https'):
            continue
        todo[ref] = absu
    download_many(list(todo.values()))
    css_dir = os.path.dirname(css_local)

    def rel(ref):
        absu = todo.get(ref)
        if absu and absu in asset_map:
            return os.path.relpath(asset_map[absu], css_dir).replace('\\', '/')
        return ref

    text = CSS_URL.sub(lambda m: f'url("{rel(m.group(2))}")', text)
    text = CSS_IMPORT.sub(lambda m: f'@import "{rel(m.group(2))}"', text)
    return text


def download_many(urls):
    urls = [u for u in dict.fromkeys(urls) if u not in asset_map]
    if not urls:
        return
    with ThreadPoolExecutor(16) as ex:
        results = list(ex.map(fetch, urls))
    css_later = []
    for url, data in zip(urls, results):
        if data is None:
            continue
        lp = local_asset_path(url)
        asset_map[url] = lp
        full = os.path.join(OUT, lp)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        path = urlparse(url).path.lower()
        if path.endswith('.css') or path.endswith('.cm'):
            css_later.append((url, lp, data))
        else:
            with open(full, 'wb') as f:
                f.write(data)
    for url, lp, data in css_later:
        text = rewrite_css(data.decode('utf-8', 'replace'), url, lp)
        with open(os.path.join(OUT, lp), 'w', encoding='utf-8') as f:
            f.write(text)


# ---------- 페이지 처리 ----------
EXTRA_JS = """
<script>
/* 복제본용 최소 동작: 드롭다운 메뉴, 모바일 메뉴 */
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.dropdown, [class*="_show_sub_menu"], li.dropdown-parent').forEach(function (li) {
    var sub = li.querySelector('.dropdown-menu, ul');
    if (!sub) return;
    li.addEventListener('mouseenter', function () { li.classList.add('open'); });
    li.addEventListener('mouseleave', function () { li.classList.remove('open'); });
  });
  document.querySelectorAll('[class*="mobile_slide_menu_open"], .mobile_menu_btn, ._mobile_menu_btn, [data-toggle="mobile-menu"]').forEach(function (b) {
    b.addEventListener('click', function (e) {
      e.preventDefault();
      document.body.classList.toggle('mobile_slide_menu_on');
      var m = document.querySelector('#mobile_slide_menu_wrap, ._mobile_slide_menu, .mobile_slide_menu');
      if (m) m.style.display = (m.style.display === 'block') ? '' : 'block';
    });
  });
});
</script>
<style>
.dropdown.open > .dropdown-menu, li.open > ul.dropdown-menu { display: block; }
/* 복제본: 좁은 화면에서도 PC 구성 그대로 표시 */
@media (max-width:991px){.section_wrap.pc_section.mobile_hide{display:block!important}.section_wrap.mobile_section{display:none!important}}
</style>
"""


def main():
    bundle = json.load(open(BUNDLE, encoding='utf-8'))
    pages = {k: v for k, v in bundle['pages'].items() if 'bmode=write' not in k}
    key_to_file = {page_key(k): page_file(k) for k in pages}

    # 사이트 자체 CSS (만료로 외부에서 못 받는 것) 저장
    site_css = {
        ORIGIN + '/css/custom.cm?1790130264': bundle['css']['custom'],
        ORIGIN + '/_/oms-customer-front-office/style.css': bundle['css'].get('oms', ''),
    }
    for url, text in site_css.items():
        lp = local_asset_path(url)
        if not lp.endswith('.css'):
            lp += '.css'
        asset_map[url] = lp
        os.makedirs(os.path.dirname(os.path.join(OUT, lp)), exist_ok=True)
        with open(os.path.join(OUT, lp), 'w', encoding='utf-8') as f:
            f.write(rewrite_css(text, url, lp))

    docs = {}
    for path, src in pages.items():
        doc = LH.document_fromstring(src)
        # 만료 안내 배너 · 관리자 전용 요소 제거
        for el in doc.xpath('//div[contains(@class,"preview_mode")]'):
            el.getparent().remove(el)
        for a in doc.xpath('//a[starts-with(@href,"/admin")]'):
            li = a.getparent()
            (li if li is not None and li.tag == 'li' else a).drop_tree()
        docs[path] = doc

    # 1) 에셋 URL 수집 후 일괄 다운로드
    urls = []
    for doc in docs.values():
        for el in doc.xpath('//link[@href]'):
            if (el.get('rel') or '').lower() in ('stylesheet', 'icon', 'shortcut icon', 'apple-touch-icon', 'preload'):
                urls.append(urljoin(ORIGIN + '/', el.get('href')))
        for el in doc.xpath('//img | //source | //video | //input[@type="image"]'):
            for attr in ('src', 'data-src', 'data-original', 'poster'):
                if el.get(attr) and not el.get(attr).startswith('data:'):
                    urls.append(urljoin(ORIGIN + '/', el.get(attr)))
            if el.get('srcset'):
                for part in el.get('srcset').split(','):
                    u = part.strip().split(' ')[0]
                    if u:
                        urls.append(urljoin(ORIGIN + '/', u))
        for el in doc.xpath('//*[@style]'):
            for m in CSS_URL.finditer(el.get('style')):
                if not m.group(2).startswith('data:'):
                    urls.append(urljoin(ORIGIN + '/', m.group(2)))
        for el in doc.xpath('//style'):
            for m in CSS_URL.finditer(el.text or ''):
                if not m.group(2).startswith('data:'):
                    urls.append(urljoin(ORIGIN + '/', m.group(2)))
    urls = [u for u in urls if urlparse(u).scheme in ('http', 'https') and u not in site_css]
    print(f'에셋 {len(set(urls))}개 다운로드 중...')
    download_many(urls)

    def loc(u):
        absu = urljoin(ORIGIN + '/', u)
        return asset_map.get(absu, u)

    # 2) 페이지별 URL 치환 후 저장
    for path, doc in docs.items():
        for el in doc.xpath('//link[@href]'):
            el.set('href', loc(el.get('href')))
        for el in doc.xpath('//img | //source | //video | //input[@type="image"]'):
            for attr in ('src', 'data-src', 'data-original', 'poster'):
                if el.get(attr) and not el.get(attr).startswith('data:'):
                    el.set(attr, loc(el.get(attr)))
            if el.get('srcset'):
                parts = []
                for part in el.get('srcset').split(','):
                    bits = part.strip().split(' ')
                    if bits[0]:
                        bits[0] = loc(bits[0])
                    parts.append(' '.join(bits))
                el.set('srcset', ', '.join(parts))
        for el in doc.xpath('//*[@style]'):
            el.set('style', CSS_URL.sub(lambda m: f'url("{loc(m.group(2))}")', el.get('style')))
        for el in doc.xpath('//style'):
            if el.text:
                el.text = CSS_URL.sub(lambda m: f'url("{loc(m.group(2))}")', el.text)
        # 내부 링크 -> 로컬 파일
        for a in doc.xpath('//a[@href]'):
            h = a.get('href')
            u = urlparse(urljoin(ORIGIN + '/', h))
            if u.scheme in ('http', 'https') and u.netloc in SITE_HOSTS:
                target = key_to_file.get(page_key(u.path + ('?' + u.query if u.query else '')))
                if target is None:
                    target = key_to_file.get(page_key(u.path))
                if target:
                    a.set('href', target + ('#' + u.fragment if u.fragment else ''))
                elif h.startswith('/'):
                    a.set('href', ORIGIN + h)  # 장바구니/로그인 등 서버 기능은 원래 주소로
        for f in doc.xpath('//form[@action]'):
            if f.get('action').startswith('/'):
                f.set('action', ORIGIN + f.get('action'))
        body = doc.find('body')
        if body is not None:
            for node in LH.fragments_fromstring(EXTRA_JS):
                body.append(node)
        out = '<!DOCTYPE html>\n' + LH.tostring(doc, encoding='unicode')
        with open(os.path.join(OUT, page_file(path)), 'w', encoding='utf-8') as f:
            f.write(out)
        print('저장', page_file(path))

    print(f'완료: 페이지 {len(docs)}개, 에셋 {len(asset_map)}개')


if __name__ == '__main__':
    main()
