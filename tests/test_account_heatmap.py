"""Real component/browser tests with existing esbuild + Python Playwright.

Build only AccountCard (not App/main). Serve bundles from memory and abort all
external requests. No project configuration, credentials or backend is loaded.
Run: python -m pytest tests/test_account_heatmap.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle():
    node = shutil.which("node")
    if not node or not (ROOT / "web/node_modules/esbuild").is_dir():
        pytest.skip("requires existing local node/esbuild dependencies")
    script = r'''
      import { build } from "esbuild";
      const result = await build({
        stdin: { contents: `
          import React from 'react';
          import {createRoot} from 'react-dom/client';
          import AccountCard from './src/components/arc/account-card/account-card.jsx';
          const root = createRoot(document.getElementById('root'));
          window.renderAccount = (account, series = []) => root.render(
            <AccountCard account={account} series={series}
              chartColors={{up:'#008800',down:'#cc0000'}} />
          );`, loader: 'jsx', resolveDir: process.cwd() },
        bundle: true, write: false, outdir: 'unused-memory-output',
        alias: {'@': process.cwd() + '/src'}, jsx: 'automatic',
        define: {'process.env.NODE_ENV': '"production"'},
        loader: {'.svg':'dataurl', '.png':'dataurl'},
      });
      console.log(JSON.stringify(Object.fromEntries(result.outputFiles.map(f =>
        [f.path.endsWith('.css') ? 'css' : 'js', f.text]))));
    '''
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            cwd=ROOT / "web", capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        # Reuse installed Chrome if the matching Playwright Chromium is unavailable.
        channel = "chromium" if Path(p.chromium.executable_path).exists() else "chrome"
        browser = p.chromium.launch(channel=channel)
        yield browser
        browser.close()


@pytest.fixture
def page(browser, bundle):
    context = browser.new_context(viewport={"width": 600, "height": 400}, timezone_id="America/Los_Angeles")
    page = context.new_page()
    page.clock.install(time=datetime(2026, 3, 4, 0, 30, tzinfo=timezone.utc))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve(route):
        url = route.request.url
        if url == "http://equity.test/":
            route.fulfill(content_type="text/html", body='''<!doctype html><html><head>
                <link rel="stylesheet" href="/card.css"><style>
                * {box-sizing:border-box} body {margin:16px;font-family:Arial,sans-serif}
                #root {width:360px;--accent-hue:45;--foreground:#1c2333}
                </style></head><body><div id="root"></div><script src="/card.js"></script></body></html>''')
        elif url == "http://equity.test/card.js":
            route.fulfill(content_type="text/javascript", body=bundle["js"])
        elif url == "http://equity.test/card.css":
            route.fulfill(content_type="text/css", body=bundle["css"])
        else:
            route.abort()

    page.route("**/*", serve)
    page.goto("http://equity.test/")
    page.wait_for_function("typeof window.renderAccount === 'function'")
    yield page
    assert not errors
    context.close()


def record(date, value):
    t = int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return {"date": date, "firstTs": t, "lastTs": t, "open": value, "close": value,
            "high": value, "low": value}


def render(page, history=None, **overrides):
    account = {"id": "test-account", "name": "test", "balance": 100, "unrealized": 0,
               "positions": [], "equityHistory": history}
    account.update(overrides)
    page.evaluate("a => window.renderAccount(a)", account)
    return account


def test_year_selection_utc_leap_days_gaps_and_today(page):
    render(page, [record("2024-02-28", 100), record("2024-02-29", 0), record("2026-03-04", -5)])
    years = page.get_by_role("combobox", name="权益年份")
    playwright.expect(years).to_have_value("2026")
    assert years.locator("option").all_text_contents() == ["2026 年", "2025 年", "2024 年"]
    playwright.expect(page.get_by_text("首次记录 2024-02-28 · UTC")).to_be_visible()
    assert page.locator("[data-date='2026-03-04']").count() == 1  # UTC, not March 3 in browser TZ
    assert page.locator("[data-date='2026-03-05']").count() == 0
    years.select_option("2024")
    assert page.locator("[data-date='2024-02-27']").count() == 0
    leap = page.locator("[data-date='2024-02-29']")
    playwright.expect(leap).to_have_attribute("aria-label", "2024-02-29 · 日末总权益 0 USDT")
    assert leap.get_attribute("data-missing") is None
    gap = page.locator("[data-date='2024-03-01']")
    playwright.expect(gap).to_have_attribute("data-missing", "true")
    playwright.expect(gap).to_have_attribute("aria-label", "2024-03-01 · 无数据")
    # A partial first month and next month can share a week: no stacked labels.
    labels = page.locator("[style*='grid-column']").evaluate_all("els => els.map(e => e.getBoundingClientRect().y)")
    assert len(set(labels)) == 1
    years.select_option("2025")
    assert page.locator("[data-date]").count() == 365
    assert page.locator("[data-missing]").count() == 365  # whole missing year selectable


def test_absolute_scale_focus_hover_and_keyboard_navigation(page):
    render(page, [record("2025-12-31", 200), record("2026-01-01", 100),
                  record("2026-01-02", 200), record("2026-01-03", -10)])
    high = page.locator("[data-date='2026-01-02']")
    low = page.locator("[data-date='2026-01-03']")
    playwright.expect(high).to_be_visible()
    high_color = high.evaluate("e => getComputedStyle(e).backgroundColor")
    assert high_color != low.evaluate("e => getComputedStyle(e).backgroundColor")
    high.focus()
    playwright.expect(page.locator("[aria-live='polite']")).to_have_text("2026-01-02 · 日末总权益 200 USDT")
    high.press("ArrowDown")
    playwright.expect(low).to_be_focused()
    playwright.expect(page.locator("[aria-live='polite']")).to_have_text("2026-01-03 · 日末总权益 -10 USDT")
    gap = page.locator("[data-date='2026-01-04']")
    gap.hover()
    playwright.expect(page.locator("[aria-live='polite']")).to_have_text("2026-01-04 · 无数据")
    page.mouse.move(590, 390)
    playwright.expect(page.locator("[aria-live='polite']")).to_have_text("2026-01-03 · 日末总权益 -10 USDT")
    low.press("End")
    playwright.expect(page.locator("[data-date='2026-03-04']")).to_be_focused()
    page.get_by_role("combobox").select_option("2025")
    old = page.locator("[data-date='2025-12-31']")
    assert old.evaluate("e => getComputedStyle(e).backgroundColor") == high_color  # same absolute value


def test_history_absent_invalid_and_single_zero_never_use_short_series(page):
    account = render(page)
    playwright.expect(page.get_by_text("暂无每日权益记录 · UTC")).to_be_visible()
    page.evaluate("a => window.renderAccount(a, [{t:Date.now(),bal:900,upnl:10}])", account)
    assert page.locator("[data-date]").count() == 0
    render(page, [record("2026-03-04", 0), record("2026-03-05", 999),
                  {"date": "2026-02-30", "close": 30}, {"date": "2026-03-03", "close": None}])
    cell = page.locator("[data-date='2026-03-04']")
    playwright.expect(cell).to_have_attribute("aria-label", "2026-03-04 · 最新总权益 0 USDT")
    assert page.locator("[data-date]").count() == 1
    assert "NaN" not in cell.get_attribute("style")
    assert cell.bounding_box()["width"] == 7  # one sample must not stretch into a bar
    cell.focus()
    playwright.expect(page.locator("[aria-live='polite']")).to_contain_text("0 USDT")


def test_error_and_positions_preserve_existing_branches(page):
    render(page, [record("2026-01-01", 100)], error="unauthorized")
    playwright.expect(page.get_by_role("alert")).to_have_text("数据获取失败")
    assert page.get_by_role("group", name="UTC 每日总权益热力图").count() == 0
    render(page, [record("2026-01-01", 100)], positions=[{
        "symbol": "BTCUSDT", "amt": 0.01, "unrealized": 5, "unrealized_pct": 2,
        "entry": 80000, "mark": 80500, "notional": 805, "margin": 80,
        "leverage": 10, "liquidation": 72000,
    }])
    playwright.expect(page.get_by_text("BTC", exact=True)).to_be_visible()
    playwright.expect(page.get_by_text("开仓", exact=True)).to_be_visible()
    playwright.expect(page.get_by_text("10x", exact=True)).to_be_visible()
    assert page.get_by_role("group", name="UTC 每日总权益热力图").count() == 0
    assert page.locator("section").evaluate("e => e.getBoundingClientRect().height") == 188
    page.evaluate("window.renderAccount(null)")
    playwright.expect(page.get_by_text("未接入账户")).to_be_visible()


@pytest.mark.parametrize("width", [260, 360, 480])
def test_calendar_and_keyboard_detail_fit_compact_card(page, width):
    page.locator("#root").evaluate("(e, width) => e.style.width = width + 'px'", width)
    render(page, [record("2024-01-01", 100), record("2026-03-04", 200)])
    page.get_by_role("combobox").select_option("2024")
    last = page.locator("[data-date='2024-12-31']")
    last.focus()
    last.press("Home")
    first = page.locator("[data-date='2024-01-01']")
    playwright.expect(first).to_be_focused()
    card = page.locator("section").bounding_box()
    detail = page.locator("[aria-live='polite']").bounding_box()
    assert card["height"] == 188
    assert detail["y"] + detail["height"] <= card["y"] + card["height"] - 4
    cell = first.bounding_box()
    assert cell["width"] == 7 and cell["height"] == 7
    assert card["x"] <= cell["x"] < card["x"] + width
    assert first.evaluate("""e => {
        const scroll = e.parentElement.parentElement.parentElement;
        return scroll.clientHeight >= scroll.firstElementChild.scrollHeight;
    }"""), "all seven weekday rows must remain visible above the horizontal scrollbar"
