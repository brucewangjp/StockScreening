#!/usr/bin/env python3
"""
Position sizer: converts scanner ALERTs into concrete order suggestions.

Risk rules applied in order:
  1. Regime gate: red light or an event inside 48h -> no new entries for
     that market. Yellow light halves the risk budget.
  2. ATR-based sizing: risk per trade = account * risk_pct (default 1%).
     Stop distance = min(stop_atr_mult * ATR, max_stop_pct of entry).
     Shares = risk budget / stop distance per share (JP rounded to 100-share
     lots, US/HK to whole shares).
  3. Position cap: market value <= max_position_pct of account.
  4. Theme concentration: AI/semiconductor-themed candidates are blocked or
     trimmed so existing + new exposure stays under theme_cap_pct.
  5. Earnings proximity: if an earnings date is provided and falls within
     2 days, the entry is deferred. Without data the plan says 要手動確認.

This tool outputs suggestions only. Orders are always placed manually.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

AI_THEME_KEYWORDS_SUBSTRING = (
    "半導体", "semiconductor", "人工知能", "チップ",
    "データセンター", "data center", "datacenter", "クラウド",
)
AI_THEME_KEYWORDS_WORD = ("ai", "chip", "gpu")

CURRENCY_BY_MARKET = {"US": "USD", "JP": "JPY", "HK": "HKD"}
LOT_SIZE_BY_MARKET = {"US": 1, "JP": 100, "HK": 100}


@dataclass
class SizingConfig:
    account_value_jpy: float
    risk_pct: float
    max_position_pct: float
    theme_cap_pct: float
    stop_atr_mult: float
    max_stop_pct: float
    fx_usdjpy: float
    fx_hkdjpy: float


def parse_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    cleaned = value.strip().replace(",", "").replace("%", "")
    if not cleaned:
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_regime(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"レジームJSONが見つかりません: {path}\n先に market_regime.py を実行してください。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def read_fred_cache_last(cache_dir: Path, series_id: str) -> float:
    cache_file = cache_dir / f"fred_{series_id}.csv"
    if not cache_file.exists():
        return 0.0
    last = 0.0
    with cache_file.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 2 and row[1].strip() not in {".", ""}:
                try:
                    last = float(row[1])
                except ValueError:
                    continue
    return last


def load_fx_from_cache(cache_dir: Path) -> tuple[float, float]:
    """(USDJPY, HKDJPY) from the regime engine's FRED cache.
    DEXJPUS = yen per dollar, DEXHKUS = HKD per dollar -> HKDJPY = USDJPY / USDHKD."""
    usdjpy = read_fred_cache_last(cache_dir, "DEXJPUS")
    usdhkd = read_fred_cache_last(cache_dir, "DEXHKUS")
    hkdjpy = usdjpy / usdhkd if usdjpy > 0 and usdhkd > 0 else 0.0
    return usdjpy, hkdjpy


def load_earnings(path: Path | None) -> dict[str, dt.date]:
    if path is None or not path.exists():
        return {}
    result = {}
    for row in read_rows(path):
        symbol = row.get("symbol", "").strip()
        raw = row.get("earnings_date", "").strip()
        if not symbol or not raw:
            continue
        try:
            result[symbol] = dt.date.fromisoformat(raw)
        except ValueError:
            continue
    return result


def is_ai_theme(row: dict[str, str]) -> bool:
    text = f"{row.get('industry', '')} {row.get('themes', '')} {row.get('name', '')}".lower()
    if any(keyword in text for keyword in AI_THEME_KEYWORDS_SUBSTRING):
        return True
    return any(re.search(rf"\b{keyword}\b", text) for keyword in AI_THEME_KEYWORDS_WORD)


def theme_exposure_jpy(portfolio: list[dict[str, str]], theme: str) -> float:
    return sum(
        parse_float(row.get("value_jpy"))
        for row in portfolio
        if row.get("theme", "").strip() == theme
    )


def fx_rate(market: str, config: SizingConfig) -> float:
    currency = CURRENCY_BY_MARKET.get(market.upper(), "JPY")
    if currency == "USD":
        return config.fx_usdjpy
    if currency == "HKD":
        return config.fx_hkdjpy
    return 1.0


def size_candidate(
    row: dict[str, str],
    config: SizingConfig,
    regime: dict,
    portfolio: list[dict[str, str]],
    planned_ai_jpy: float,
    earnings: dict[str, dt.date],
    today: dt.date,
) -> dict[str, str]:
    symbol = row["symbol"]
    market = row.get("market", "").upper()
    price = parse_float(row.get("price"))
    atr_pct = parse_float(row.get("atr14_pct"))
    score = row.get("score", "")
    notes: list[str] = []

    result = {
        "symbol": symbol,
        "market": market,
        "name": row.get("name", ""),
        "score": score,
        "entry_price": f"{price:.2f}",
        "stop_price": "",
        "shares": "0",
        "position_value_jpy": "0",
        "position_pct": "0.0",
        "risk_jpy": "0",
        "theme_group": "AI/半導体" if is_ai_theme(row) else "その他",
        "earnings_check": "",
        "decision": "",
        "notes": "",
    }

    market_regime = regime.get("markets", {}).get(market)
    if market_regime is None:
        result["decision"] = "見送り"
        result["notes"] = f"レジーム判定なし ({market})。market_regime.py を --markets で実行"
        return result

    exposure = float(market_regime.get("exposure_multiplier", 0.0))
    if exposure <= 0:
        result["decision"] = "見送り"
        result["notes"] = "レジーム赤灯: 新規エントリー停止中"
        return result
    if market_regime.get("no_new_entries"):
        events = "; ".join(market_regime.get("blocked_events", []))
        result["decision"] = "延期"
        result["notes"] = f"重要イベント48時間ルール: {events}"
        return result
    if exposure < 1.0:
        notes.append(f"黄灯のためリスク予算{exposure:.0%}")

    earnings_date = earnings.get(symbol)
    if earnings_date is not None:
        days_until = (earnings_date - today).days
        if 0 <= days_until <= 2:
            result["decision"] = "延期"
            result["earnings_check"] = earnings_date.isoformat()
            result["notes"] = f"決算発表まで{days_until}日: 突破直後の決算跨ぎは禁止"
            return result
        result["earnings_check"] = earnings_date.isoformat()
    else:
        result["earnings_check"] = "要手動確認"
        notes.append("決算日未登録: 発注前に必ず確認")

    fx = fx_rate(market, config)
    if fx <= 0:
        result["decision"] = "見送り"
        result["notes"] = f"為替レート未設定 ({CURRENCY_BY_MARKET.get(market, '?')})"
        return result

    if price <= 0:
        result["decision"] = "見送り"
        result["notes"] = "価格データなし"
        return result

    if atr_pct > 0:
        stop_distance = min(price * atr_pct / 100 * config.stop_atr_mult, price * config.max_stop_pct / 100)
    else:
        stop_distance = price * config.max_stop_pct / 100
        notes.append("ATR未取得のため最大ストップ幅で計算 (サイズ過小の可能性)")
    stop_price = price - stop_distance

    risk_budget_jpy = config.account_value_jpy * config.risk_pct / 100 * exposure
    shares_raw = risk_budget_jpy / (stop_distance * fx)
    lot = int(parse_float(row.get("lot_size"))) or LOT_SIZE_BY_MARKET.get(market, 1)
    shares = math.floor(shares_raw / lot) * lot

    max_value_jpy = config.account_value_jpy * config.max_position_pct / 100
    if shares * price * fx > max_value_jpy:
        shares = math.floor(max_value_jpy / (price * fx) / lot) * lot
        notes.append(f"単一銘柄上限{config.max_position_pct:.0f}%で減額")

    if result["theme_group"] == "AI/半導体":
        theme_cap_jpy = config.account_value_jpy * config.theme_cap_pct / 100
        current = theme_exposure_jpy(portfolio, "AI") + planned_ai_jpy
        headroom = theme_cap_jpy - current
        if headroom <= 0:
            result["decision"] = "見送り"
            result["notes"] = (
                f"テーマ集中度: AI/半導体が既に{current / config.account_value_jpy * 100:.0f}%で"
                f"上限{config.theme_cap_pct:.0f}%超過。既存ポジション縮小が先"
            )
            return result
        if shares * price * fx > headroom:
            shares = math.floor(headroom / (price * fx) / lot) * lot
            notes.append(f"テーマ上限{config.theme_cap_pct:.0f}%に合わせて減額")

    if shares <= 0:
        result["decision"] = "見送り"
        result["notes"] = "; ".join(notes + ["計算結果が最小単元未満"])
        return result

    position_value = shares * price * fx
    result["stop_price"] = f"{stop_price:.2f}"
    result["shares"] = str(shares)
    result["position_value_jpy"] = f"{position_value:.0f}"
    result["position_pct"] = f"{position_value / config.account_value_jpy * 100:.1f}"
    result["risk_jpy"] = f"{shares * stop_distance * fx:.0f}"
    result["decision"] = "執行候補" if not any("減額" in note for note in notes) else "執行候補(減額済)"
    result["notes"] = "; ".join(notes)
    return result


def write_plan(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "symbol", "market", "name", "score", "entry_price", "stop_price",
        "shares", "position_value_jpy", "position_pct", "risk_jpy",
        "theme_group", "earnings_check", "decision", "notes",
    ]
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ALERT銘柄をATRベースのポジションサイズに変換します。")
    parser.add_argument("candidates_csv", type=Path, help="position_trend_scanner.py の出力CSV")
    parser.add_argument("--regime-json", type=Path, default=Path("outputs/market_regime.json"))
    parser.add_argument("--portfolio-csv", type=Path, default=Path("outputs/my_portfolio.csv"))
    parser.add_argument("--earnings-csv", type=Path, help="symbol,earnings_date 形式のCSV")
    parser.add_argument("--account-value", type=float, default=0.0, help="口座全体の評価額(円)。0なら portfolio 合計+現金")
    parser.add_argument("--cash", type=float, default=0.0, help="現金(円)。account-value未指定時に加算")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="1取引の最大リスク (口座比%)")
    parser.add_argument("--max-position-pct", type=float, default=10.0, help="単一銘柄の上限 (口座比%)")
    parser.add_argument("--theme-cap-pct", type=float, default=40.0, help="AI/半導体テーマ合計の上限 (口座比%)")
    parser.add_argument("--stop-atr-mult", type=float, default=2.0, help="ストップ幅 = ATR x この倍率")
    parser.add_argument("--max-stop-pct", type=float, default=15.0, help="ストップ幅の上限 (%)")
    parser.add_argument("--fx-usdjpy", type=float, default=0.0, help="USDJPY。0ならレジームキャッシュから取得")
    parser.add_argument("--fx-hkdjpy", type=float, default=0.0, help="HKDJPY。香港株を使う場合は必須")
    parser.add_argument("--cache-dir", type=Path, default=Path("outputs/cache"))
    parser.add_argument("-o", "--output", type=Path, default=Path("outputs/position_plan.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidates = read_rows(args.candidates_csv)
    regime = load_regime(args.regime_json)

    portfolio: list[dict[str, str]] = []
    if args.portfolio_csv.exists():
        portfolio = read_rows(args.portfolio_csv)
    else:
        print(f"ポートフォリオCSV無し ({args.portfolio_csv}): テーマ集中度チェックは新規分のみで計算")

    account_value = args.account_value
    if account_value <= 0:
        account_value = sum(parse_float(row.get("value_jpy")) for row in portfolio) + args.cash
    if account_value <= 0:
        raise SystemExit("--account-value か portfolio CSV のどちらかで口座評価額を指定してください。")

    cached_usdjpy, cached_hkdjpy = load_fx_from_cache(args.cache_dir)
    fx_usdjpy = args.fx_usdjpy or cached_usdjpy
    fx_hkdjpy = args.fx_hkdjpy or cached_hkdjpy
    if fx_usdjpy <= 0:
        print("USDJPY未取得: 米国株候補はスキップされます (--fx-usdjpy で指定可)")
    if fx_hkdjpy <= 0 and any(row.get("market", "").upper() == "HK" for row in candidates):
        print("HKDJPY未取得: 香港株候補はスキップされます (market_regime.py --markets HK を先に実行)")

    config = SizingConfig(
        account_value_jpy=account_value,
        risk_pct=args.risk_pct,
        max_position_pct=args.max_position_pct,
        theme_cap_pct=args.theme_cap_pct,
        stop_atr_mult=args.stop_atr_mult,
        max_stop_pct=args.max_stop_pct,
        fx_usdjpy=fx_usdjpy,
        fx_hkdjpy=fx_hkdjpy,
    )
    earnings = load_earnings(args.earnings_csv)
    today = dt.date.today()

    alerts = [row for row in candidates if row.get("status") == "ALERT"]
    watch_count = sum(1 for row in candidates if row.get("status") in {"WATCH", "SETUP"})
    alerts.sort(key=lambda row: -parse_float(row.get("score")))

    plan: list[dict[str, str]] = []
    planned_ai_jpy = 0.0
    for row in alerts:
        sized = size_candidate(row, config, regime, portfolio, planned_ai_jpy, earnings, today)
        if sized["theme_group"] == "AI/半導体" and sized["decision"].startswith("執行"):
            planned_ai_jpy += parse_float(sized["position_value_jpy"])
        plan.append(sized)

    write_plan(plan, args.output)

    ai_existing = theme_exposure_jpy(portfolio, "AI")
    print(f"口座評価額: ¥{account_value:,.0f} / AI・半導体テーマ既存: ¥{ai_existing:,.0f} ({ai_existing / account_value * 100:.0f}%)")
    print(f"ALERT {len(alerts)}件を判定 (WATCH/SETUP {watch_count}件は監視のみ)\n")
    for row in plan:
        print(
            f"{row['decision']:<10} {row['symbol']:<10} score={row['score']:>3} "
            f"株数={row['shares']:>6} 金額=¥{parse_float(row['position_value_jpy']):,.0f} "
            f"({row['position_pct']}%) 損切り={row['stop_price']} リスク=¥{parse_float(row['risk_jpy']):,.0f}"
        )
        if row["notes"]:
            print(f"           └ {row['notes']}")
    print(f"\n発注プランCSV: {args.output}")
    print("注意: これは提案であり自動発注はしない。発注前に決算日・ニュース・板を必ず確認すること。")


if __name__ == "__main__":
    main()
