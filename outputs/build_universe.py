#!/usr/bin/env python3
"""
Build a backtest universe — a return-neutral list of currently-tradeable stocks.

Purpose: give the backtester an UNBIASED symbol set. The multibagger list is
selected by future performance (survivorship + look-ahead), so backtesting on it
flatters the strategy. This builder instead enumerates the universe purely by
*current* market-cap and liquidity floors — it does NOT look at returns — so the
"picked the winners" bias is removed.

Residual bias (honest disclosure): delisted/merged companies are absent from the
current universe and from Yahoo, so survivorship bias remains. Free data cannot
fix this. Read backtest results on this universe as "of the names that survived,
how did the strategy do" — still far better than testing on known multibaggers.

Universe definition (per market, configurable):
  current market cap >= floor  AND  current turnover >= floor
  optionally capped at --max-per-market largest by market cap.

Output: outputs/backtest_universe.csv (symbol,market,name,market_cap,turnover),
ready to feed `moomoo_backtest_runner.py --symbols-csv`.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from moomoo_openapi_screener import fetch_snapshots, import_openapi, safe_float  # noqa: E402
from multibagger_finder import MARKET_FLOOR, enumerate_universe  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="回測用の無バイアス(リターン非依存)ユニバースを構築")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=11111)
    p.add_argument("--markets", default="US,JP,HK")
    p.add_argument("--max-per-market", type=int, default=500, help="各市場の上限（超過分はランダム抽出、0=全件）")
    p.add_argument("--seed", type=int, default=42, help="ランダム抽出の乱数シード（再現性）")
    p.add_argument("--page-sleep", type=float, default=3.5, help="get_stock_filterは30秒10回制限")
    p.add_argument("--page-size", type=int, default=200)
    p.add_argument("--out", type=Path, default=Path("outputs/backtest_universe.csv"))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    api = import_openapi()
    quote_ctx = api.OpenQuoteContext(host=args.host, port=args.port)
    rows: list[dict] = []
    try:
        for market in markets:
            floor = MARKET_FLOOR.get(market, {"cap": 0, "turnover": 0})
            try:
                universe = enumerate_universe(
                    api, quote_ctx, market, floor, args.page_size, args.page_sleep, None
                )
            except (RuntimeError, SystemExit) as exc:
                print(f"{market}: 列挙失敗、他市場は継続 ({exc})", file=sys.stderr)
                continue
            # 上限超過分はランダム抽出（時価総額上位だとメガキャップに偏り、スクリーナーが
            # 狙う中小型の突破銘柄が母集団から漏れる＝非代表的になるため）。
            # リターンでは選ばないので勝者バイアスは入らない。シードで再現性を確保。
            # ※スナップショットは使わない（数千銘柄の取得はレート制限地獄）。time_cap≥floor で
            #   十分tradeableなので、get_stock_filter が返す時価総額・名前のみで構築する。
            if args.max_per_market and len(universe) > args.max_per_market:
                universe = random.Random(args.seed).sample(universe, args.max_per_market)
            enriched = [
                {"symbol": r["symbol"], "market": market, "name": r["name"],
                 "market_cap": r["market_cap"], "turnover": 0.0}
                for r in universe
            ]
            enriched.sort(key=lambda x: -x["market_cap"])  # 出力は規模順で見やすく
            rows.extend(enriched)
            print(f"{market}: ユニバース {len(enriched)}銘柄", file=sys.stderr)
    finally:
        quote_ctx.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "market", "name", "market_cap", "turnover"])
        w.writeheader()
        for r in rows:
            w.writerow({**r, "market_cap": f"{r['market_cap']:.0f}", "turnover": f"{r['turnover']:.0f}"})
    print(f"\nユニバース {len(rows)}銘柄 → {args.out}")
    print("注意: 現存・上場中の銘柄のみ。上場廃止組は含まれない（生存バイアスは残る）。")
    print("これを回測へ: moomoo_backtest_runner.py --strategy position --bars-source yahoo \\")
    print(f"  --symbols-csv {args.out} --start <3-4年前> --end <直近> --benchmark ^GSPC --split-date <中間>")


if __name__ == "__main__":
    main()
