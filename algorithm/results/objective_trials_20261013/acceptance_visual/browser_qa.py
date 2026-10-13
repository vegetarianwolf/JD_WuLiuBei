"""Offline, isolated-browser acceptance of both saved reports; no solver run."""
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright


OUT = Path(__file__).resolve().parent
RESULTS = OUT.parent
records = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    for name, directory, expected_rows in (
        ("parent", RESULTS, 39), ("compact", RESULTS / "compact_trial", 26),
    ):
        for viewport, width, height, scheme in (
            ("desktop-light", 1280, 960, "light"),
            ("mobile-dark", 360, 800, "dark"),
        ):
            context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
            context.set_offline(True)
            page = context.new_page()
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url)
                    if request.url.startswith(("http:", "https:")) else None)
            target = directory / "index.html"
            page.goto(target.as_uri(), wait_until="load")
            page.wait_for_function("document.querySelector('#scope').textContent.length>0")
            data = page.locator("#report-data").evaluate("n=>JSON.parse(n.textContent)")
            assert data["run_count"] == expected_rows
            assert data["validation"]["all_runs_successful"]
            assert data["validation"]["protocol_complete"]
            assert not data["validation"]["decision_audit"]["default_eligible"]
            assert "保留现有默认算法" in page.locator("#outcome").inner_text()
            assert page.locator("#metrics svg").count() == 5
            assert page.locator("#curves svg").count() == 3
            assert "30 秒预算" in page.locator("#scope").inner_text()
            if name == "compact":
                assert "10.45 / 45 分钟" in page.locator("#facts").inner_text()
                assert page.locator("#pairs td.status.bad").count() == 3
            else:
                assert "10.45 / 45 分钟" in page.locator("#related-trial").inner_text()
                page.select_option("#variant", "objective")
                assert "第一目标奖励" in page.locator("#candidate-label").inner_text()
                page.select_option("#variant", "boundary")
            page.locator("#metric-heading").evaluate("el=>el.scrollIntoView({block:'start'})")
            image = OUT / f"{name}-{viewport}.png"
            page.screenshot(path=str(image), full_page=False)
            dims = page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth})")
            assert dims["scroll"] <= dims["width"], dims
            seeds = page.locator("#seed option").evaluate_all("nodes=>nodes.map(n=>n.value)")
            page.select_option("#seed", seeds[-1])
            assert all(seeds[-1] in text for text in page.locator("#curves svg").evaluate_all(
                "nodes=>nodes.map(n=>n.getAttribute('aria-label'))"))
            mark = page.locator("#curves [data-chart-hit]").first
            box = mark.bounding_box()
            mark.click(position={"x": box["width"] / 2, "y": box["height"] / 2})
            assert "当前默认基线" in page.locator("#curve-detail").inner_text()
            page.select_option("#stage", "screen")
            single_screen = next(option["value"] for option in page.locator("#scenario option").evaluate_all(
                "nodes=>nodes.map(n=>({value:n.value,label:n.textContent}))") if "单机" in option["label"])
            page.select_option("#scenario", single_screen)
            assert "25 项任务" in page.locator("#scope").inner_text()
            assert "5 秒预算" in page.locator("#scope").inner_text()
            assert page.locator("#pairs tbody tr").count() == 2
            assert page.locator("#curves svg").count() == 3
            links = []
            for href in page.locator("a[href]").evaluate_all("nodes=>nodes.map(n=>n.href)"):
                parsed = urlparse(href)
                assert parsed.scheme == "file", href
                assert Path(unquote(parsed.path)).is_file(), href
                links.append(href)
            assert not errors and not requests, (errors, requests)
            records.append({"page": str(target.relative_to(RESULTS)), "viewport": [width, height],
                            "color_scheme": scheme, "offline": True, "overflow": dims,
                            "page_errors": errors, "external_requests": requests,
                            "local_links_checked": len(links), "data_rows": expected_rows,
                            "interactions": ["candidate where available", "seed", "curve click", "stage and scenario"],
                            "screenshot": image.name,
                            "html_sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
            context.close()
    browser.close()

result = {"passed": True, "browser": "Playwright Chromium, isolated temporary profile",
          "pages": records, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
(OUT / "browser_qa.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"passed": True, "pages": len(records)}, ensure_ascii=False))
