from __future__ import annotations

import argparse
import json
from copy import deepcopy
import sys
from pathlib import Path
from typing import Any


CURRENT_DIR = Path(__file__).resolve().parent
OPTION_REPORTS_DIR = CURRENT_DIR.parent
GOLD_EXPORTER_DIR = OPTION_REPORTS_DIR / "gold_futures_options_barchart"
TENCENT_DIR = OPTION_REPORTS_DIR / "tencent_options_dashboard"

for extra_path in (GOLD_EXPORTER_DIR, TENCENT_DIR):
    if str(extra_path) not in sys.path:
        sys.path.append(str(extra_path))

from barchart_gold_options_export import collect_gold_options_payload  # noqa: E402
from barchart_pdd_options_export import collect_pdd_options_payload  # noqa: E402
from metrics import build_metrics_frame  # noqa: E402
from options_bundle_schema import (  # noqa: E402
    build_source_meta,
    normalize_gold_payload,
    normalize_pdd_payload,
    normalize_tencent_payload,
    now_utc_iso,
)
from tencent_hkex_options_export import collect_tencent_hkex_dashboard_data  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出腾讯、GC、PDD 的统一期权 JSON。")
    parser.add_argument("--json-out", default="latest_options_bundle.json", help="输出 JSON 路径。")
    parser.add_argument("--skip-tencent", action="store_true", help="跳过腾讯数据。")
    parser.add_argument("--skip-gold", action="store_true", help="跳过 GC 数据。")
    parser.add_argument("--skip-pdd", action="store_true", help="跳过 PDD 数据。")
    parser.add_argument("--gold-puts-only", action="store_true", help="GC 只导出 Put。")
    parser.add_argument("--pdd-puts-only", action="store_true", help="PDD 只导出 Put。")
    parser.add_argument("--gold-max-pages", type=int, default=0, help="GC 最多抓取多少个页面。")
    parser.add_argument("--pdd-max-expirations", type=int, default=0, help="PDD 最多抓取多少个到期日。")
    parser.add_argument("--tencent-max-expiries", type=int, default=0, help="腾讯最多抓取多少个到期日，0 表示全部。")
    parser.add_argument("--timeout", type=int, default=30, help="网页源请求超时秒数。")
    parser.add_argument("--tencent-cost-basis", type=float, default=0.0, help="腾讯持仓成本，默认使用现价。")
    parser.add_argument("--tencent-expected-price", type=float, default=0.0, help="腾讯预期到期价，默认使用现价。")
    return parser.parse_args()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def success_status(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_code": asset["asset_code"],
        "status": "ok",
        "message": "",
        "retrieved_at": asset["source_meta"].get("retrieved_at"),
        "contract_count": len(asset.get("contracts") or []),
        "provider": asset["source_meta"].get("provider"),
    }


def error_status(asset_code: str, provider: str, message: str) -> dict[str, Any]:
    return {
        "asset_code": asset_code,
        "status": "error",
        "message": message,
        "retrieved_at": None,
        "contract_count": 0,
        "provider": provider,
    }


def stale_status(asset: dict[str, Any], provider: str, message: str) -> dict[str, Any]:
    return {
        "asset_code": asset["asset_code"],
        "status": "stale",
        "message": message,
        "retrieved_at": asset.get("source_meta", {}).get("retrieved_at"),
        "contract_count": len(asset.get("contracts") or []),
        "provider": provider,
    }


def stale_asset(previous_asset: dict[str, Any], message: str) -> dict[str, Any]:
    asset = deepcopy(previous_asset)
    source_meta = dict(asset.get("source_meta") or {})
    source_meta["status"] = "stale"
    notes = list(source_meta.get("notes") or [])
    notes.append(f"本次更新失败，暂时保留上次成功抓取结果。失败原因: {message}")
    source_meta["notes"] = notes
    asset["source_meta"] = source_meta
    return asset


def previous_asset_map(bundle: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not bundle:
        return {}
    return {
        str(asset.get("asset_code")): asset
        for asset in (bundle.get("assets") or [])
        if asset.get("asset_code")
    }


def fetch_tencent_asset(cost_basis: float, expected_price: float, timeout: int, max_expiries: int) -> dict[str, Any]:
    dashboard = collect_tencent_hkex_dashboard_data(timeout=timeout, max_expiries=max_expiries)
    resolved_cost_basis = cost_basis if cost_basis > 0 else dashboard.spot_price
    resolved_expected_price = expected_price if expected_price > 0 else dashboard.spot_price
    metrics_df = build_metrics_frame(
        dashboard.table,
        spot_price=dashboard.spot_price,
        cost_basis=resolved_cost_basis,
        expected_expiry_price=resolved_expected_price,
    )
    return normalize_tencent_payload(metrics_df, dashboard.spot_price, dashboard.update_time)


def main() -> int:
    args = parse_args()
    output_path = Path(args.json_out)
    previous_bundle = load_json_if_exists(output_path)
    previous_assets = previous_asset_map(previous_bundle)
    assets: list[dict[str, Any]] = []
    source_statuses: list[dict[str, Any]] = []

    if not args.skip_tencent:
        try:
            asset = fetch_tencent_asset(
                cost_basis=args.tencent_cost_basis,
                expected_price=args.tencent_expected_price,
                timeout=args.timeout,
                max_expiries=args.tencent_max_expiries,
            )
            assets.append(asset)
            source_statuses.append(success_status(asset))
        except Exception as exc:
            previous_asset = previous_assets.get("TENCENT")
            if previous_asset:
                asset = stale_asset(previous_asset, str(exc))
                assets.append(asset)
                source_statuses.append(stale_status(asset, "HKEX", str(exc)))
            else:
                source_statuses.append(error_status("TENCENT", "HKEX", str(exc)))

    if not args.skip_gold:
        try:
            raw_gold = collect_gold_options_payload(
                puts_only=args.gold_puts_only,
                max_pages=args.gold_max_pages,
                timeout=args.timeout,
            )
            asset = normalize_gold_payload(raw_gold)
            assets.append(asset)
            source_statuses.append(success_status(asset))
        except Exception as exc:
            previous_asset = previous_assets.get("GC")
            if previous_asset:
                asset = stale_asset(previous_asset, str(exc))
                assets.append(asset)
                source_statuses.append(stale_status(asset, "Barchart", str(exc)))
            else:
                source_statuses.append(error_status("GC", "Barchart", str(exc)))

    if not args.skip_pdd:
        try:
            raw_pdd = collect_pdd_options_payload(
                puts_only=args.pdd_puts_only,
                max_expirations=args.pdd_max_expirations,
                timeout=args.timeout,
            )
            asset = normalize_pdd_payload(raw_pdd)
            assets.append(asset)
            source_statuses.append(success_status(asset))
        except Exception as exc:
            previous_asset = previous_assets.get("PDD")
            if previous_asset:
                asset = stale_asset(previous_asset, str(exc))
                assets.append(asset)
                source_statuses.append(stale_status(asset, "Barchart", str(exc)))
            else:
                source_statuses.append(error_status("PDD", "Barchart", str(exc)))

    bundle = {
        "meta": {
            "generated_at_utc": now_utc_iso(),
            "bundle_version": 1,
            "asset_count": len(assets),
            "successful_source_count": sum(1 for item in source_statuses if item["status"] == "ok"),
            "failed_source_count": sum(1 for item in source_statuses if item["status"] != "ok"),
            "notes": [
                "不同来源的数据实时性不同，请以各 asset.source_meta.delayed 与 retrieved_at 为准。",
                "网页源字段可能是延迟或混合时点快照，更适合展示、筛选和原型。",
            ],
        },
        "source_statuses": source_statuses,
        "assets": assets,
    }

    write_json(output_path, bundle)
    print(f"已输出统一 JSON 到 {args.json_out}")
    for item in source_statuses:
        print(f"{item['asset_code']}: {item['status']} ({item['contract_count']} 条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
