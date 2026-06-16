#!/usr/bin/env python3
"""
Multibagger finder — list stocks that ran 5x within the trailing 3 years.

LEARNING / RESEARCH TOOL, not a buy screener. The goal is to collect study
material: which stocks multiplied, what sector they were in, how fast the run
was, and whether they are still trending up or already faded. It does NOT feed
the forward scanner/sizer pipeline (mixing past winners into the buy logic
invites overfitting).

Method (peak run within the window, per the user's spec "3年間のどこかで5倍到達"):
  window  = last `--years` of split-adjusted closes
  peak    = max close in window;  trough = min close BEFORE that peak
  run_multiple = peak / trough  -> HIT if >= `--multiple` (default 5.0)
Also reports current_multiple (close_now / trough) and drawdown from peak.

Data: universe + metadata from moomoo OpenD (per user's moomoo-API requirement);
3-year price history from Yahoo Finance (free, no historical-K-line quota), the
same hybrid the rest of the system uses. Delisted/merged names are not on Yahoo
and are skipped, which naturally limits results to currently-existing companies.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from moomoo_openapi_screener import (  # noqa: E402
    enum_value,
    fetch_daily_bars_yahoo,
    fetch_plate_info,
    fetch_snapshots,
    field_value,
    import_openapi,
    make_simple_filter,
    safe_float,
    stock_attr,
)

# 現在の時価総額floor（各市場の現地通貨）と売買代金floor
MARKET_FLOOR = {
    "US": {"cap": 300_000_000, "turnover": 5_000_000},
    "JP": {"cap": 30_000_000_000, "turnover": 300_000_000},
    "HK": {"cap": 2_000_000_000, "turnover": 10_000_000},
}
CACHE_TTL_SEC = 24 * 3600


@dataclass
class Hit:
    market: str
    symbol: str
    name: str
    industry: str
    themes: str
    run_multiple: float
    current_multiple: float
    drawdown_from_peak_pct: float
    date_trough: str
    price_trough: float
    date_peak: str
    price_peak: float
    days_trough_to_peak: int
    price_now: float
    still_uptrend: int
    market_cap_now: float


def sma(closes: list[float], window: int) -> float:
    if len(closes) < window:
        return 0.0
    return sum(closes[-window:]) / window


def compute_run(bars: list[dict], years: int) -> dict | None:
    """Returns run metrics for the trailing `years` window, or None if history insufficient."""
    if len(bars) < 60:
        return None
    cutoff = (dt.date.today() - dt.timedelta(days=int(years * 365.25))).isoformat()
    # 最古バーが3年前より新しい = 上場3年未満 → 履歴不足
    if bars[0]["date"] > cutoff:
        return None
    window = [b for b in bars if b["date"] >= cutoff]
    if len(window) < 30:
        return None
    closes = [b["close"] for b in window]
    # データ異常ガード: 分割未調整/不良ティックを除外（学習用に偽マルチバガーを排除）
    #  - 単日で>2.5倍に跳ねる = 逆分割等の未調整
    #  - 谷が中央値の5%未満 = ゼロ近傍の不良バー（19,000,000倍の正体）
    med = sorted(closes)[len(closes) // 2]
    for a, b in zip(closes, closes[1:]):
        if a > 0 and b / a > 2.5:
            return None
    if med > 0 and min(closes) < 0.05 * med:
        return None
    peak_idx = max(range(len(closes)), key=lambda i: closes[i])
    pre = closes[: peak_idx + 1]
    trough = min(pre)
    if trough <= 0:
        return None
    trough_idx = pre.index(trough)
    peak = closes[peak_idx]
    now = closes[-1]
    full_closes = [b["close"] for b in bars]
    sma200 = sma(full_closes, 200)
    return {
        "run_multiple": peak / trough,
        "current_multiple": now / trough,
        "drawdown_from_peak_pct": (now / peak - 1) * 100,
        "date_trough": window[trough_idx]["date"],
        "price_trough": trough,
        "date_peak": window[peak_idx]["date"],
        "price_peak": peak,
        "days_trough_to_peak": (
            dt.date.fromisoformat(window[peak_idx]["date"])
            - dt.date.fromisoformat(window[trough_idx]["date"])
        ).days,
        "price_now": now,
        "still_uptrend": 1 if (sma200 > 0 and now > sma200) else 0,
    }


def cached_bars(symbol: str, cache_dir: Path, range_str: str) -> list[dict]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cf = cache_dir / f"{symbol.replace('.', '_')}.json"
    if cf.exists() and (time.time() - cf.stat().st_mtime) < CACHE_TTL_SEC:
        try:
            return json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            pass
    bars = fetch_daily_bars_yahoo(symbol, range_str=range_str)
    cf.write_text(json.dumps(bars), encoding="utf-8")
    return bars


def enumerate_universe(api, quote_ctx, market: str, floor: dict, page_size: int, page_sleep: float, max_rows: int | None) -> list[dict]:
    filters = {
        "cap": make_simple_filter(api, ("MARKET_VAL",), floor["cap"], None),
        "price": make_simple_filter(api, ("CUR_PRICE",), 1, None),
    }
    rows: list[dict] = []
    begin = 0
    api_market = enum_value(api.Market, market)
    while True:
        ret, payload = quote_ctx.get_stock_filter(
            market=api_market, filter_list=list(filters.values()), begin=begin, num=page_size
        )
        if ret != api.RET_OK:
            # get_stock_filter は 30秒で10回まで。頻度超過なら待って再試行。
            if "high frequency" in str(payload).lower() or "频" in str(payload):
                print(f"  {market}: 頻度制限 → 31秒待機して再試行", file=sys.stderr)
                time.sleep(31)
                continue
            raise RuntimeError(f"get_stock_filter failed for {market}: {payload}")
        last_page, _all, stock_list = payload
        for item in stock_list:
            rows.append(
                {
                    "symbol": stock_attr(item, "stock_code"),
                    "name": stock_attr(item, "stock_name"),
                    "market_cap": field_value(item, filters["cap"]),
                }
            )
            if max_rows and len(rows) >= max_rows:
                break
        if (max_rows and len(rows) >= max_rows) or last_page or not stock_list:
            break
        begin += len(stock_list)
        time.sleep(page_sleep)
    return rows


def write_hits(hits: list[Hit], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(hits[0]).keys()) if hits else [f.name for f in Hit.__dataclass_fields__.values()]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for h in sorted(hits, key=lambda x: -x.run_multiple):
            row = asdict(h)
            for k in ("run_multiple", "current_multiple"):
                row[k] = f"{row[k]:.2f}"
            row["drawdown_from_peak_pct"] = f"{row['drawdown_from_peak_pct']:.1f}"
            for k in ("price_trough", "price_peak", "price_now"):
                row[k] = f"{row[k]:.2f}"
            row["market_cap_now"] = f"{row['market_cap_now']:.0f}"
            w.writerow(row)


def write_summary(hits: list[Hit], path: Path, years: int, multiple: float) -> None:
    from collections import Counter

    lines = [f"# 過去{years}年 {multiple:g}倍株（マルチバガー）抽出結果\n"]
    by_mkt = Counter(h.market for h in hits)
    lines.append(f"ヒット総数: {len(hits)}（" + " / ".join(f"{m} {by_mkt[m]}" for m in ('US', 'JP', 'HK') if by_mkt[m]) + "）\n")
    alive = sum(1 for h in hits if h.still_uptrend)
    faded = len(hits) - alive
    lines.append(f"## 生存バイアスの注意")
    lines.append(f"今も上昇トレンド: {alive}件 ／ 暴騰後に崩れた(200日線割れ): {faded}件")
    lines.append("「過去5倍」は結果論。多くは既に減速。チャートの右端を追わず、上がる前の形を学ぶこと。\n")
    lines.append("## 倍率トップ20")
    lines.append("| 倍率 | 銘柄 | 名前 | 業種 | 現在倍率 | ピーク比 | 今も上昇 |")
    lines.append("|---|---|---|---|---|---|---|")
    for h in sorted(hits, key=lambda x: -x.run_multiple)[:20]:
        lines.append(
            f"| {h.run_multiple:.1f}x | {h.symbol} | {h.name[:18]} | {h.industry[:14]} | "
            f"{h.current_multiple:.1f}x | {h.drawdown_from_peak_pct:.0f}% | {'○' if h.still_uptrend else '×'} |"
        )
    lines.append("\n## 業種別ヒット分布（学習の入口）")
    for ind, n in Counter(h.industry or "未取得" for h in hits).most_common(15):
        lines.append(f"- {ind}: {n}")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="過去N年でM倍になった銘柄を抽出（学習用、買い判定には非接続）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=11111)
    p.add_argument("--markets", default="US,JP,HK")
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--multiple", type=float, default=5.0, help="ヒット閾値（×5＝5.0）")
    p.add_argument("--symbols", default="", help="検証用: カンマ区切りの銘柄を直接判定（宇宙列挙をスキップ）")
    p.add_argument("--max-per-market", type=int, default=0, help="各市場の上限（0=全件）")
    p.add_argument("--symbol-sleep", type=float, default=0.15)
    p.add_argument("--page-sleep", type=float, default=3.5, help="get_stock_filterは30秒10回制限のため3秒以上")
    p.add_argument("--page-size", type=int, default=200)
    p.add_argument("--cache-dir", type=Path, default=Path("outputs/cache_yahoo_hist"))
    p.add_argument("--out", type=Path, default=Path("outputs/multibaggers_3y_5x.csv"))
    p.add_argument("--summary", type=Path, default=Path("outputs/multibaggers_summary.md"))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    range_str = "5y" if args.years <= 4 else "10y"
    api = import_openapi()
    quote_ctx = api.OpenQuoteContext(host=args.host, port=args.port)

    hits: list[Hit] = []
    try:
        for market in markets:
            floor = MARKET_FLOOR.get(market, {"cap": 0, "turnover": 0})
            if args.symbols:
                universe = [
                    {"symbol": s.strip(), "name": "", "market_cap": 0.0}
                    for s in args.symbols.split(",")
                    if s.strip().upper().startswith(f"{market}.")
                ]
                if not universe:
                    continue
            else:
                try:
                    universe = enumerate_universe(
                        api, quote_ctx, market, floor, args.page_size, args.page_sleep,
                        args.max_per_market or None,
                    )
                    # 流動性floor（スナップショットの売買代金）でさらに絞る
                    snaps = fetch_snapshots(api, quote_ctx, [r["symbol"] for r in universe], batch_size=100)
                    universe = [r for r in universe if safe_float(snaps.get(r["symbol"], {}).get("turnover")) >= floor["turnover"]]
                except (RuntimeError, SystemExit) as exc:
                    print(f"{market}: 宇宙列挙に失敗、他市場は継続 ({exc})", file=sys.stderr)
                    continue
            print(f"{market}: {len(universe)}銘柄を判定", file=sys.stderr)

            market_hits: list[dict] = []
            for i, r in enumerate(universe):
                sym = r["symbol"]
                try:
                    bars = cached_bars(sym, args.cache_dir, range_str)
                except Exception as exc:
                    print(f"  skip {sym}: {exc}", file=sys.stderr)
                    time.sleep(args.symbol_sleep)
                    continue
                run = compute_run(bars, args.years)
                time.sleep(args.symbol_sleep)
                if run is None or run["run_multiple"] < args.multiple:
                    continue
                market_hits.append({"r": r, "run": run})
                if (i + 1) % 200 == 0:
                    print(f"  {market}: {i+1}/{len(universe)} 走査, ヒット{len(market_hits)}", file=sys.stderr)

            # ヒットのみメタ取得（業種・現在時価総額・名前）
            if market_hits:
                codes = [m["r"]["symbol"] for m in market_hits]
                plates = fetch_plate_info(api, quote_ctx, codes)
                snaps2 = fetch_snapshots(api, quote_ctx, codes, batch_size=100)
                for m in market_hits:
                    sym = m["r"]["symbol"]
                    run = m["run"]
                    snap = snaps2.get(sym, {})
                    hits.append(Hit(
                        market=market, symbol=sym,
                        name=str(snap.get("name") or m["r"]["name"]),
                        industry=plates.get(sym, {}).get("industry", "未取得"),
                        themes=plates.get(sym, {}).get("themes", "未取得"),
                        run_multiple=run["run_multiple"],
                        current_multiple=run["current_multiple"],
                        drawdown_from_peak_pct=run["drawdown_from_peak_pct"],
                        date_trough=run["date_trough"], price_trough=run["price_trough"],
                        date_peak=run["date_peak"], price_peak=run["price_peak"],
                        days_trough_to_peak=run["days_trough_to_peak"],
                        price_now=run["price_now"], still_uptrend=run["still_uptrend"],
                        market_cap_now=safe_float(snap.get("total_market_val")) or m["r"]["market_cap"],
                    ))
                write_hits(hits, args.out)  # 逐次保存
            print(f"{market}: ヒット{len(market_hits)}件", file=sys.stderr)
    finally:
        quote_ctx.close()

    write_hits(hits, args.out)
    write_summary(hits, args.summary, args.years, args.multiple)
    print(f"\n総ヒット {len(hits)}件 → {args.out}")
    print(f"概要 → {args.summary}")
    for h in sorted(hits, key=lambda x: -x.run_multiple)[:15]:
        print(f"  {h.run_multiple:5.1f}x {h.symbol:<10} {h.name[:20]:<20} {h.industry[:12]:<12} 現在{h.current_multiple:.1f}x {'生存' if h.still_uptrend else '減速'}")


if __name__ == "__main__":
    main()
