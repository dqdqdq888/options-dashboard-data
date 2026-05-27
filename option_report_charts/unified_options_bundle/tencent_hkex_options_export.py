from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup


HKEX_OPTIONS_URL = "https://www.hkex.com.hk/eng/sorc/options/stock_options_search.aspx"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/0700.HK"
USER_AGENT = "Mozilla/5.0"
TARGET_CODE = "00700"
TARGET_SYMBOL = "HK.00700"
HKATS_CODE = "TCH"
CONTRACT_MULTIPLIER = 100


@dataclass
class DashboardData:
    spot_price: float
    update_time: str
    expiries: list[str]
    table: pd.DataFrame


class TencentHkexClient:
    def __init__(self, timeout: int = 30, min_interval_seconds: float = 0.6, max_retries: int = 3) -> None:
        self.timeout = timeout
        self.min_interval_seconds = min_interval_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)

            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            self._last_request_at = time.monotonic()
            if response.status_code != 429:
                response.raise_for_status()
                return response

            if attempt >= self.max_retries:
                response.raise_for_status()
            time.sleep((attempt + 1) * 2.0)

        raise RuntimeError("HKEX 请求重试失败")

    def post_options(self, payload: dict[str, Any]) -> str:
        response = self._request(
            "POST",
            HKEX_OPTIONS_URL,
            data=payload,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": HKEX_OPTIONS_URL,
                "Origin": "https://www.hkex.com.hk",
            },
        )
        return response.text

    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "GET",
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        return response.json()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"-", "N/A", "NA", "NULL"}:
        return None
    text = text.replace(",", "").replace("%", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    if number is None:
        return None
    return int(number)


def normalize_option_type(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text == "CALL":
        return "CALL"
    if text == "PUT":
        return "PUT"
    return None


def parse_hkex_expiry(value: Any) -> str | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def build_hkex_payload(request_type: str, expiry_date: str = "", page: int | str = "") -> dict[str, Any]:
    return {
        "action": "ajax",
        "type": request_type,
        "otype": "ucode",
        "code": TARGET_CODE,
        "wtype": "All",
        "mdate": expiry_date,
        "moneyness1": -100,
        "moneyness2": 100,
        "premium1": 0,
        "premium2": 999,
        "ordering": "ucode_asc" if request_type != "initOption" else "",
        "page": page,
    }


def fetch_available_expiries(client: TencentHkexClient) -> list[str]:
    html = client.post_options(build_hkex_payload("initOption"))
    soup = BeautifulSoup(html, "html.parser")
    expiries = []
    for option in soup.find_all("option"):
        value = option.get("value")
        expiry = parse_hkex_expiry(value)
        if expiry:
            expiries.append(expiry)
    return sorted(set(expiries))


def fetch_total_pages(client: TencentHkexClient, expiry_date: str) -> int:
    html = client.post_options(build_hkex_payload("getTotal", expiry_date, 1))
    match = re.search(r"ResultTableTotal'>(\d+)<", html)
    total_rows = int(match.group(1)) if match else 0
    return max(1, math.ceil(total_rows / 10))


def make_contract_symbol(option_type: str, expiry_date: str, strike_price: float) -> str:
    expiry = datetime.strptime(expiry_date, "%Y-%m-%d").strftime("%y%m%d")
    side = "C" if option_type == "CALL" else "P"
    strike = int(round(strike_price * 1000))
    return f"HK.{HKATS_CODE}{expiry}{side}{strike:06d}"


def make_display_symbol(option_type: str, expiry_date: str, strike_price: float) -> str:
    expiry = datetime.strptime(expiry_date, "%Y-%m-%d").strftime("%y%m%d")
    side = "购" if option_type == "CALL" else "沽"
    return f"腾讯 {expiry} {strike_price:.2f} {side}"


def parse_option_rows(html: str, quote_time: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    today = date.today()
    rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 19:
            continue

        option_type = normalize_option_type(cells[6])
        expiry_date = parse_hkex_expiry(cells[7])
        strike_price = to_float(cells[8])
        if not option_type or not expiry_date or strike_price is None:
            continue

        expiry = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        days_to_expiry = max((expiry - today).days, 1)
        rows.append(
            {
                "option_code": make_contract_symbol(option_type, expiry_date, strike_price),
                "option_name": make_display_symbol(option_type, expiry_date, strike_price),
                "option_type": option_type,
                "strike_price": strike_price,
                "expiry_date": expiry_date,
                "contract_multiplier": CONTRACT_MULTIPLIER,
                "last_price": to_float(cells[9]),
                "bid_price": None,
                "ask_price": None,
                "volume": to_int(cells[17]) or 0,
                "open_interest": to_int(cells[18]) or 0,
                "implied_volatility": to_float(cells[12]),
                "quote_time": quote_time,
                "days_to_expiry": days_to_expiry,
                "source_page_url": HKEX_OPTIONS_URL,
            }
        )
    return rows


def fetch_option_chain(client: TencentHkexClient, expiry_date: str) -> list[dict[str, Any]]:
    total_pages = fetch_total_pages(client, expiry_date)
    quote_time = now_utc_iso()
    rows: list[dict[str, Any]] = []
    for page in range(1, total_pages + 1):
        html = client.post_options(build_hkex_payload("search", expiry_date, page))
        rows.extend(parse_option_rows(html, quote_time))
    return rows


def fetch_tencent_quote(client: TencentHkexClient) -> tuple[float, str]:
    payload = client.get_json(YAHOO_CHART_URL, {"range": "1d", "interval": "1m"})
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError("Yahoo 腾讯现价返回为空")

    meta = result[0].get("meta") or {}
    price = to_float(meta.get("regularMarketPrice") or meta.get("previousClose"))
    if price is None:
        raise RuntimeError("Yahoo 腾讯现价字段为空")

    timestamp = to_int(meta.get("regularMarketTime"))
    update_time = datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else now_utc_iso()
    return price, update_time


def collect_tencent_hkex_dashboard_data(timeout: int = 30, max_expiries: int = 0) -> DashboardData:
    client = TencentHkexClient(timeout=timeout)
    spot_price, _ = fetch_tencent_quote(client)
    expiries = fetch_available_expiries(client)
    if max_expiries > 0:
        expiries = expiries[:max_expiries]

    rows: list[dict[str, Any]] = []
    for expiry in expiries:
        rows.extend(fetch_option_chain(client, expiry))

    table = pd.DataFrame(rows)
    if not table.empty:
        table["spot_price"] = spot_price
        table = table.sort_values(["expiry_date", "strike_price", "option_type"]).reset_index(drop=True)

    try:
        spot_price, _ = fetch_tencent_quote(client)
    except Exception:
        pass
    update_time = now_utc_iso()

    return DashboardData(
        spot_price=spot_price,
        update_time=update_time,
        expiries=expiries,
        table=table,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 HKEX 公共网页导出腾讯港股期权链。")
    parser.add_argument("--json-out", default="", help="可选 JSON 输出路径。")
    parser.add_argument("--max-expiries", type=int, default=0, help="最多抓取多少个到期日，0 表示全部。")
    parser.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dashboard = collect_tencent_hkex_dashboard_data(timeout=args.timeout, max_expiries=args.max_expiries)
    payload = {
        "meta": {
            "generated_at_utc": now_utc_iso(),
            "provider": "HKEX",
            "quote_provider": "Yahoo Finance",
            "spot_price": dashboard.spot_price,
            "update_time": dashboard.update_time,
            "expiry_count": len(dashboard.expiries),
            "contract_count": len(dashboard.table),
        },
        "expiries": dashboard.expiries,
        "rows": dashboard.table.to_dict(orient="records"),
    }
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"TENCENT HKEX: {len(dashboard.table)} 条，{len(dashboard.expiries)} 个到期日，现价 {dashboard.spot_price}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
