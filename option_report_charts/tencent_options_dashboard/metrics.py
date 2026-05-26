from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


ANNUAL_DAYS = 365.0


@dataclass(frozen=True)
class BucketRule:
    label: str
    lower: float | None = None
    upper: float | None = None


BUCKET_RULES: tuple[BucketRule, ...] = (
    BucketRule("<=-20%", None, -0.20),
    BucketRule("-20%~-10%", -0.20, -0.10),
    BucketRule("-10%~-5%", -0.10, -0.05),
    BucketRule("-5%~+5%", -0.05, 0.05),
    BucketRule("+5%~+10%", 0.05, 0.10),
    BucketRule("+10%~+20%", 0.10, 0.20),
    BucketRule(">=+20%", 0.20, None),
)


def bucket_for_pct(distance_pct: float) -> str:
    for rule in BUCKET_RULES:
        lower_ok = rule.lower is None or distance_pct >= rule.lower
        upper_ok = rule.upper is None or distance_pct < rule.upper
        if lower_ok and upper_ok:
            return rule.label
    return "other"


def safe_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def build_metrics_frame(
    raw_df: pd.DataFrame,
    spot_price: float,
    cost_basis: float,
    expected_expiry_price: float,
) -> pd.DataFrame:
    if raw_df.empty:
        return raw_df.copy()

    df = raw_df.copy()
    df["last_price"] = safe_numeric(df["last_price"])
    df["bid_price"] = safe_numeric(df["bid_price"])
    df["ask_price"] = safe_numeric(df["ask_price"])
    df["volume"] = safe_numeric(df["volume"])
    df["open_interest"] = safe_numeric(df["open_interest"])
    df["days_to_expiry"] = safe_numeric(df["days_to_expiry"]).clip(lower=1)
    df["contract_multiplier"] = safe_numeric(df["contract_multiplier"]).clip(lower=1)

    df["spot_price"] = spot_price
    df["cost_basis"] = float(cost_basis)
    df["expected_expiry_price"] = float(expected_expiry_price)
    df["premium"] = df["last_price"]
    df["spread"] = (df["ask_price"] - df["bid_price"]).clip(lower=0)
    df["spread_pct"] = (df["spread"] / df["last_price"].replace(0, pd.NA)).fillna(0.0)
    df["strike_distance_pct"] = (df["strike_price"] / spot_price) - 1.0
    df["moneyness_bucket"] = df["strike_distance_pct"].map(bucket_for_pct)

    multiplier = df["contract_multiplier"]
    premium_cash = df["premium"] * multiplier

    covered_call_stock_pnl = (pd.concat([df["expected_expiry_price"], df["strike_price"]], axis=1).min(axis=1) - df["cost_basis"]) * multiplier
    cash_secured_put_assignment_pnl = (df["expected_expiry_price"] - df["strike_price"]).clip(upper=0) * multiplier

    df["covered_call_net_profit"] = premium_cash + covered_call_stock_pnl
    df["cash_secured_put_net_profit"] = premium_cash + cash_secured_put_assignment_pnl

    df["covered_call_annualized"] = (
        df["covered_call_net_profit"] / (df["cost_basis"] * multiplier).replace(0, pd.NA) * ANNUAL_DAYS / df["days_to_expiry"]
    ).fillna(0.0)
    df["cash_secured_put_annualized"] = (
        df["cash_secured_put_net_profit"] / (df["strike_price"] * multiplier).replace(0, pd.NA) * ANNUAL_DAYS / df["days_to_expiry"]
    ).fillna(0.0)

    volume_rank = df["volume"].rank(pct=True, method="average")
    oi_rank = df["open_interest"].rank(pct=True, method="average")
    spread_rank = (1 - df["spread_pct"].rank(pct=True, method="average")).clip(lower=0)
    df["liquidity_score"] = ((volume_rank * 0.45) + (oi_rank * 0.40) + (spread_rank * 0.15)) * 100
    df["liquidity_score"] = df["liquidity_score"].round(2)

    df["estimated_fee"] = 0.0
    df["covered_call_net_profit"] = df["covered_call_net_profit"].round(2)
    df["cash_secured_put_net_profit"] = df["cash_secured_put_net_profit"].round(2)
    df["covered_call_annualized_pct"] = (df["covered_call_annualized"] * 100).round(2)
    df["cash_secured_put_annualized_pct"] = (df["cash_secured_put_annualized"] * 100).round(2)
    df["strike_distance_pct_display"] = (df["strike_distance_pct"] * 100).round(2)

    return df


def build_decision_summary(df: pd.DataFrame, top_n: int = 3) -> dict[str, object]:
    if df.empty:
        return {
            "best_iv_expiry": None,
            "best_put_ideas": [],
        }

    put_df = df[df["option_type"] == "PUT"].copy()
    if put_df.empty:
        return {
            "best_iv_expiry": None,
            "best_put_ideas": [],
        }

    expiry_df = (
        put_df.groupby("expiry_date", as_index=False)
        .agg(
            avg_iv=("implied_volatility", "mean"),
            total_volume=("volume", "sum"),
            total_open_interest=("open_interest", "sum"),
            avg_premium=("premium", "mean"),
        )
        .sort_values(["total_open_interest", "total_volume", "avg_iv"], ascending=[False, False, False])
    )
    put_candidates = (
        put_df
        .sort_values(
            ["cash_secured_put_annualized_pct", "open_interest", "volume", "implied_volatility"],
            ascending=[False, False, False, False],
        )
        .head(top_n)
    )

    return {
        "best_iv_expiry": expiry_df.iloc[0].to_dict() if not expiry_df.empty else None,
        "best_put_ideas": put_candidates[
            [
                "expiry_date",
                "strike_price",
                "premium",
                "cash_secured_put_annualized_pct",
                "implied_volatility",
                "volume",
                "open_interest",
                "moneyness_bucket",
                "option_code",
            ]
        ].to_dict(orient="records"),
    }


def summarize_top_items(df: pd.DataFrame, top_n: int = 5) -> dict[str, list[dict] | dict[str, float | str]]:
    if df.empty:
        return {
            "best_expiries": [],
            "best_buckets": [],
            "top_cash_secured_puts": [],
        }

    put_df = df[df["option_type"] == "PUT"].copy()
    expiry_df = (
        put_df.groupby("expiry_date", as_index=False)
        .agg(
            avg_iv=("implied_volatility", "mean"),
            total_volume=("volume", "sum"),
            total_open_interest=("open_interest", "sum"),
        )
        .sort_values(["total_open_interest", "total_volume", "avg_iv"], ascending=False)
    )

    bucket_df = (
        put_df.groupby("moneyness_bucket", as_index=False)
        .agg(
            avg_iv=("implied_volatility", "mean"),
            total_volume=("volume", "sum"),
            total_open_interest=("open_interest", "sum"),
        )
        .sort_values(["total_open_interest", "total_volume", "avg_iv"], ascending=False)
    )
    put_df = (
        put_df.sort_values(["cash_secured_put_annualized_pct", "open_interest", "volume", "implied_volatility"], ascending=False)
        .head(top_n)
    )

    return {
        "best_expiries": expiry_df.head(top_n).to_dict(orient="records"),
        "best_buckets": bucket_df.head(top_n).to_dict(orient="records"),
        "top_cash_secured_puts": put_df[
            [
                "option_code",
                "expiry_date",
                "strike_price",
                "cash_secured_put_net_profit",
                "cash_secured_put_annualized_pct",
                "implied_volatility",
            ]
        ].to_dict(orient="records"),
    }


def filter_records(
    df: pd.DataFrame,
    expiry_filter: str = "全部",
    option_type: str = "全部",
    bucket_filter: str = "全部",
) -> pd.DataFrame:
    filtered = df.copy()
    if expiry_filter != "全部":
        filtered = filtered[filtered["expiry_date"] == expiry_filter]
    if option_type != "全部":
        filtered = filtered[filtered["option_type"] == option_type]
    if bucket_filter != "全部":
        filtered = filtered[filtered["moneyness_bucket"] == bucket_filter]
    return filtered.reset_index(drop=True)


def bucket_options() -> list[str]:
    return ["全部", *[rule.label for rule in BUCKET_RULES]]
