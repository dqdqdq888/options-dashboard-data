from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def clean_json_value(value: Any) -> Any:
    value = clean_value(value)
    if isinstance(value, dict):
        return {key: clean_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json_value(item) for item in value]
    return value


def to_float(value: Any) -> float | None:
    value = clean_value(value)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    if number is None:
        return None
    return int(number)


def normalize_iv(value: Any) -> float | None:
    iv = to_float(value)
    if iv is None:
        return None
    return iv / 100.0 if iv > 3 else iv


def parse_days_label(value: Any) -> int | None:
    if value is None:
        return None


def compute_premium_put_annualized_pct(
    option_type: Any,
    premium: Any,
    strike_price: Any,
    days_to_expiry: Any,
) -> float | None:
    if str(option_type).upper() != "PUT":
        return None
    premium_value = to_float(premium)
    strike_value = to_float(strike_price)
    days_value = to_int(days_to_expiry)
    if premium_value is None or strike_value in (None, 0) or days_value in (None, 0):
        return None
    return round((premium_value / strike_value) * (365.0 / days_value) * 100.0, 2)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().split()[0]
    try:
        return int(float(text))
    except ValueError:
        return None


def build_source_meta(
    provider: str,
    source_type: str,
    delayed: bool,
    retrieved_at: str | None,
    status: str = "ok",
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "source_type": source_type,
        "delayed": delayed,
        "status": status,
        "retrieved_at": retrieved_at,
        "notes": notes or [],
    }


def build_summary(contracts: list[dict[str, Any]]) -> dict[str, Any]:
    if not contracts:
        return {
            "contract_count": 0,
            "put_count": 0,
            "call_count": 0,
            "expiry_count": 0,
            "underlying_count": 0,
            "top_expiries_by_open_interest": [],
            "top_puts_by_open_interest": [],
            "top_puts_by_volume": [],
            "top_puts_by_annualized": [],
        }

    df = pd.DataFrame(contracts).copy()
    for column in (
        "open_interest",
        "volume",
        "implied_volatility",
        "cash_secured_put_annualized_pct",
        "strike_price",
        "last_price",
        "bid_price",
        "ask_price",
    ):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    expiry_df = (
        df.groupby("expiry_date", dropna=True, as_index=False)
        .agg(
            total_open_interest=("open_interest", "sum"),
            total_volume=("volume", "sum"),
            avg_iv=("implied_volatility", "mean"),
            contract_count=("contract_symbol", "count"),
        )
        .sort_values(["total_open_interest", "total_volume"], ascending=False)
    )

    put_df = df[df["option_type"].astype(str).str.upper() == "PUT"].copy()
    top_put_columns = [
        "contract_symbol",
        "underlying_symbol",
        "expiry_date",
        "strike_price",
        "last_price",
        "bid_price",
        "ask_price",
        "volume",
        "open_interest",
        "implied_volatility",
        "cash_secured_put_annualized_pct",
    ]
    top_put_columns = [column for column in top_put_columns if column in put_df.columns]

    top_puts_by_open_interest = (
        put_df.sort_values(["open_interest", "volume"], ascending=False).head(8)[top_put_columns].to_dict(orient="records")
        if not put_df.empty
        else []
    )
    top_puts_by_volume = (
        put_df.sort_values(["volume", "open_interest"], ascending=False).head(8)[top_put_columns].to_dict(orient="records")
        if not put_df.empty
        else []
    )

    annualized_puts = []
    if "cash_secured_put_annualized_pct" in put_df.columns and put_df["cash_secured_put_annualized_pct"].notna().any():
        annualized_puts = (
            put_df.sort_values(["cash_secured_put_annualized_pct", "open_interest"], ascending=False)
            .head(8)[top_put_columns]
            .to_dict(orient="records")
        )

    return clean_json_value({
        "contract_count": len(df),
        "put_count": int((df["option_type"].astype(str).str.upper() == "PUT").sum()),
        "call_count": int((df["option_type"].astype(str).str.upper() == "CALL").sum()),
        "expiry_count": int(df["expiry_date"].nunique(dropna=True)),
        "underlying_count": int(df["underlying_symbol"].nunique(dropna=True)),
        "top_expiries_by_open_interest": expiry_df.head(8).to_dict(orient="records"),
        "top_puts_by_open_interest": top_puts_by_open_interest,
        "top_puts_by_volume": top_puts_by_volume,
        "top_puts_by_annualized": annualized_puts,
    })


def normalize_gold_payload(payload: dict[str, Any]) -> dict[str, Any]:
    underlyings = []
    for symbol, quote in sorted((payload.get("underlyings") or {}).items()):
        quote = quote or {}
        underlyings.append(
            {
                "symbol": symbol,
                "display_name": quote.get("symbol_name") or "Gold",
                "market": "COMEX",
                "asset_class": "futures",
                "last_price": to_float(quote.get("last_price")),
                "bid_price": to_float(quote.get("bid_price")),
                "ask_price": to_float(quote.get("ask_price")),
                "price_change": to_float(quote.get("price_change")),
                "percent_change": to_float(quote.get("percent_change")),
                "volume": to_int(quote.get("volume")),
                "open_interest": to_int(quote.get("open_interest")),
                "trade_time": quote.get("trade_time"),
                "trade_time_epoch": to_int(quote.get("trade_time_epoch")),
            }
        )

    expiries = []
    for chain in payload.get("chains") or []:
        expiries.append(
            {
                "underlying_symbol": chain.get("futures_symbol"),
                "chain_symbol": chain.get("api_symbol"),
                "label": chain.get("month_label"),
                "chain_type": chain.get("option_type_label"),
                "expiry_date": chain.get("expiration_date"),
                "days_to_expiry": parse_days_label(chain.get("expiration_days")),
                "page_url": chain.get("url"),
            }
        )

    contracts = []
    for row in payload.get("rows") or []:
        contracts.append(
            {
                "underlying_symbol": row.get("futures_symbol"),
                "contract_symbol": row.get("symbol"),
                "display_symbol": row.get("long_symbol"),
                "chain_symbol": row.get("page_symbol"),
                "option_type": row.get("option_type"),
                "expiry_date": row.get("expiration_date"),
                "days_to_expiry": parse_days_label(row.get("expiration_days")),
                "strike_price": to_float(row.get("strike")),
                "last_price": to_float(row.get("last_price")),
                "bid_price": to_float(row.get("bid_price")),
                "ask_price": to_float(row.get("ask_price")),
                "mid_price": None,
                "volume": to_int(row.get("volume")),
                "open_interest": to_int(row.get("open_interest")),
                "implied_volatility": None,
                "delta": None,
                "premium": to_float(row.get("premium")),
                "price_change": to_float(row.get("price_change")),
                "percent_change": None,
                "moneyness_pct": None,
                "moneyness_bucket": None,
                "cash_secured_put_annualized_pct": compute_premium_put_annualized_pct(
                    row.get("option_type"),
                    row.get("last_price"),
                    row.get("strike"),
                    parse_days_label(row.get("expiration_days")),
                ),
                "trade_time": row.get("trade_time"),
                "trade_time_epoch": to_int(row.get("trade_time_epoch")),
                "contract_multiplier": to_int(row.get("point_value")),
                "source_page_url": row.get("page_url"),
            }
        )

    return {
        "asset_code": "GC",
        "display_name": "COMEX Gold Options",
        "market": "COMEX",
        "asset_class": "futures_options",
        "source_meta": build_source_meta(
            provider="Barchart",
            source_type="website_internal_api",
            delayed=True,
            retrieved_at=(payload.get("meta") or {}).get("generated_at_utc"),
            notes=["Barchart 网页与接口为延迟/混合时点数据，适合展示与筛选。"],
        ),
        "underlyings": underlyings,
        "expiries": expiries,
        "contracts": contracts,
        "summary": build_summary(contracts),
    }


def normalize_pdd_payload(payload: dict[str, Any]) -> dict[str, Any]:
    underlying = payload.get("underlying") or {}
    underlyings = [
        {
            "symbol": underlying.get("symbol") or "PDD",
            "display_name": underlying.get("symbol_name") or "PDD Holdings Inc.",
            "market": "NASDAQ",
            "asset_class": "equity",
            "last_price": to_float(underlying.get("last_price")),
            "bid_price": to_float(underlying.get("bid_price")),
            "ask_price": to_float(underlying.get("ask_price")),
            "price_change": to_float(underlying.get("price_change")),
            "percent_change": to_float(underlying.get("percent_change")),
            "volume": to_int(underlying.get("volume")),
            "open_interest": to_int(underlying.get("open_interest")),
            "trade_time": underlying.get("trade_time"),
            "trade_time_epoch": to_int(underlying.get("trade_time_epoch")),
        }
    ]

    expiries = []
    for chain in payload.get("chains") or []:
        expiries.append(
            {
                "underlying_symbol": chain.get("base_symbol"),
                "chain_symbol": chain.get("base_symbol"),
                "label": chain.get("expiration_date"),
                "chain_type": chain.get("expiration_type"),
                "expiry_date": chain.get("expiration_date"),
                "days_to_expiry": to_int(chain.get("days_to_expiry")),
                "page_url": chain.get("page_url"),
            }
        )

    contracts = []
    for row in payload.get("rows") or []:
        contracts.append(
            {
                "underlying_symbol": row.get("base_symbol"),
                "contract_symbol": row.get("symbol"),
                "display_symbol": row.get("symbol"),
                "chain_symbol": row.get("page_symbol"),
                "option_type": row.get("option_type"),
                "expiry_date": row.get("expiration_date"),
                "days_to_expiry": to_int(row.get("days_to_expiry")),
                "strike_price": to_float(row.get("strike_price")),
                "last_price": to_float(row.get("last_price")),
                "bid_price": to_float(row.get("bid_price")),
                "ask_price": to_float(row.get("ask_price")),
                "mid_price": to_float(row.get("mid_price")),
                "volume": to_int(row.get("volume")),
                "open_interest": to_int(row.get("open_interest")),
                "implied_volatility": normalize_iv(row.get("implied_volatility")),
                "delta": to_float(row.get("delta")),
                "premium": to_float(row.get("last_price")),
                "price_change": to_float(row.get("price_change")),
                "percent_change": to_float(row.get("percent_change")),
                "moneyness_pct": to_float(row.get("moneyness")),
                "moneyness_bucket": None,
                "cash_secured_put_annualized_pct": compute_premium_put_annualized_pct(
                    row.get("option_type"),
                    row.get("last_price"),
                    row.get("strike_price"),
                    row.get("days_to_expiry"),
                ),
                "trade_time": row.get("trade_time"),
                "trade_time_epoch": to_int(row.get("trade_time_epoch")),
                "contract_multiplier": 100,
                "source_page_url": row.get("page_url"),
                "open_interest_change": to_int(row.get("open_interest_change")),
                "expiration_type": row.get("expiration_type"),
                "implied_volatility_rank_1y": to_float(row.get("implied_volatility_rank_1y")),
                "average_volatility": normalize_iv(row.get("average_volatility")),
                "historic_volatility_30d": normalize_iv(row.get("historic_volatility_30d")),
            }
        )

    return {
        "asset_code": "PDD",
        "display_name": "PDD Holdings Options",
        "market": "NASDAQ",
        "asset_class": "equity_options",
        "source_meta": build_source_meta(
            provider="Barchart",
            source_type="website_internal_api",
            delayed=True,
            retrieved_at=(payload.get("meta") or {}).get("generated_at_utc"),
            notes=["Barchart 美股期权链为延迟行情，适合原型与展示。"],
        ),
        "underlyings": underlyings,
        "expiries": expiries,
        "contracts": contracts,
        "summary": build_summary(contracts),
    }


def normalize_tencent_payload(
    metrics_df: pd.DataFrame,
    spot_price: float,
    update_time: str,
) -> dict[str, Any]:
    underlyings = [
        {
            "symbol": "HK.00700",
            "display_name": "Tencent Holdings",
            "market": "HKEX",
            "asset_class": "equity",
            "last_price": to_float(spot_price),
            "bid_price": None,
            "ask_price": None,
            "price_change": None,
            "percent_change": None,
            "volume": None,
            "open_interest": None,
            "trade_time": update_time,
            "trade_time_epoch": None,
        }
    ]

    expiries = []
    contracts = []
    for _, row in metrics_df.iterrows():
        expiry_date = clean_value(row.get("expiry_date"))
        if expiry_date and expiry_date not in {item["expiry_date"] for item in expiries}:
            expiries.append(
                {
                    "underlying_symbol": "HK.00700",
                    "chain_symbol": "HK.00700",
                    "label": expiry_date,
                    "chain_type": "stock_options",
                    "expiry_date": expiry_date,
                    "days_to_expiry": to_int(row.get("days_to_expiry")),
                    "page_url": None,
                }
            )
        contracts.append(
            {
                "underlying_symbol": "HK.00700",
                "contract_symbol": clean_value(row.get("option_code")),
                "display_symbol": clean_value(row.get("option_name")),
                "chain_symbol": "HK.00700",
                "option_type": clean_value(row.get("option_type")),
                "expiry_date": expiry_date,
                "days_to_expiry": to_int(row.get("days_to_expiry")),
                "strike_price": to_float(row.get("strike_price")),
                "last_price": to_float(row.get("last_price")),
                "bid_price": to_float(row.get("bid_price")),
                "ask_price": to_float(row.get("ask_price")),
                "mid_price": None,
                "volume": to_int(row.get("volume")),
                "open_interest": to_int(row.get("open_interest")),
                "implied_volatility": normalize_iv(row.get("implied_volatility")),
                "delta": None,
                "premium": to_float(row.get("premium")),
                "price_change": None,
                "percent_change": None,
                "moneyness_pct": to_float(row.get("strike_distance_pct")),
                "moneyness_bucket": clean_value(row.get("moneyness_bucket")),
                "cash_secured_put_annualized_pct": to_float(row.get("cash_secured_put_annualized_pct")),
                "trade_time": clean_value(row.get("quote_time")),
                "trade_time_epoch": None,
                "contract_multiplier": to_int(row.get("contract_multiplier")),
                "source_page_url": None,
                "cash_secured_put_net_profit": to_float(row.get("cash_secured_put_net_profit")),
            }
        )

    return {
        "asset_code": "TENCENT",
        "display_name": "Tencent Options",
        "market": "HKEX",
        "asset_class": "equity_options",
        "source_meta": build_source_meta(
            provider="HKEX",
            source_type="website_scrape",
            delayed=True,
            retrieved_at=update_time,
            notes=["腾讯港股期权链来自 HKEX 公共网页，腾讯现价来自 Yahoo Finance 公共行情。"],
        ),
        "underlyings": underlyings,
        "expiries": sorted(expiries, key=lambda item: item["expiry_date"]),
        "contracts": contracts,
        "summary": build_summary(contracts),
    }
