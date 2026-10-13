import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright


ROOT = Path('/Users/vegetarianwolf/Projects/JD_WuLiuBei')
RESULTS = ROOT / 'algorithm/results/regret_followup_20261013'
OUT = RESULTS / 'acceptance_visual'
OUT.mkdir(exist_ok=True)
OLD = ROOT / 'algorithm/results/optimization_20261013'
result = {'schema_version': 1, 'browser': 'Playwright Chromium, isolated temporary profile',
          'offline': True, 'pages': [], 'screenshots': [], 'checks': {}}


def local_links(page):
    links = page.locator('a[href]').evaluate_all('(nodes)=>nodes.map(n=>({label:n.textContent,url:n.href}))')
    checked = []
    for item in links:
        parsed = urlparse(item['url'])
        if parsed.scheme == 'file':
            path = Path(unquote(parsed.path))
            assert path.is_file(), item
            checked.append({'label': item['label'], 'path': str(path.relative_to(ROOT))})
        else:
            assert parsed.scheme not in ('http', 'https'), item
    return checked


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    for label, width, height, scheme in (
        ('desktop-light', 1280, 900, 'light'), ('mobile-light', 360, 800, 'light'),
        ('mobile-dark', 360, 800, 'dark'), ('desktop-dark', 1280, 900, 'dark'),
    ):
        context = browser.new_context(viewport={'width': width, 'height': height}, color_scheme=scheme)
        context.set_offline(True)
        page = context.new_page()
        errors, console_errors, web_requests = [], [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
        page.on('request', lambda request: web_requests.append(request.url) if request.url.startswith(('http:', 'https:')) else None)
        page.goto((RESULTS / 'index.html').as_uri(), wait_until='load')
        page.locator('#followup-status').wait_for()
        page.wait_for_function('document.querySelector("#followup-status").textContent.length>0')
        data = page.locator('#optimization-data').evaluate('(n)=>JSON.parse(n.textContent)')
        headline = page.locator('#outcome-note').inner_text()
        assert '主批 A' in headline and '独立追加 B' in headline and '词典序退化' in headline, headline
        assert data['followup']['status'] == data['followup']['supplemental']['status'] == 'not_promoted'
        assert data['followup']['supplemental']['confirmation']['complete_pairs'] == 1
        assert data['followup']['supplemental']['confirmation']['counts']['new']['missing'] == 3
        assert abs(data['followup']['shared_budget']['known_elapsed_seconds'] - 1435.001042329939) < 1e-8
        assert data['followup']['shared_budget']['within_budget'] is True
        assert page.locator('#followup-mechanism').is_visible()
        assert '34,319' in page.locator('#followup-mechanism-note').inner_text()
        assert '基线 63 项、B 65 项' in page.locator('#followup-mechanism-note').inner_text()
        assert not page.locator('[aria-labelledby="confirmation-title"] details').evaluate('(n)=>n.open')
        assert page.locator('#supplemental-section').evaluate('(n)=>Boolean(n.compareDocumentPosition(document.querySelector("[aria-labelledby=confirmation-title]"))&Node.DOCUMENT_POSITION_FOLLOWING)')
        overflow = page.evaluate('({scroll:document.documentElement.scrollWidth,viewport:innerWidth})')
        assert overflow['scroll'] <= overflow['viewport'], overflow
        screenshot = OUT / f'{label}.png'
        page.screenshot(path=str(screenshot), full_page=False)
        result['screenshots'].append(screenshot.relative_to(RESULTS).as_posix())
        page.select_option('#diagnostic-axis', 'iterations')
        assert '迭代数' in page.locator('#alignment-chart svg').get_attribute('aria-label')
        page.select_option('#diagnostic-metric', 'distance_km')
        assert 'km' in page.locator('#alignment-chart svg').get_attribute('aria-label')
        assert page.locator('#alignment-chart circle').count() == 26
        detail = page.locator('details').filter(has=page.get_by_text('全部检查点的三个原始指标', exact=True))
        detail.locator('summary').click()
        assert detail.evaluate('(n)=>n.open')
        detail.locator('summary').click()
        assert not detail.evaluate('(n)=>n.open')
        stage_checks = []
        for family in page.locator('#original-results .family').all():
            stage_select = family.locator('.family-head select')
            options = stage_select.locator('option').all()
            if len(options) > 1:
                before = stage_select.input_value()
                target = next(o.get_attribute('value') for o in options if o.get_attribute('value') != before)
                stage_select.select_option(target)
                assert stage_select.input_value() == target
                stage_checks.append({'from': before, 'to': target})
                stage_select.select_option(before)
        seed_checks = []
        for family in page.locator('#original-results .family').all():
            select = family.locator('.seed-select')
            options = select.locator('option').all()
            if len(options) > 1:
                seed = options[1].inner_text()
                select.select_option(label=seed)
                assert all(seed in label for label in family.locator('[data-curve] svg').evaluate_all('(nodes)=>nodes.map(n=>n.getAttribute("aria-label"))'))
                seed_checks.append(seed)
        assert stage_checks and seed_checks
        links = local_links(page)
        if label == 'desktop-light':
            page.locator('#followup-mechanism').scroll_into_view_if_needed()
            screenshot = OUT / 'mechanism-desktop-light.png'
            page.screenshot(path=str(screenshot), full_page=False)
            result['screenshots'].append(screenshot.relative_to(RESULTS).as_posix())
        assert not errors and not console_errors and not web_requests, (errors, console_errors, web_requests)
        result['pages'].append({'page': 'index.html', 'label': label, 'viewport': [width, height],
                               'color_scheme': scheme, 'overflow': overflow, 'page_errors': errors,
                               'console_errors': console_errors, 'external_requests': web_requests,
                               'stage_changes': stage_checks, 'seed_changes': seed_checks,
                               'local_links_checked': len(links), 'headline': headline})
        context.close()
    context = browser.new_context(viewport={'width': 1280, 'height': 900})
    context.set_offline(True)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto((OLD / 'index.html').as_uri(), wait_until='load')
    assert page.locator('base').count() == 1
    assert page.locator('#outcome-note').inner_text().startswith('主批 A')
    assert page.locator('a[href="runs.json"]').get_attribute('href') == 'runs.json'
    assert page.locator('a[href="runs.json"]').evaluate('(n)=>n.href') == (RESULTS / 'runs.json').as_uri()
    links = local_links(page)
    assert hashlib.sha256((OLD / 'index.initial.html').read_bytes()).hexdigest() == '959e8d3699e064e5156d41c2b8e7bf0a9cca14a0c1b3f0985f19528fb32016d2'
    result['checks']['old_entry'] = {'base_count': 1, 'resolved_runs': str((RESULTS / 'runs.json').relative_to(ROOT)),
                                   'local_links': links, 'archive_sha256': hashlib.sha256((OLD / 'index.initial.html').read_bytes()).hexdigest()}
    page.goto((RESULTS / 'supplemental_trial/index.html').as_uri(), wait_until='load')
    assert page.locator('#optimization-data').evaluate('(n)=>JSON.parse(n.textContent).run_count') == 26
    local_links(page)
    assert not errors, errors
    result['checks']['child_report'] = {'run_count': 26, 'page_errors': errors, 'offline_loaded': True}
    browser.close()

result['checks']['script_syntax'] = 'all inline executable scripts passed node --check before browser QA'
result['checks']['data_scope'] = 'parent 40 rows and child 26 rows remain separate; only known seed paired in child confirm'
result['checks']['outcome'] = 'Both candidates not promoted; no missing new-seed scores fabricated'
result['passed'] = True
(OUT / 'browser_qa.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(OUT / 'browser_qa.json')
