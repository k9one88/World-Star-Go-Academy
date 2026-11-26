#!/usr/bin/env python3
"""Daily routine health check for World Star Go Academy (index.html & live site).

Runs static HTML/JS/JSON-LD/SEO/i18n checks, plus optional headless browser
(--browser) and live GitHub Pages / CDN (--check-live) verification.
Exits 0 when all checks pass, or 1 if any issue is found.
"""

from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import re
import socketserver
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

LIVE_SITE_URL = "https://k9one88.github.io/World-Star-Go-Academy/"
TZ_MY = timezone(timedelta(hours=8))


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    severity: str = "error"  # "error" or "warning"


def check_inline_js(html: str) -> CheckResult:
    scripts = re.findall(
        r"""<script(?![^>]*\bsrc=)(?![^>]*\btype=['"]application/ld\+json['"])[^>]*>(.*?)</script>""",
        html,
        flags=re.S | re.I,
    )
    if not scripts:
        return CheckResult("Inline JS syntax", False, "No inline JavaScript <script> blocks found.")

    errors = []
    for idx, code in enumerate(scripts, 1):
        res = subprocess.run(["node", "--check"], input=code, text=True, capture_output=True)
        if res.returncode != 0:
            err_line = (res.stderr or res.stdout).strip().splitlines()[:4]
            errors.append(f"Script #{idx}: {' | '.join(err_line)}")
    if errors:
        return CheckResult("Inline JS syntax", False, "; ".join(errors))
    return CheckResult("Inline JS syntax", True, f"All {len(scripts)} inline JS blocks passed `node --check`.")


def check_json_ld(html: str) -> CheckResult:
    blocks = re.findall(
        r"""<script[^>]*\btype=['"]application/ld\+json['"][^>]*>(.*?)</script>""",
        html,
        flags=re.S | re.I,
    )
    if not blocks:
        return CheckResult(
            "Structured Data (JSON-LD)",
            False,
            "No `<script type=\"application/ld+json\">` found (recommended for SEO).",
            severity="warning",
        )
    for idx, raw in enumerate(blocks, 1):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or "@context" not in data or "@type" not in data:
                return CheckResult("Structured Data (JSON-LD)", False, f"JSON-LD #{idx} missing @context or @type.")
        except json.JSONDecodeError as exc:
            return CheckResult("Structured Data (JSON-LD)", False, f"JSON-LD #{idx} invalid JSON: {exc}")
    return CheckResult("Structured Data (JSON-LD)", True, f"{len(blocks)} valid JSON-LD block(s).")


def check_html_hygiene(html: str) -> list[CheckResult]:
    results: list[CheckResult] = []

    # 1. Duplicate IDs
    ids = re.findall(r"""\bid=["']([^"']+)["']""", html)
    dups = {k: v for k, v in Counter(ids).items() if v > 1}
    if dups:
        results.append(CheckResult("Duplicate HTML IDs", False, f"Duplicate IDs found: {dups}"))
    else:
        results.append(CheckResult("Duplicate HTML IDs", True, f"{len(ids)} unique IDs, no duplicates."))

    # 2. Internal anchor links
    anchors = set(re.findall(r"""href=["']#([^"']+)["']""", html))
    id_set = set(ids)
    broken_anchors = sorted(a for a in anchors if a and a not in id_set)
    if broken_anchors:
        results.append(CheckResult("Internal anchor links", False, f"Broken `#` targets: {broken_anchors}"))
    else:
        results.append(CheckResult("Internal anchor links", True, f"All {len(anchors)} internal anchor targets exist."))

    # 3. target="_blank" security
    blank_links = re.findall(r"""<a\b[^>]*target=["']_blank["'][^>]*>""", html, flags=re.I)
    missing_rel = [a for a in blank_links if "noopener" not in a.lower()]
    if missing_rel:
        results.append(
            CheckResult("External link security", False, f"{len(missing_rel)} `target=\"_blank\"` link(s) missing `noopener`.")
        )
    else:
        results.append(CheckResult("External link security", True, f"All {len(blank_links)} `target=\"_blank\"` links include `noopener`."))

    # 4. Excessive blank lines
    lines = html.splitlines()
    empty_count = sum(1 for l in lines if l.strip() == "")
    ratio = empty_count / max(len(lines), 1)
    if ratio > 0.35:
        results.append(
            CheckResult(
                "File formatting (blank lines)",
                False,
                f"{empty_count}/{len(lines)} lines ({ratio:.1%}) are blank — possible doubled line endings.",
                severity="warning",
            )
        )
    else:
        results.append(
            CheckResult("File formatting (blank lines)", True, f"{len(lines)} lines total ({ratio:.1%} blank).")
        )

    return results


def check_seo_meta(html: str) -> CheckResult:
    checks = {
        "<title>": bool(re.search(r"<title>[^<]+</title>", html, flags=re.I)),
        "meta[name=description]": bool(re.search(r"""<meta\b[^>]*name=["']description["'][^>]*content=["'][^"']+["']""", html, flags=re.I)),
        "meta[property=og:title]": bool(re.search(r"""<meta\b[^>]*property=["']og:title["'][^>]*content=["'][^"']+["']""", html, flags=re.I)),
        "meta[property=og:description]": bool(re.search(r"""<meta\b[^>]*property=["']og:description["'][^>]*content=["'][^"']+["']""", html, flags=re.I)),
        "link[rel=icon]": bool(re.search(r"""<link\b[^>]*rel=["'][^"']*icon[^"']*["']""", html, flags=re.I)),
    }
    missing = [k for k, ok in checks.items() if not ok]
    if missing:
        return CheckResult("SEO & Social Metadata", False, f"Missing tags: {', '.join(missing)}", severity="warning")
    return CheckResult("SEO & Social Metadata", True, "Title, description, Open Graph, and favicon all present.")


def check_i18n_parity(html: str) -> CheckResult:
    html_keys = set(re.findall(r"""data-i18n=["']([^"']+)["']""", html))
    dict_match = re.search(r"const\s+i18nData\s*=\s*\{(.*?)\n\s*\};", html, flags=re.S)
    if not dict_match:
        return CheckResult("Bilingual i18n parity", False, "Could not locate `i18nData` dictionary.")

    body = dict_match.group(1)
    en_m = re.search(r"\ben\s*:\s*\{(.*?)\n\s*\}\s*,", body, flags=re.S)
    zh_m = re.search(r"\bzh\s*:\s*\{(.*?)\n\s*\}", body, flags=re.S)
    if not en_m or not zh_m:
        return CheckResult("Bilingual i18n parity", False, "Could not parse `en` or `zh` blocks inside `i18nData`.")

    en_keys = set(re.findall(r"^\s*([a-zA-Z0-9_]+)\s*:", en_m.group(1), flags=re.M))
    zh_keys = set(re.findall(r"^\s*([a-zA-Z0-9_]+)\s*:", zh_m.group(1), flags=re.M))

    issues = []
    if html_keys - en_keys:
        issues.append(f"missing in EN: {sorted(html_keys - en_keys)[:5]}")
    if html_keys - zh_keys:
        issues.append(f"missing in ZH: {sorted(html_keys - zh_keys)[:5]}")
    if en_keys - zh_keys:
        issues.append(f"in EN only: {sorted(en_keys - zh_keys)[:5]}")
    if zh_keys - en_keys:
        issues.append(f"in ZH only: {sorted(zh_keys - en_keys)[:5]}")

    if issues:
        return CheckResult("Bilingual i18n parity", False, "; ".join(issues))
    return CheckResult("Bilingual i18n parity", True, f"All {len(html_keys)} HTML `data-i18n` keys match EN ({len(en_keys)}) & ZH ({len(zh_keys)}).")


def check_browser_e2e(index_path: pathlib.Path) -> CheckResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return CheckResult("Headless browser E2E", False, "Playwright is not installed.")

    repo_dir = index_path.parent.resolve()

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(repo_dir), **kwargs)

        def log_message(self, fmt, *args):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), QuietHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    page_errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("pageerror", lambda err: page_errors.append(str(err)))
            page.goto(f"http://127.0.0.1:{port}/{index_path.name}", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1000)

            h1_en = page.locator("h1").inner_text().strip()
            canvas_count = page.locator("canvas").count()
            desktop_overflow = page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")

            page.click("#btn-zh")
            page.wait_for_timeout(250)
            lang_zh = page.evaluate("() => document.documentElement.lang")
            h1_zh = page.locator("h1").inner_text().strip()

            page.click("#btn-en")
            page.wait_for_timeout(200)

            page.set_viewport_size({"width": 375, "height": 812})
            page.wait_for_timeout(250)
            mobile_overflow = page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
            browser.close()
    finally:
        httpd.shutdown()

    problems = []
    if page_errors:
        problems.append(f"JS page errors: {page_errors[:3]}")
    if canvas_count < 1:
        problems.append("Go board <canvas> not rendered")
    if not h1_en or h1_en == h1_zh or not lang_zh.startswith("zh"):
        problems.append("EN/ZH language switch did not update page content")
    if desktop_overflow:
        problems.append("Desktop (1280px) horizontal overflow detected")
    if mobile_overflow:
        problems.append("Mobile (375px) horizontal overflow detected")

    if problems:
        return CheckResult("Headless browser E2E", False, "; ".join(problems))
    return CheckResult(
        "Headless browser E2E",
        True,
        "Zero JS page errors, `<canvas>` rendered, EN/ZH toggle working, no desktop/mobile horizontal overflow.",
    )


def _http_probe(url: str, timeout: int = 15) -> tuple[bool, str]:
    headers = {"User-Agent": "WorldStarGoAcademy-DailyCheck/1.0"}
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers=headers)
        if method == "GET":
            req.add_header("Range", "bytes=0-1023")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                if 200 <= status < 400:
                    return True, f"HTTP {status}"
                return False, f"HTTP {status}"
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (403, 405, 501):
                continue
            return False, f"HTTP {exc.code}"
        except Exception as exc:
            return False, str(exc)
    return False, "Probe failed"


def check_live_and_cdn(html: str, live_url: str) -> list[CheckResult]:
    results: list[CheckResult] = []

    # 1. Live GitHub Pages check
    try:
        req = urllib.request.Request(live_url, headers={"User-Agent": "WorldStarGoAcademy-DailyCheck/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            ok = 200 <= resp.status < 300 and "World Star Go" in body
            detail = f"HTTP {resp.status}, site reachable at {live_url}" if ok else f"Unexpected response from {live_url} (HTTP {resp.status})"
            results.append(CheckResult("Live GitHub Pages site", ok, detail))
    except Exception as exc:
        results.append(CheckResult("Live GitHub Pages site", False, f"Failed to reach {live_url}: {exc}"))

    # 2. External CDN assets referenced in index.html
    cdn_urls = sorted(
        set(
            re.findall(
                r"""https://(?:cdn\.jsdelivr\.net/[^\s"'<>)]+\.(?:js|onnx)|fonts\.googleapis\.com/css2\?[^\s"'<>)]+)""",
                html,
            )
        )
    )
    failed_cdns = []
    for url in cdn_urls:
        ok, info = _http_probe(url)
        if not ok:
            failed_cdns.append(f"{url} ({info})")
    if failed_cdns:
        results.append(CheckResult("External CDN assets", False, "Unreachable: " + "; ".join(failed_cdns)))
    else:
        results.append(CheckResult("External CDN assets", True, f"All {len(cdn_urls)} CDN dependencies reachable (HTTP 200)."))

    return results


def format_markdown(results: list[CheckResult]) -> str:
    now_my = datetime.now(TZ_MY).strftime("%Y-%m-%d %H:%M (UTC+8)")
    passed_count = sum(1 for r in results if r.passed)
    failed = [r for r in results if not r.passed]
    overall = "✅ 全部通过，无需修正" if not failed else f"⚠️ 发现 {len(failed)} 项需要关注/修正"

    lines = [
        f"## 每日站点例行检查报告 — {now_my}",
        "",
        f"**总体结果**：{overall}（{passed_count}/{len(results)} 项通过）",
        "",
        "| 检查项 | 状态 | 详情 |",
        "| --- | :---: | --- |",
    ]
    for r in results:
        icon = "✅" if r.passed else ("❌" if r.severity == "error" else "⚠️")
        safe_detail = r.detail.replace("|", "\\|")
        lines.append(f"| {r.name} | {icon} | {safe_detail} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run routine health checks for World Star Go Academy.")
    parser.add_argument("--file", default="index.html", help="Path to index.html")
    parser.add_argument("--browser", action="store_true", help="Run Playwright headless Chromium E2E check")
    parser.add_argument("--check-live", action="store_true", help="Check live GitHub Pages URL and external CDN availability")
    parser.add_argument("--live-url", default=LIVE_SITE_URL, help="Live site URL to probe")
    parser.add_argument("--markdown-out", default="", help="Optional path to write Markdown summary")
    args = parser.parse_args()

    index_path = pathlib.Path(args.file)
    if not index_path.exists():
        print(f"ERROR: {index_path} not found", file=sys.stderr)
        return 1

    html = index_path.read_text(encoding="utf-8")

    results: list[CheckResult] = [
        check_inline_js(html),
        check_json_ld(html),
        *check_html_hygiene(html),
        check_seo_meta(html),
        check_i18n_parity(html),
    ]
    if args.browser:
        results.append(check_browser_e2e(index_path))
    if args.check_live:
        results.extend(check_live_and_cdn(html, args.live_url))

    md = format_markdown(results)
    print(md)

    if args.markdown_out:
        pathlib.Path(args.markdown_out).write_text(md, encoding="utf-8")

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
