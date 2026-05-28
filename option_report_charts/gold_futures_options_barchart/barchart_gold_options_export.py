from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests


BASE_URL = "https://www.barchart.com"
DEFAULT_START_URL = f"{BASE_URL}/futures/quotes/GC*0/options"
QUOTE_API_URL = f"{BASE_URL}/proxies/core-api/v1/quotes/get"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


@dataclass
class SelectOption:
    label: str
    url: str
    selected: bool


@dataclass
class OptionPage:
    url: str
    futures_symbol: str
    api_symbol: str
    option_type_label: str
    month_label: str
    expiration_days: str | None
    expiration_date: str | None
    point_value: float | None
    api_params: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 Barchart 导出 COMEX 黄金期货期权链。")
    parser.add_argument("--start-url", default=DEFAULT_START_URL, help="起始 Barchart 期权页面。")
    parser.add_argument("--json-out", default="gold_options_barchart.json", help="JSON 输出文件路径。")
    parser.add_argument("--csv-out", default="", help="可选的 CSV 输出文件路径。")
    parser.add_argument("--puts-only", action="store_true", help="只导出 Put。")
    parser.add_argument("--max-pages", type=int, default=0, help="最多抓取多少个期权页面，0 表示全部。")
    parser.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数。")
    return parser.parse_args()


class BarchartClient:
    def __init__(self, timeout: int = 30, min_interval_seconds: float = 0.6, max_retries: int = 3) -> None:
        self.timeout = timeout
        self.min_interval_seconds = min_interval_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _request(self, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)
            response = self.session.get(url, timeout=self.timeout, **kwargs)
            self._last_request_at = time.monotonic()
            if response.status_code != 429:
                response.raise_for_status()
                return response
            if attempt >= self.max_retries:
                response.raise_for_status()
            time.sleep((attempt + 1) * 2.0)
        raise RuntimeError("Barchart 请求重试失败")

    def fetch_html(self, url: str) -> str:
        response = self._request(url)
        return response.text

    def fetch_quote_payload(self, referer: str, params: dict[str, Any]) -> dict[str, Any]:
        token = self.session.cookies.get("XSRF-TOKEN", "")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": referer,
            "Origin": BASE_URL,
        }
        if token:
            headers["X-XSRF-TOKEN"] = unquote(unquote(token))
        response = self._request(QUOTE_API_URL, params=params, headers=headers)
        return response.json()

    def fetch_underlying_quote(self, futures_symbol: str, referer: str) -> dict[str, Any] | None:
        payload = self.fetch_quote_payload(
            referer=referer,
            params={
                "symbols": futures_symbol,
                "fields": "symbol,symbolName,lastPrice,priceChange,percentChange,bidPrice,askPrice,volume,openInterest,tradeTime",
                "raw": "1",
            },
        )
        rows = payload.get("data") or []
        if not rows:
            return None
        row = rows[0]
        raw = row.get("raw", {})
        return {
            "symbol": row.get("symbol"),
            "symbol_name": row.get("symbolName"),
            "last_price": raw.get("lastPrice"),
            "bid_price": raw.get("bidPrice"),
            "ask_price": raw.get("askPrice"),
            "price_change": raw.get("priceChange"),
            "percent_change": raw.get("percentChange"),
            "volume": raw.get("volume"),
            "open_interest": raw.get("openInterest"),
            "trade_time_epoch": raw.get("tradeTime"),
            "trade_time": row.get("tradeTime"),
        }


def extract_options_from_select(html: str, select_id: str) -> list[SelectOption]:
    select_match = re.search(rf'<select[^>]*id="{re.escape(select_id)}"[^>]*>(.*?)</select>', html, re.S)
    if not select_match:
        return []
    block = select_match.group(1)
    results: list[SelectOption] = []
    for attrs, value, label in re.findall(r"<option([^>]*)value=\"([^\"]+)\"[^>]*>(.*?)</option>", block, re.S):
        cleaned_label = re.sub(r"<[^>]+>", "", unescape(label)).strip()
        cleaned_url = urljoin(BASE_URL, unescape(value).strip())
        results.append(
            SelectOption(
                label=cleaned_label,
                url=cleaned_url,
                selected="selected" in attrs,
            )
        )
    return results


def extract_text_pattern(html: str, pattern: str) -> tuple[str, ...] | None:
    match = re.search(pattern, html, re.S)
    if not match:
        return None
    return tuple(unescape(part).strip() for part in match.groups())


def parse_page(html: str, url: str) -> tuple[OptionPage, list[SelectOption], list[SelectOption]]:
    api_match = re.search(r'data-api-config="([^"]+)"', html)
    if not api_match:
        raise ValueError(f"未在页面中找到 data-api-config: {url}")

    api_config = json.loads(unescape(api_match.group(1)))
    api_params = dict(api_config.get("api", {}))
    api_params["raw"] = "1"

    type_options = extract_options_from_select(html, "bc-options-toolbar__dropdown-type")
    month_options = extract_options_from_select(html, "bc-options-toolbar__dropdown-month")
    selected_type = next((item for item in type_options if item.selected), type_options[0] if type_options else None)
    selected_month = next((item for item in month_options if item.selected), month_options[0] if month_options else None)

    expiry_parts = extract_text_pattern(
        html,
        r"<strong>([^<]+)</strong>\s*to expiration on\s*<strong>([^<]+)</strong>",
    )
    point_parts = extract_text_pattern(
        html,
        r"Price Value of Option point:\s*<strong>\$([^<]+)</strong>",
    )
    futures_match = re.search(r"/futures/quotes/([^/]+)/options", urlparse(url).path)
    futures_symbol = futures_match.group(1) if futures_match else ""
    api_symbol = str(api_params.get("symbol", ""))
    if futures_symbol == "GC*0" and api_symbol:
        futures_symbol = api_symbol

    page = OptionPage(
        url=url,
        futures_symbol=futures_symbol,
        api_symbol=api_symbol,
        option_type_label=selected_type.label if selected_type else "",
        month_label=selected_month.label if selected_month else "",
        expiration_days=expiry_parts[0] if expiry_parts else None,
        expiration_date=expiry_parts[1] if expiry_parts else None,
        point_value=float(point_parts[0].replace(",", "")) if point_parts else None,
        api_params=api_params,
    )
    return page, type_options, month_options


def flatten_option_rows(page: OptionPage, payload: dict[str, Any], puts_only: bool) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    groups: dict[str, list[dict[str, Any]]]
    if isinstance(data, dict):
        groups = data
    else:
        groups = {"All": data}

    rows: list[dict[str, Any]] = []
    for group_name, items in groups.items():
        for item in items:
            raw = item.get("raw", {})
            option_type = raw.get("optionType") or item.get("optionType") or group_name
            if puts_only and str(option_type).lower() != "put":
                continue
            rows.append(
                {
                    "futures_symbol": page.futures_symbol,
                    "page_symbol": page.api_symbol,
                    "page_url": page.url,
                    "option_type_group": page.option_type_label,
                    "month_label": page.month_label,
                    "expiration_days": page.expiration_days,
                    "expiration_date": page.expiration_date,
                    "point_value": page.point_value,
                    "option_type": option_type,
                    "strike": raw.get("strike"),
                    "last_price": raw.get("lastPrice"),
                    "bid_price": raw.get("bidPrice"),
                    "ask_price": raw.get("askPrice"),
                    "price_change": raw.get("priceChange"),
                    "volume": raw.get("volume"),
                    "open_interest": raw.get("openInterest"),
                    "premium": raw.get("premium"),
                    "trade_time_epoch": raw.get("tradeTime"),
                    "trade_time": item.get("tradeTime"),
                    "symbol": raw.get("symbol") or item.get("symbol"),
                    "long_symbol": raw.get("longSymbol") or item.get("longSymbol"),
                }
            )
    rows.sort(key=lambda row: (row["option_type"], row["strike"] if row["strike"] is not None else -1))
    return rows


def discover_pages(client: BarchartClient, start_url: str, max_pages: int = 0) -> list[OptionPage]:
    queue = deque([start_url])
    seen: set[str] = set()
    seen_api_symbols: set[str] = set()
    pages: list[OptionPage] = []

    while queue:
        if max_pages and len(pages) >= max_pages:
            break
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)

        html = client.fetch_html(url)
        try:
            page, type_options, month_options = parse_page(html, url)
        except ValueError:
            if "No options were found" in html or "There is no option data" in html:
                continue
            raise
        if page.api_symbol in seen_api_symbols:
            continue
        seen_api_symbols.add(page.api_symbol)
        pages.append(page)

        for option in [*type_options, *month_options]:
            if "/futures/quotes/GC" not in option.url:
                continue
            if option.url not in seen:
                queue.append(option.url)

    pages.sort(key=lambda page: (page.option_type_label, page.month_label, page.futures_symbol, page.api_symbol))
    return pages


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "futures_symbol",
        "page_symbol",
        "page_url",
        "option_type_group",
        "month_label",
        "expiration_days",
        "expiration_date",
        "point_value",
        "option_type",
        "strike",
        "last_price",
        "bid_price",
        "ask_price",
        "price_change",
        "volume",
        "open_interest",
        "premium",
        "trade_time_epoch",
        "trade_time",
        "symbol",
        "long_symbol",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_output_payload(
    start_url: str,
    pages: list[OptionPage],
    underlying_quotes: dict[str, dict[str, Any] | None],
    rows: list[dict[str, Any]],
    puts_only: bool,
) -> dict[str, Any]:
    return {
        "meta": {
            "source": "Barchart website internal quotes endpoint",
            "start_url": start_url,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "puts_only": puts_only,
            "page_count": len(pages),
            "row_count": len(rows),
        },
        "underlyings": underlying_quotes,
        "chains": [asdict(page) for page in pages],
        "rows": rows,
    }


def collect_gold_options_payload(
    start_url: str = DEFAULT_START_URL,
    puts_only: bool = False,
    max_pages: int = 0,
    timeout: int = 30,
) -> dict[str, Any]:
    client = BarchartClient(timeout=timeout)
    pages = discover_pages(client, start_url, max_pages=max_pages)
    rows: list[dict[str, Any]] = []
    for page in pages:
        payload = client.fetch_quote_payload(page.url, page.api_params)
        rows.extend(flatten_option_rows(page, payload, puts_only=puts_only))

    underlying_quotes: dict[str, dict[str, Any] | None] = {}
    for futures_symbol in sorted({page.futures_symbol for page in pages if page.futures_symbol}):
        referer = next(page.url for page in pages if page.futures_symbol == futures_symbol)
        underlying_quotes[futures_symbol] = client.fetch_underlying_quote(futures_symbol, referer)

    return build_output_payload(
        start_url=start_url,
        pages=pages,
        underlying_quotes=underlying_quotes,
        rows=rows,
        puts_only=puts_only,
    )


def main() -> int:
    args = parse_args()

    try:
        output = collect_gold_options_payload(
            start_url=args.start_url,
            puts_only=args.puts_only,
            max_pages=args.max_pages,
            timeout=args.timeout,
        )

        json_path = Path(args.json_out)
        write_json(json_path, output)

        if args.csv_out:
            write_csv(Path(args.csv_out), output["rows"])

        print(f"已输出 {len(output['rows'])} 条期权记录到 {json_path}")
        if args.csv_out:
            print(f"已输出 CSV 到 {args.csv_out}")
        return 0
    except Exception as exc:
        print(f"抓取失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
