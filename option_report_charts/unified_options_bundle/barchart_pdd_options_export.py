from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests


CURRENT_DIR = Path(__file__).resolve().parent
GOLD_EXPORTER_DIR = CURRENT_DIR.parent / "gold_futures_options_barchart"
if str(GOLD_EXPORTER_DIR) not in sys.path:
    sys.path.append(str(GOLD_EXPORTER_DIR))

from barchart_gold_options_export import write_json  # noqa: E402


DEFAULT_SYMBOL = "PDD"
DEFAULT_START_URL = "https://www.barchart.com/stocks/quotes/PDD/options"
DEFAULT_JSON_OUT = "pdd_options_barchart.json"
OPTIONS_API_URL = "https://www.barchart.com/proxies/core-api/v1/options/get"
UNDERLYING_API_URL = "https://www.barchart.com/proxies/core-api/v1/quotes/get"
USER_AGENT = "Mozilla/5.0"
OPTION_FIELDS = (
    "symbol,baseSymbol,strikePrice,expirationDate,moneyness,bidPrice,midpoint,askPrice,lastPrice,"
    "priceChange,percentChange,volume,openInterest,openInterestChange,volatility,delta,optionType,"
    "daysToExpiration,expirationDate,tradeTime,averageVolatility,historicVolatility30d,baseNextEarningsDate,"
    "dividendExDate,baseTimeCode,expirationType,impliedVolatilityRank1y"
)


class PddBarchartClient:
    def __init__(self, timeout: int = 30, min_interval_seconds: float = 0.6, max_retries: int = 3) -> None:
        self.timeout = timeout
        self.min_interval_seconds = min_interval_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self.session = requests.Session()

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
        raise RuntimeError("PDD Barchart 请求重试失败")

    def initialize(self, start_url: str) -> None:
        response = self._request(
            start_url,
            headers={"User-Agent": USER_AGENT},
        )

    def _headers(self, referer: str) -> dict[str, str]:
        token = self.session.cookies.get("XSRF-TOKEN", "")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": referer,
            "Origin": "https://www.barchart.com",
        }
        if token:
            headers["X-XSRF-TOKEN"] = unquote(unquote(token))
        return headers

    def fetch_option_payload(self, referer: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            OPTIONS_API_URL,
            params=params,
            headers=self._headers(referer),
        )
        return response.json()

    def fetch_underlying_quote(self, base_symbol: str, referer: str) -> dict[str, Any] | None:
        response = self._request(
            UNDERLYING_API_URL,
            params={
                "symbols": base_symbol,
                "fields": "symbol,symbolName,lastPrice,priceChange,percentChange,bidPrice,askPrice,volume,openInterest,tradeTime",
                "raw": "1",
            },
            headers=self._headers(referer),
        )
        payload = response.json()
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


def write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 Barchart 导出 PDD 美股期权链。")
    parser.add_argument("--base-symbol", default=DEFAULT_SYMBOL, help="美股代码，默认 PDD。")
    parser.add_argument("--start-url", default=DEFAULT_START_URL, help="Barchart 期权页地址。")
    parser.add_argument("--json-out", default=DEFAULT_JSON_OUT, help="JSON 输出路径。")
    parser.add_argument("--csv-out", default="", help="可选的 CSV 输出路径。")
    parser.add_argument("--puts-only", action="store_true", help="只导出 Put。")
    parser.add_argument("--max-expirations", type=int, default=0, help="最多抓取多少个到期日，0 表示全部。")
    parser.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数。")
    return parser.parse_args()


def build_option_params(base_symbol: str, expiration_date: str, expiration_type: str) -> dict[str, Any]:
    return {
        "baseSymbol": base_symbol,
        "fields": OPTION_FIELDS,
        "groupBy": "optionType",
        "expirationDate": expiration_date,
        "expirationType": expiration_type,
        "meta": "field.shortName,expirations",
        "orderBy": "strikePrice",
        "orderDir": "asc",
        "optionsOverview": "true",
        "raw": "1",
    }


def fetch_expiration_map(client: PddBarchartClient, base_symbol: str, start_url: str) -> dict[str, list[str]]:
    payload = client.fetch_option_payload(
        start_url,
        {
            "baseSymbol": base_symbol,
            "fields": OPTION_FIELDS,
            "groupBy": "optionType",
            "expirationDate": "nearest",
            "meta": "field.shortName,expirations",
            "orderBy": "strikePrice",
            "orderDir": "asc",
            "optionsOverview": "true",
            "raw": "1",
        },
    )
    expirations = payload.get("meta", {}).get("expirations") or {}
    return {
        str(exp_type): [str(item) for item in dates]
        for exp_type, dates in expirations.items()
        if isinstance(dates, list)
    }


def flatten_stock_option_rows(
    base_symbol: str,
    page_url: str,
    expiration_date: str,
    expiration_type: str,
    payload: dict[str, Any],
    puts_only: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return rows

    for group_name, items in data.items():
        for item in items:
            raw = item.get("raw", {})
            option_type = raw.get("optionType") or item.get("optionType") or group_name
            if puts_only and str(option_type).upper() != "PUT":
                continue
            rows.append(
                {
                    "base_symbol": base_symbol,
                    "page_url": page_url,
                    "page_symbol": base_symbol,
                    "expiration_date": raw.get("expirationDate") or expiration_date,
                    "expiration_type": raw.get("expirationType") or expiration_type,
                    "days_to_expiry": raw.get("daysToExpiration"),
                    "option_type": option_type,
                    "strike_price": raw.get("strikePrice"),
                    "moneyness": raw.get("moneyness"),
                    "bid_price": raw.get("bidPrice"),
                    "mid_price": raw.get("midpoint"),
                    "ask_price": raw.get("askPrice"),
                    "last_price": raw.get("lastPrice"),
                    "price_change": raw.get("priceChange"),
                    "percent_change": raw.get("percentChange"),
                    "volume": raw.get("volume"),
                    "open_interest": raw.get("openInterest"),
                    "open_interest_change": raw.get("openInterestChange"),
                    "implied_volatility": raw.get("volatility"),
                    "delta": raw.get("delta"),
                    "average_volatility": raw.get("averageVolatility"),
                    "historic_volatility_30d": raw.get("historicVolatility30d"),
                    "base_next_earnings_date": raw.get("baseNextEarningsDate"),
                    "dividend_ex_date": raw.get("dividendExDate"),
                    "base_time_code": raw.get("baseTimeCode"),
                    "implied_volatility_rank_1y": raw.get("impliedVolatilityRank1y"),
                    "trade_time_epoch": raw.get("tradeTime"),
                    "trade_time": item.get("tradeTime"),
                    "symbol": raw.get("symbol") or item.get("symbol"),
                    "base_symbol_from_row": raw.get("baseSymbol") or item.get("baseSymbol"),
                }
            )

    rows.sort(key=lambda row: (row["expiration_date"], row["option_type"], row["strike_price"] or -1))
    return rows


def build_chain_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    chains: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row["expiration_date"]), str(row["expiration_type"]))
        if key in seen:
            continue
        seen.add(key)
        chains.append(
            {
                "expiration_date": row["expiration_date"],
                "expiration_type": row["expiration_type"],
                "days_to_expiry": row["days_to_expiry"],
                "page_url": row["page_url"],
                "base_symbol": row["base_symbol"],
            }
        )
    chains.sort(key=lambda item: (item["expiration_date"], item["expiration_type"]))
    return chains


def build_output_payload(
    base_symbol: str,
    start_url: str,
    expiration_map: dict[str, list[str]],
    underlying_quote: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    puts_only: bool,
) -> dict[str, Any]:
    return {
        "meta": {
            "source": "Barchart website internal stock options endpoint",
            "base_symbol": base_symbol,
            "start_url": start_url,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "puts_only": puts_only,
            "expiration_count": len(build_chain_entries(rows)),
            "row_count": len(rows),
        },
        "underlying": underlying_quote,
        "expiration_map": expiration_map,
        "chains": build_chain_entries(rows),
        "rows": rows,
    }


def collect_pdd_options_payload(
    base_symbol: str = DEFAULT_SYMBOL,
    start_url: str = DEFAULT_START_URL,
    puts_only: bool = False,
    max_expirations: int = 0,
    timeout: int = 30,
) -> dict[str, Any]:
    client = PddBarchartClient(timeout=timeout)
    client.initialize(start_url)
    expiration_map = fetch_expiration_map(client, base_symbol, start_url)
    rows: list[dict[str, Any]] = []
    collected = 0

    for expiration_type in ("weekly", "monthly"):
        for expiration_date in expiration_map.get(expiration_type, []):
            if max_expirations and collected >= max_expirations:
                break
            payload = client.fetch_option_payload(
                start_url,
                build_option_params(base_symbol, expiration_date, expiration_type),
            )
            rows.extend(
                flatten_stock_option_rows(
                    base_symbol=base_symbol,
                    page_url=start_url,
                    expiration_date=expiration_date,
                    expiration_type=expiration_type,
                    payload=payload,
                    puts_only=puts_only,
                )
            )
            collected += 1
        if max_expirations and collected >= max_expirations:
            break

    underlying_quote = client.fetch_underlying_quote(base_symbol, start_url)
    return build_output_payload(
        base_symbol=base_symbol,
        start_url=start_url,
        expiration_map=expiration_map,
        underlying_quote=underlying_quote,
        rows=rows,
        puts_only=puts_only,
    )


def main() -> int:
    args = parse_args()
    try:
        payload = collect_pdd_options_payload(
            base_symbol=args.base_symbol,
            start_url=args.start_url,
            puts_only=args.puts_only,
            max_expirations=args.max_expirations,
            timeout=args.timeout,
        )
        write_json(Path(args.json_out), payload)
        if args.csv_out:
            write_rows_csv(Path(args.csv_out), payload["rows"])
        print(f"已输出 {len(payload['rows'])} 条 PDD 期权记录到 {args.json_out}")
        if args.csv_out:
            print(f"已输出 CSV 到 {args.csv_out}")
        return 0
    except Exception as exc:
        print(f"PDD 抓取失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
