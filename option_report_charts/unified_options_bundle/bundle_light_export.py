from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_INPUT = "latest_options_bundle.json"
DEFAULT_OUTPUT = "latest_options_bundle_light.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将统一期权总包压缩为适合阿拉丁的轻量 JSON。")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="输入的完整 bundle JSON。")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="输出的轻量 bundle JSON。")
    parser.add_argument("--max-expiries", type=int, default=24, help="每个资产最多保留多少个到期日。")
    parser.add_argument("--max-preview-contracts", type=int, default=180, help="每个资产最多保留多少条预览合约。")
    parser.add_argument("--strike-band-pct", type=float, default=0.15, help="按现价上下多少比例筛选行权价，默认 0.15。")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clean_json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, dict):
        return {key: clean_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json_value(item) for item in value]
    return value


def pick_contract_fields(contract: dict[str, Any]) -> dict[str, Any]:
    fields = [
        "underlying_symbol",
        "contract_symbol",
        "display_symbol",
        "chain_symbol",
        "option_type",
        "expiry_date",
        "days_to_expiry",
        "strike_price",
        "last_price",
        "bid_price",
        "ask_price",
        "mid_price",
        "volume",
        "open_interest",
        "implied_volatility",
        "delta",
        "premium",
        "moneyness_pct",
        "moneyness_bucket",
        "cash_secured_put_annualized_pct",
        "trade_time",
        "contract_multiplier",
    ]
    return {field: contract.get(field) for field in fields if field in contract}


def contract_priority(contract: dict[str, Any]) -> tuple:
    open_interest = contract.get("open_interest") or 0
    volume = contract.get("volume") or 0
    annualized = contract.get("cash_secured_put_annualized_pct")
    annualized = annualized if annualized is not None else -10**9
    days = contract.get("days_to_expiry")
    days = days if days is not None else 10**9
    return (open_interest, volume, annualized, -days)


def build_underlying_price_map(asset: dict[str, Any]) -> dict[str, float]:
    mapping: dict[str, float] = {}
    for underlying in asset.get("underlyings") or []:
        symbol = underlying.get("symbol")
        last_price = to_float(underlying.get("last_price"))
        if symbol and last_price and last_price > 0:
            mapping[str(symbol)] = last_price
    return mapping


def filter_contracts(asset: dict[str, Any], strike_band_pct: float) -> list[dict[str, Any]]:
    contracts = asset.get("contracts") or []
    if not contracts:
        return []

    price_map = build_underlying_price_map(asset)
    filtered: list[dict[str, Any]] = []
    for contract in contracts:
        if str(contract.get("option_type", "")).upper() != "PUT":
            continue
        underlying_symbol = str(contract.get("underlying_symbol") or "")
        spot_price = price_map.get(underlying_symbol)
        strike_price = to_float(contract.get("strike_price"))
        if not spot_price or strike_price is None:
            continue
        lower = spot_price * (1 - strike_band_pct)
        upper = spot_price * (1 + strike_band_pct)
        if lower <= strike_price <= upper:
            filtered.append(contract)
    return filtered


def dedupe_contracts(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for contract in contracts:
        key = (
            contract.get("underlying_symbol"),
            contract.get("contract_symbol"),
            contract.get("expiry_date"),
            contract.get("strike_price"),
            contract.get("option_type"),
        )
        current = deduped.get(key)
        if current is None:
            deduped[key] = contract
            continue
        current_score = ((current.get("open_interest") or 0), (current.get("volume") or 0), (current.get("last_price") or 0))
        new_score = ((contract.get("open_interest") or 0), (contract.get("volume") or 0), (contract.get("last_price") or 0))
        if new_score > current_score:
            deduped[key] = contract
    return list(deduped.values())


def build_filtered_summary(filtered_contracts: list[dict[str, Any]]) -> dict[str, Any]:
    if not filtered_contracts:
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

    df = pd.DataFrame(filtered_contracts).copy()
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

    top_cols = [
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
    top_cols = [column for column in top_cols if column in df.columns]

    top_by_oi = df.sort_values(["open_interest", "volume"], ascending=False).head(10)[top_cols].to_dict(orient="records")
    top_by_volume = df.sort_values(["volume", "open_interest"], ascending=False).head(10)[top_cols].to_dict(orient="records")
    annualized_available = "cash_secured_put_annualized_pct" in df.columns and df["cash_secured_put_annualized_pct"].notna().any()
    top_by_annualized = (
        df.sort_values(["cash_secured_put_annualized_pct", "open_interest"], ascending=False).head(10)[top_cols].to_dict(orient="records")
        if annualized_available
        else []
    )

    return clean_json_value({
        "contract_count": len(df),
        "put_count": len(df),
        "call_count": 0,
        "expiry_count": int(df["expiry_date"].nunique(dropna=True)),
        "underlying_count": int(df["underlying_symbol"].nunique(dropna=True)),
        "top_expiries_by_open_interest": expiry_df.head(10).to_dict(orient="records"),
        "top_puts_by_open_interest": top_by_oi,
        "top_puts_by_volume": top_by_volume,
        "top_puts_by_annualized": top_by_annualized,
    })


def build_preview_contracts(filtered_contracts: list[dict[str, Any]], max_preview_contracts: int) -> list[dict[str, Any]]:
    if not filtered_contracts:
        return []

    sorted_contracts = sorted(filtered_contracts, key=contract_priority, reverse=True)
    preview = [pick_contract_fields(contract) for contract in sorted_contracts[:max_preview_contracts]]
    return preview


def slim_asset(asset: dict[str, Any], max_expiries: int, max_preview_contracts: int, strike_band_pct: float) -> dict[str, Any]:
    filtered_contracts = dedupe_contracts(filter_contracts(asset, strike_band_pct=strike_band_pct))
    filtered_expiry_dates = {contract.get("expiry_date") for contract in filtered_contracts}
    filtered_expiries = [item for item in (asset.get("expiries") or []) if item.get("expiry_date") in filtered_expiry_dates][:max_expiries]
    return {
        "asset_code": asset.get("asset_code"),
        "display_name": asset.get("display_name"),
        "market": asset.get("market"),
        "asset_class": asset.get("asset_class"),
        "source_meta": asset.get("source_meta"),
        "underlyings": asset.get("underlyings") or [],
        "expiries": filtered_expiries,
        "available_expiry_dates": [item.get("expiry_date") for item in filtered_expiries],
        "summary": build_filtered_summary(filtered_contracts),
        "contracts": [pick_contract_fields(contract) for contract in sorted(filtered_contracts, key=contract_priority, reverse=True)],
        "contracts_preview": build_preview_contracts(filtered_contracts, max_preview_contracts),
        "preview_note": (
            f"仅保留 Put，且行权价限制在现价上下 {int(strike_band_pct * 100)}% 区间内；"
            f"contracts 为筛选后的完整合约集，contracts_preview 为前 {max_preview_contracts} 条高优先级预览。"
        ),
    }


def build_light_bundle(bundle: dict[str, Any], max_expiries: int, max_preview_contracts: int, strike_band_pct: float) -> dict[str, Any]:
    assets = [
        slim_asset(
            asset,
            max_expiries=max_expiries,
            max_preview_contracts=max_preview_contracts,
            strike_band_pct=strike_band_pct,
        )
        for asset in bundle.get("assets") or []
    ]
    return {
        "meta": {
            **(bundle.get("meta") or {}),
            "lightweight": True,
            "intended_consumer": "Aladdin / lightweight web dashboard",
            "contracts_removed": False,
            "contracts_filtered_only": True,
            "preview_contract_limit_per_asset": max_preview_contracts,
            "expiry_limit_per_asset": max_expiries,
            "filters": {
                "option_type": "PUT",
                "strike_band_pct": strike_band_pct,
            },
        },
        "source_statuses": bundle.get("source_statuses") or [],
        "assets": assets,
    }


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    bundle = load_json(input_path)
    light_bundle = build_light_bundle(
        bundle,
        max_expiries=args.max_expiries,
        max_preview_contracts=args.max_preview_contracts,
        strike_band_pct=args.strike_band_pct,
    )
    write_json(output_path, light_bundle)
    print(f"已输出轻量 JSON 到 {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
