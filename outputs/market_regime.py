#!/usr/bin/env python3
"""
Market regime engine: top-down exposure throttle for the position pipeline.

Three layers, strictly separated from stock selection:
  1. Market layer (daily, per market): index trend, breadth proxy, VIX,
     high-yield credit spread -> green / yellow / red.
  2. Slow macro layer (weekly, global): initial jobless claims, Sahm rule,
     yield-curve un-inversion, core PCE trend. Veto power only -- a slow
     warning can demote the light but can never promote it.
  3. Event calendar: FOMC / CPI / NFP / BOJ dates -> "no new entries" flag
     for the next 48 hours. NFP dates are computed (first Friday); other
     dates load from a CSV you must verify against official calendars.

Output: outputs/market_regime.json plus a console summary. This engine
controls position sizing permission only. It never changes stock scores.

Data sources (free, no API key):
  - FRED fredgraph.csv endpoint (S&P500, Nikkei, VIX, HY OAS, ICSA, Sahm,
    T10Y2Y, core PCE, USD/JPY)
  - Yahoo Finance chart API (RSP equal-weight ETF as US breadth proxy)
Every fetch is cached under outputs/cache/; on network failure the cache
is used and the result is marked stale.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d"

EXPOSURE = {"green": 1.0, "yellow": 0.5, "red": 0.0}
LIGHT_ORDER = {"green": 0, "yellow": 1, "red": 2}


@dataclass
class Series:
    name: str
    dates: list[dt.date]
    values: list[float]
    stale: bool = False

    @property
    def last(self) -> float:
        return self.values[-1]

    @property
    def last_date(self) -> dt.date:
        return self.dates[-1]


@dataclass
class Check:
    label: str
    passed: bool
    detail: str


@dataclass
class MarketRegime:
    market: str
    light: str
    score: int
    max_score: int
    exposure_multiplier: float
    checks: list[Check] = field(default_factory=list)
    slow_warnings: list[str] = field(default_factory=list)
    blocked_events: list[str] = field(default_factory=list)
    data_warnings: list[str] = field(default_factory=list)


def fetch_text(
    url: str, cache_path: Path, offline: bool, timeout: float = 30.0, retries: int = 2
) -> tuple[str, bool]:
    """Returns (text, stale). stale=True means served from cache after a failure."""
    if not offline:
        for attempt in range(retries + 1):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (regime-engine)"})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    text = response.read().decode("utf-8", errors="replace")
                if text.strip():
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(text, encoding="utf-8")
                    return text, False
            except Exception as exc:
                if attempt < retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                print(f"取得失敗 ({url.split('?')[0]}): {exc} -> キャッシュを使用")
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8"), True
    raise RuntimeError(f"データ取得不可でキャッシュも無し: {url}")


def parse_two_column_csv(text: str, value_column: int = 1) -> tuple[list[dt.date], list[float]]:
    reader = csv.reader(io.StringIO(text))
    header_skipped = False
    dates: list[dt.date] = []
    values: list[float] = []
    for row in reader:
        if not header_skipped:
            header_skipped = True
            continue
        if len(row) <= value_column:
            continue
        raw = row[value_column].strip()
        if raw in {".", ""}:
            continue
        try:
            dates.append(dt.date.fromisoformat(row[0].strip()))
            values.append(float(raw))
        except ValueError:
            continue
    return dates, values


def fetch_fred(series_id: str, start: dt.date, cache_dir: Path, offline: bool) -> Series:
    url = FRED_URL.format(series=series_id, start=start.isoformat())
    text, stale = fetch_text(url, cache_dir / f"fred_{series_id}.csv", offline)
    dates, values = parse_two_column_csv(text)
    if not values:
        raise RuntimeError(f"FRED {series_id}: 有効データなし")
    return Series(name=series_id, dates=dates, values=values, stale=stale)


def fetch_yahoo_close(symbol: str, cache_dir: Path, offline: bool) -> Series:
    url = YAHOO_URL.format(symbol=symbol)
    text, stale = fetch_text(url, cache_dir / f"yahoo_{symbol}.json", offline)
    try:
        result = json.loads(text)["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Yahoo {symbol}: 応答形式が不正 ({exc})") from exc
    dates: list[dt.date] = []
    values: list[float] = []
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        dates.append(dt.date.fromtimestamp(timestamp))
        values.append(float(close))
    if not values:
        raise RuntimeError(f"Yahoo {symbol}: 有効データなし")
    return Series(name=symbol, dates=dates, values=values, stale=stale)


def sma(values: list[float], window: int, end_offset: int = 0) -> float:
    end = len(values) - end_offset
    start = end - window
    if start < 0:
        return 0.0
    sample = values[start:end]
    return sum(sample) / len(sample)


def trend_checks(series: Series, label: str) -> list[Check]:
    sma200 = sma(series.values, 200)
    sma200_prev = sma(series.values, 200, end_offset=20)
    checks = [
        Check(
            label=f"{label}が200日線上",
            passed=sma200 > 0 and series.last > sma200,
            detail=f"終値 {series.last:,.0f} / 200日線 {sma200:,.0f}",
        ),
        Check(
            label=f"{label}の200日線が上向き",
            passed=sma200 > sma200_prev > 0,
            detail=f"現在 {sma200:,.0f} / 20営業日前 {sma200_prev:,.0f}",
        ),
    ]
    return checks


def vix_check(vix: Series) -> Check:
    return Check(
        label="VIXが25未満",
        passed=vix.last < 25,
        detail=f"VIX {vix.last:.1f}",
    )


def credit_checks(hy_oas: Series) -> list[Check]:
    month_ago = hy_oas.values[-22] if len(hy_oas.values) >= 22 else hy_oas.last
    widening = hy_oas.last - month_ago
    return [
        Check(
            label="HY利差が400bp未満",
            passed=hy_oas.last < 4.0,
            detail=f"HY OAS {hy_oas.last * 100:.0f}bp",
        ),
        Check(
            label="HY利差が急拡大していない",
            passed=widening < 0.5,
            detail=f"1ヶ月変化 {widening * 100:+.0f}bp",
        ),
    ]


def breadth_proxy_check(rsp: Series) -> Check:
    sma200 = sma(rsp.values, 200)
    return Check(
        label="等ウェイトETF(RSP)が200日線上 (広度代理)",
        passed=sma200 > 0 and rsp.last > sma200,
        detail=f"RSP {rsp.last:.1f} / 200日線 {sma200:.1f}",
    )


def yen_stability_check(usdjpy: Series) -> Check:
    month_ago = usdjpy.values[-20] if len(usdjpy.values) >= 20 else usdjpy.last
    appreciation_pct = (month_ago - usdjpy.last) / month_ago * 100 if month_ago > 0 else 0.0
    return Check(
        label="円急騰なし (20営業日で5%未満)",
        passed=appreciation_pct < 5.0,
        detail=f"USDJPY {usdjpy.last:.1f}, 円高方向 {appreciation_pct:+.1f}%",
    )


def yuan_stability_check(usdcny: Series) -> Check:
    month_ago = usdcny.values[-20] if len(usdcny.values) >= 20 else usdcny.last
    depreciation_pct = (usdcny.last - month_ago) / month_ago * 100 if month_ago > 0 else 0.0
    return Check(
        label="人民元急落なし (20営業日で2%未満)",
        passed=depreciation_pct < 2.0,
        detail=f"USDCNY {usdcny.last:.2f}, 元安方向 {depreciation_pct:+.1f}%",
    )


def slow_macro_warnings(
    icsa: Series, sahm: Series, curve: Series, core_pce: Series
) -> list[str]:
    warnings: list[str] = []

    if len(icsa.values) >= 56:
        ma4 = [sma(icsa.values[: i + 1], 4) for i in range(len(icsa.values) - 52, len(icsa.values))]
        low = min(ma4)
        current = ma4[-1]
        if low > 0 and current > low * 1.15:
            warnings.append(
                f"新規失業保険申請が52週底から+{(current / low - 1) * 100:.0f}% (雇用悪化の先行サイン)"
            )

    if sahm.last >= 0.5:
        warnings.append(f"Sahmルール {sahm.last:.2f} (景気後退入りの可能性が高い)")

    if len(curve.values) >= 380:
        recent = curve.values[-380:]
        was_inverted = min(recent) < -0.2
        if was_inverted and curve.last > 0.2:
            warnings.append(
                f"逆イールド解消 (10Y-2Y {curve.last:+.2f}%) は歴史的に景気後退の直前シグナル"
            )

    if len(core_pce.values) >= 16:
        yoy_now = (core_pce.values[-1] / core_pce.values[-13] - 1) * 100
        yoy_3m_ago = (core_pce.values[-4] / core_pce.values[-16] - 1) * 100
        if yoy_now > 3.0 and yoy_now > yoy_3m_ago + 0.1:
            warnings.append(
                f"コアPCE {yoy_now:.1f}%で再加速中 (利上げ圧力 -> グロース株逆風)"
            )

    return warnings


def first_fridays(start: dt.date, months: int) -> list[dt.date]:
    results = []
    year, month = start.year, start.month
    for _ in range(months):
        day = dt.date(year, month, 1)
        offset = (4 - day.weekday()) % 7
        results.append(day + dt.timedelta(days=offset))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return results


def load_event_calendar(path: Path, today: dt.date) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    event_date = dt.date.fromisoformat(row["date"].strip())
                except (KeyError, ValueError):
                    continue
                events.append(
                    {
                        "date": event_date.isoformat(),
                        "market": row.get("market", "ALL").strip().upper() or "ALL",
                        "event": row.get("event", "").strip(),
                    }
                )
    else:
        print(f"イベントカレンダー無し: {path} (FOMC/CPI/日銀の日付を必ず登録してください)")

    for nfp_date in first_fridays(today, 12):
        events.append({"date": nfp_date.isoformat(), "market": "US", "event": "米雇用統計 (自動計算)"})
    return events


def blocked_events_for(market: str, events: list[dict[str, str]], today: dt.date) -> list[str]:
    blocked = []
    for event in events:
        event_date = dt.date.fromisoformat(event["date"])
        days_until = (event_date - today).days
        if 0 <= days_until <= 1 and event["market"] in {market, "ALL"}:
            blocked.append(f"{event['date']} {event['event']}")
    return blocked


def upcoming_events(events: list[dict[str, str]], today: dt.date, horizon_days: int = 7) -> list[dict[str, str]]:
    soon = [
        event
        for event in events
        if 0 <= (dt.date.fromisoformat(event["date"]) - today).days <= horizon_days
    ]
    return sorted(soon, key=lambda event: event["date"])


def decide_light(checks: list[Check]) -> tuple[str, int]:
    failures = sum(1 for check in checks if not check.passed)
    if failures == 0:
        light = "green"
    elif failures <= 2:
        light = "yellow"
    else:
        light = "red"
    return light, len(checks) - failures


def demote(light: str, levels: int) -> str:
    lights = ["green", "yellow", "red"]
    index = min(len(lights) - 1, LIGHT_ORDER[light] + levels)
    return lights[index]


def build_regime(
    market: str,
    checks: list[Check],
    slow_warnings: list[str],
    events: list[dict[str, str]],
    today: dt.date,
    data_warnings: list[str],
) -> MarketRegime:
    light, score = decide_light(checks)
    if slow_warnings:
        demoted = demote(light, 1 if len(slow_warnings) == 1 else 2)
        if demoted != light:
            light = demoted
    blocked = blocked_events_for(market, events, today)
    exposure = EXPOSURE[light]
    return MarketRegime(
        market=market,
        light=light,
        score=score,
        max_score=len(checks),
        exposure_multiplier=exposure,
        checks=checks,
        slow_warnings=slow_warnings,
        blocked_events=blocked,
        data_warnings=data_warnings,
    )


def regime_to_dict(regime: MarketRegime) -> dict:
    return {
        "light": regime.light,
        "score": f"{regime.score}/{regime.max_score}",
        "exposure_multiplier": regime.exposure_multiplier,
        "no_new_entries": bool(regime.blocked_events),
        "blocked_events": regime.blocked_events,
        "checks": [
            {"label": check.label, "passed": check.passed, "detail": check.detail}
            for check in regime.checks
        ],
        "slow_warnings": regime.slow_warnings,
        "data_warnings": regime.data_warnings,
    }


def print_summary(regimes: list[MarketRegime], events_soon: list[dict[str, str]]) -> None:
    light_ja = {"green": "緑 (通常運用)", "yellow": "黄 (新規半分・ALERT上位のみ)", "red": "赤 (新規停止・損切り厳格化)"}
    for regime in regimes:
        print(f"\n=== {regime.market}: {light_ja[regime.light]}  スコア {regime.score}/{regime.max_score} ===")
        for check in regime.checks:
            mark = "OK " if check.passed else "NG "
            print(f"  {mark}{check.label}: {check.detail}")
        for warning in regime.slow_warnings:
            print(f"  警告(スロー系): {warning}")
        for blocked in regime.blocked_events:
            print(f"  新規禁止(48時間ルール): {blocked}")
        for warning in regime.data_warnings:
            print(f"  データ注意: {warning}")
    if events_soon:
        print("\n--- 今後7日のイベント ---")
        for event in events_soon:
            print(f"  {event['date']} [{event['market']}] {event['event']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="マクロ・市場レジーム判定 (ポジション許可エンジン)")
    parser.add_argument("--markets", default="US,JP", help="Comma-separated: US,JP")
    parser.add_argument("--output", type=Path, default=Path("outputs/market_regime.json"))
    parser.add_argument("--calendar", type=Path, default=Path("outputs/macro_event_calendar.csv"))
    parser.add_argument("--cache-dir", type=Path, default=Path("outputs/cache"))
    parser.add_argument("--offline", action="store_true", help="ネット接続せずキャッシュのみ使用")
    parser.add_argument("--lookback-years", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    markets = [market.strip().upper() for market in args.markets.split(",") if market.strip()]
    today = dt.date.today()
    start = today - dt.timedelta(days=args.lookback_years * 365)
    cache = args.cache_dir
    offline = args.offline

    def load(series_id: str) -> Series:
        return fetch_fred(series_id, start, cache, offline)

    vix = load("VIXCLS")
    hy_oas = load("BAMLH0A0HYM2")
    icsa = load("ICSA")
    sahm = load("SAHMREALTIME")
    curve = load("T10Y2Y")
    core_pce = load("PCEPILFE")

    slow = slow_macro_warnings(icsa, sahm, curve, core_pce)
    events = load_event_calendar(args.calendar, today)

    regimes: list[MarketRegime] = []
    for market in markets:
        data_warnings = []
        for series in (vix, hy_oas, icsa, sahm, curve, core_pce):
            if series.stale:
                data_warnings.append(f"{series.name} はキャッシュ ({series.last_date}) を使用")
        if market == "US":
            spx = load("SP500")
            checks = trend_checks(spx, "S&P500")
            try:
                rsp = fetch_yahoo_close("RSP", cache, offline)
                checks.append(breadth_proxy_check(rsp))
                if rsp.stale:
                    data_warnings.append(f"RSP はキャッシュ ({rsp.last_date}) を使用")
            except RuntimeError as exc:
                data_warnings.append(f"広度代理(RSP)を取得できず判定から除外: {exc}")
            checks.append(vix_check(vix))
            checks.extend(credit_checks(hy_oas))
            if spx.stale:
                data_warnings.append(f"SP500 はキャッシュ ({spx.last_date}) を使用")
        elif market == "JP":
            nikkei = load("NIKKEI225")
            usdjpy = load("DEXJPUS")
            checks = trend_checks(nikkei, "日経平均")
            checks.append(yen_stability_check(usdjpy))
            checks.append(vix_check(vix))
            checks.extend(credit_checks(hy_oas))
            if nikkei.stale:
                data_warnings.append(f"NIKKEI225 はキャッシュ ({nikkei.last_date}) を使用")
        elif market == "HK":
            hsi = fetch_yahoo_close("^HSI", cache, offline)
            usdcny = load("DEXCHUS")
            load("DEXHKUS")  # position_sizer がキャッシュから HKDJPY を算出するために必要
            load("DEXJPUS")
            checks = trend_checks(hsi, "ハンセン指数")
            checks.append(yuan_stability_check(usdcny))
            checks.append(vix_check(vix))
            checks.extend(credit_checks(hy_oas))
            if hsi.stale:
                data_warnings.append(f"ハンセン指数はキャッシュ ({hsi.last_date}) を使用")
        else:
            raise SystemExit(f"Unsupported market: {market}")
        regimes.append(build_regime(market, checks, slow, events, today, data_warnings))

    events_soon = upcoming_events(events, today)
    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "markets": {regime.market: regime_to_dict(regime) for regime in regimes},
        "slow_macro_warnings": slow,
        "events_next_7d": events_soon,
        "note": "このエンジンはポジションサイズの許可のみを制御する。銘柄スコアには一切影響させないこと。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(regimes, events_soon)
    print(f"\nレジームJSON: {args.output}")
    print("注意: FOMC/CPI/日銀の日付は四半期ごとに公式カレンダーと照合してください。")


if __name__ == "__main__":
    main()
