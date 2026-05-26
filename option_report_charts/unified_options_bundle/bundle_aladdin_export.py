from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_INPUT = "latest_options_bundle_light.json"
DEFAULT_OUTPUT = "latest_options_bundle_aladdin.json"
DEFAULT_CHUNK_DIR = "aladdin_chunks"
DEFAULT_CHUNK_CHARS = 180000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成适合阿拉丁复制粘贴的压缩数据与分片文本。")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="输入轻量版 JSON。")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="输出给阿拉丁的压缩 JSON。")
    parser.add_argument("--chunk-dir", default=DEFAULT_CHUNK_DIR, help="分片文本输出目录。")
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="每个分片最大字符数。")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def compact_contracts(asset: dict[str, Any]) -> dict[str, Any]:
    contracts = asset.get("contracts") or []
    if not contracts:
        return {
            **asset,
            "contracts_columns": [],
            "contracts_rows": [],
            "contracts": [],
        }

    columns = list(contracts[0].keys())
    rows = [[contract.get(column) for column in columns] for contract in contracts]

    compact_asset = dict(asset)
    compact_asset["contracts_columns"] = columns
    compact_asset["contracts_rows"] = rows
    compact_asset.pop("contracts", None)
    return compact_asset


def build_compact_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    assets = [compact_contracts(asset) for asset in bundle.get("assets") or []]
    meta = dict(bundle.get("meta") or {})
    meta["aladdin_compact"] = True
    meta["contracts_encoding"] = "contracts_columns + contracts_rows"
    return {
        "meta": meta,
        "source_statuses": bundle.get("source_statuses") or [],
        "assets": assets,
    }


def chunk_text(text: str, max_chars: int) -> list[str]:
    return [text[index : index + max_chars] for index in range(0, len(text), max_chars)]


def build_chunk_prompt(part_index: int, total_parts: int, chunk: str) -> str:
    return (
        f"下面是期权数据分片 {part_index}/{total_parts}。\n"
        "请先仅接收和记住，不要开始生成网页，不要解释，也不要调用任何工具。\n"
        "等我把所有分片都发完，再统一生成单文件 HTML。\n\n"
        f"[ALADDIN_DATA_PART_{part_index}_BEGIN]\n"
        f"{chunk}\n"
        f"[ALADDIN_DATA_PART_{part_index}_END]\n"
    )


def build_final_instruction() -> str:
    return """所有数据分片已经发送完成。

不要调用任何函数、工具、cron、schedule、job、workflow。
不要执行、不要部署、不要创建任务。
你现在只输出一个单文件 HTML 页面源码。

数据说明：
1. 这是已过滤后的完整 Put 数据
2. 行权价只保留在现价上下 15%
3. 每个 asset 中：
   - underlyings: 标的信息
   - available_expiry_dates: 到期日选择器候选项
   - summary: 汇总信息
   - contracts_columns + contracts_rows: 完整合约明细
   - contracts_preview: 首页默认重点展示

页面要求：
1. 顶部显示整体更新时间和数据源状态
2. 分 3 张卡片展示腾讯、GC、PDD
3. 每张卡片展示标的价格、来源、是否延迟、更新时间、到期日概览、Top Put 排名
4. 每张卡片都要有“到期日选择框”
5. contracts 明细表不要依赖 preview，要使用 contracts_columns + contracts_rows 还原完整表格
6. 默认先显示 contracts_preview，再允许按到期日切换完整明细
7. 只返回完整 HTML 源码，不要解释
"""


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    chunk_dir = Path(args.chunk_dir)

    bundle = load_json(input_path)
    compact_bundle = build_compact_bundle(bundle)
    compact_json = json.dumps(compact_bundle, ensure_ascii=False, separators=(",", ":"))

    write_text(output_path, json.dumps(compact_bundle, ensure_ascii=False, indent=2))

    chunks = chunk_text(compact_json, args.chunk_chars)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    for idx, chunk in enumerate(chunks, start=1):
        write_text(chunk_dir / f"aladdin_data_part_{idx:02d}.txt", build_chunk_prompt(idx, len(chunks), chunk))

    write_text(chunk_dir / "aladdin_final_instruction.txt", build_final_instruction())

    print(f"已输出阿拉丁压缩 JSON 到 {output_path}")
    print(f"已输出 {len(chunks)} 个分片到 {chunk_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
