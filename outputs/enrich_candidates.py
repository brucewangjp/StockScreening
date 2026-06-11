#!/usr/bin/env python3
"""
Enrich runner candidate CSV files with moomoo basic info, industry, themes, and
Japanese trend comments.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Any


INDUSTRY_JA = {
    "Shell Companies": "シェルカンパニー/SPAC",
    "Capital Markets": "資本市場・金融サービス",
    "Pollution & Treatment Controls": "環境・汚染処理",
    "Real Estate": "不動産",
    "Auto Parts": "自動車部品",
    "Securities & Brokerage": "証券・ブローカー",
    "Gaming": "ゲーム・娯楽",
}


THEME_JA = {
    "Top Gainers Yesterday": "前日上昇率上位",
    "Hot Stock": "注目株",
    "Growth": "グロース市場",
    "IPOs": "IPO関連",
    "Chinese Concept": "中国関連株",
    "China Concept Stock Recent IPOs": "中国関連の直近IPO",
    "Stocks tradable only on moomoo": "moomoo取扱銘柄",
}


def import_openapi() -> Any:
    try:
        import moomoo as api  # type: ignore

        return api
    except ImportError as exc:
        raise SystemExit("moomoo-api is not installed. Run: python3 -m pip install -r outputs/requirements.txt") from exc


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_rows(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    base_fields = list(rows[0].keys()) if rows else []
    extra_fields = ["company_name", "exchange", "industry", "themes", "company_overview", "latest_trend"]
    fieldnames = base_fields + [field for field in extra_fields if field not in base_fields]
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def market_enum(api: Any, market: str) -> Any:
    market = market.upper()
    if market == "US":
        return api.Market.US
    if market == "JP":
        return api.Market.JP
    if market == "HK":
        return api.Market.HK
    raise ValueError(f"Unsupported market: {market}")


def fetch_basic_info(api: Any, quote_ctx: Any, markets: set[str]) -> dict[str, dict[str, Any]]:
    info: dict[str, dict[str, Any]] = {}
    for market in sorted(markets):
        ret, data = quote_ctx.get_stock_basicinfo(market=market_enum(api, market), stock_type=api.SecurityType.STOCK)
        if ret != api.RET_OK:
            print(f"{market}: basic info skipped: {data}")
            continue
        for row in data.to_dict("records"):
            info[str(row.get("code", ""))] = row
    return info


def fetch_plate_info(api: Any, quote_ctx: Any, symbols: list[str]) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"industry": [], "themes": []})
    for start in range(0, len(symbols), 50):
        batch = symbols[start : start + 50]
        ret, data = quote_ctx.get_owner_plate(batch)
        if ret != api.RET_OK:
            print(f"owner plate skipped for {batch}: {data}")
            continue
        for row in data.to_dict("records"):
            code = str(row.get("code", ""))
            plate_name = str(row.get("plate_name", ""))
            plate_type = str(row.get("plate_type", ""))
            if plate_type == "INDUSTRY":
                result[code]["industry"].append(plate_name)
            elif plate_type in {"CONCEPT", "OTHER"}:
                result[code]["themes"].append(plate_name)
    return result


def translate_items(items: list[str], mapping: dict[str, str]) -> str:
    if not items:
        return ""
    translated = []
    for item in items:
        translated.append(mapping.get(item, item))
    return " / ".join(dict.fromkeys(translated))


def trend_comment(row: dict[str, str]) -> str:
    change = row.get("change_pct", "")
    rv = row.get("relative_volume", "")
    high_distance = row.get("distance_to_52w_high_pct", "")
    gap = row.get("gap_pct", "")
    status = row.get("status", "")
    parts = [f"{status}判定"]
    if change:
        parts.append(f"当日上昇率 {change}%")
    if rv:
        parts.append(f"相対出来高 {rv}倍")
    if high_distance:
        parts.append(f"52週高値から {high_distance}%")
    if gap:
        parts.append(f"ギャップ {gap}%")
    return "、".join(parts)


def overview(name: str, industry: str, themes: str, exchange: str) -> str:
    subject = name or "当該企業"
    industry_part = industry or "業界分類未取得"
    if themes:
        return f"{subject}はmoomoo分類で「{industry_part}」に属し、関連テーマは「{themes}」。取引所区分は{exchange or '未取得'}。"
    return f"{subject}はmoomoo分類で「{industry_part}」に属する銘柄。取引所区分は{exchange or '未取得'}。"


def enrich(rows: list[dict[str, str]], basic: dict[str, dict[str, Any]], plates: dict[str, dict[str, list[str]]]) -> list[dict[str, str]]:
    for row in rows:
        symbol = row.get("symbol", "")
        basic_row = basic.get(symbol, {})
        plate_row = plates.get(symbol, {"industry": [], "themes": []})
        name = str(basic_row.get("name") or "")
        exchange = str(basic_row.get("exchange_type") or "")
        industry = translate_items(plate_row["industry"], INDUSTRY_JA)
        themes = translate_items(plate_row["themes"], THEME_JA)
        row["company_name"] = name
        row["exchange"] = exchange
        row["industry"] = industry or "未取得"
        row["themes"] = themes or "未取得"
        row["company_overview"] = overview(name, row["industry"], "" if themes == "未取得" else themes, exchange)
        row["latest_trend"] = trend_comment(row)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="候補CSVに業界・企業概要・最新トレンドを追加します。")
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = read_rows(args.input_csv)
    if not rows:
        write_rows(rows, args.output)
        print(f"No rows found: {args.input_csv}")
        return
    symbols = [row["symbol"] for row in rows]
    markets = {row["market"] for row in rows if row.get("market")}
    api = import_openapi()
    quote_ctx = api.OpenQuoteContext(host=args.host, port=args.port)
    try:
        basic = fetch_basic_info(api, quote_ctx, markets)
        plates = fetch_plate_info(api, quote_ctx, symbols)
    finally:
        quote_ctx.close()
    enriched = enrich(rows, basic, plates)
    write_rows(enriched, args.output)
    print(f"Enriched {len(enriched)} rows: {args.output}")


if __name__ == "__main__":
    main()
