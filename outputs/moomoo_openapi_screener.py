#!/usr/bin/env python3
"""
Fetch short-term runner candidates from moomoo OpenAPI/OpenD.

Prerequisites:
  1. Install moomoo/futu OpenAPI Python package.
  2. Start moomoo OpenD locally.
  3. Log in and make sure market data permissions are available.

This script creates the CSV expected by short_term_runner_scanner.py. It does
not place trades.
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT = Path("outputs/moomoo_runner_input.csv")


@dataclass(frozen=True)
class MarketPreset:
    api_market: str
    csv_market: str
    price_min: float
    price_max: float
    market_cap_min: float
    market_cap_max: float
    change_min: float
    volume_ratio_min: float
    high_distance_min: float


PRESETS = {
    "US": MarketPreset(
        api_market="US",
        csv_market="US",
        price_min=1,
        price_max=50,
        market_cap_min=50_000_000,
        market_cap_max=5_000_000_000,
        change_min=5,
        volume_ratio_min=1.8,
        high_distance_min=-15,
    ),
    "JP": MarketPreset(
        api_market="JP",
        csv_market="JP",
        price_min=100,
        price_max=5000,
        market_cap_min=5_000_000_000,
        market_cap_max=300_000_000_000,
        change_min=5,
        volume_ratio_min=1.8,
        high_distance_min=-15,
    ),
    "HK": MarketPreset(
        api_market="HK",
        csv_market="HK",
        price_min=0.5,
        price_max=100,
        market_cap_min=300_000_000,
        market_cap_max=30_000_000_000,
        change_min=5,
        volume_ratio_min=1.8,
        high_distance_min=-15,
    ),
}

POSITION_PRESETS = {
    "US": MarketPreset(
        api_market="US",
        csv_market="US",
        price_min=5,
        price_max=100_000,
        market_cap_min=300_000_000,
        market_cap_max=300_000_000_000,
        change_min=-100,
        volume_ratio_min=0,
        high_distance_min=-25,
    ),
    "JP": MarketPreset(
        api_market="JP",
        csv_market="JP",
        price_min=200,
        price_max=10_000_000,
        market_cap_min=10_000_000_000,
        market_cap_max=3_000_000_000_000,
        change_min=-100,
        volume_ratio_min=0,
        high_distance_min=-25,
    ),
    "HK": MarketPreset(
        api_market="HK",
        csv_market="HK",
        price_min=1,
        price_max=100_000,
        market_cap_min=1_000_000_000,
        market_cap_max=300_000_000_000,
        change_min=-100,
        volume_ratio_min=0,
        high_distance_min=-25,
    ),
}

BENCHMARKS = {"US": "US.SPY", "JP": "JP.1306", "HK": "HK.02800"}
# Yahoo経由では指数そのものを使う (ETFは分割の調整漏れリスクがあるため)
YAHOO_BENCHMARKS = {"US": "^GSPC", "JP": "^N225", "HK": "^HSI"}

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
    except ImportError:
        try:
            import futu as api  # type: ignore

            return api
        except ImportError as exc:
            raise SystemExit(
                "moomoo/futu OpenAPI package is not installed. Install the Python "
                "package from moomoo OpenAPI, start OpenD, then rerun this script."
            ) from exc


def enum_value(enum_cls: Any, *names: str) -> Any:
    for name in names:
        if hasattr(enum_cls, name):
            return getattr(enum_cls, name)
    readable = " or ".join(names)
    raise SystemExit(f"Your OpenAPI package does not expose {enum_cls}.{readable}.")


def make_simple_filter(api: Any, stock_fields: tuple[str, ...], minimum: float | None, maximum: float | None) -> Any:
    item = api.SimpleFilter()
    item.stock_field = enum_value(api.StockField, *stock_fields)
    item.is_no_filter = False
    if minimum is not None:
        item.filter_min = minimum
    if maximum is not None:
        item.filter_max = maximum
    return item


def field_value(item: Any, filter_obj: Any) -> float:
    try:
        value = item[filter_obj]
    except Exception:
        value = None
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def stock_attr(item: Any, name: str) -> str:
    value = getattr(item, name, "")
    return "" if value is None else str(value)


def safe_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return ((current - previous) / previous) * 100


def fetch_snapshots(
    api: Any,
    quote_ctx: Any,
    codes: list[str],
    batch_size: int,
    retry_after_seconds: float = 31.0,
    retries: int = 1,
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for start in range(0, len(codes), batch_size):
        batch = codes[start : start + batch_size]
        ret, payload = quote_ctx.get_market_snapshot(batch)
        if ret != api.RET_OK:
            if "high frequency" in str(payload).lower() and retries > 0:
                print(f"Rate limit hit; sleeping {retry_after_seconds:.0f}s before retrying snapshot batch.", file=sys.stderr)
                time.sleep(retry_after_seconds)
                retry_batch_size = min(batch_size, max(1, len(batch) // 2))
                snapshots.update(fetch_snapshots(api, quote_ctx, batch, retry_batch_size, retry_after_seconds, retries - 1))
                continue
            if len(batch) == 1:
                print(f"Skipping {batch[0]}: get_market_snapshot failed: {payload}", file=sys.stderr)
                continue
            snapshots.update(fetch_snapshots(api, quote_ctx, batch, batch_size=max(1, len(batch) // 2), retries=retries))
            continue
        for row in payload.to_dict("records"):
            snapshots[str(row.get("code", ""))] = row
    return snapshots


def translate_items(items: list[str], mapping: dict[str, str]) -> str:
    translated = [mapping.get(item, item) for item in items if item]
    return " / ".join(dict.fromkeys(translated))


def fetch_plate_info(api: Any, quote_ctx: Any, codes: list[str]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, list[str]]] = {
        code: {"industry": [], "themes": []} for code in codes
    }

    def apply_batch(batch: list[str]) -> None:
        ret, payload = quote_ctx.get_owner_plate(batch)
        if ret != api.RET_OK:
            if len(batch) == 1:
                print(f"Skipping owner plate for {batch[0]}: {payload}", file=sys.stderr)
                return
            midpoint = max(1, len(batch) // 2)
            apply_batch(batch[:midpoint])
            apply_batch(batch[midpoint:])
            return
        for item in payload.to_dict("records"):
            code = str(item.get("code", ""))
            plate_name = str(item.get("plate_name", ""))
            plate_type = str(item.get("plate_type", ""))
            if code not in result:
                continue
            if plate_type == "INDUSTRY":
                result[code]["industry"].append(plate_name)
            elif plate_type in {"CONCEPT", "OTHER"}:
                result[code]["themes"].append(plate_name)

    for start in range(0, len(codes), 50):
        apply_batch(codes[start : start + 50])

    return {
        code: {
            "industry": translate_items(values["industry"], INDUSTRY_JA) or "未取得",
            "themes": translate_items(values["themes"], THEME_JA) or "未取得",
        }
        for code, values in result.items()
    }


def risk_flags(symbol: str, name: str, exchange: str, industry: str, themes: str) -> str:
    normalized = " ".join([symbol, name, exchange, industry, themes]).lower()

    def has_word(word: str) -> bool:
        # 単語境界マッチ: "spac" が "space"、"otc" が "scotch" 等に誤爆しないように
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", normalized) is not None

    flags = []
    if has_word("pink") or has_word("otc"):
        flags.append("OTC/PINK")
    if has_word("shell") or has_word("spac") or "acquisition corp" in normalized:
        flags.append("SPAC/シェル")
    if symbol.upper().endswith("U") or " unit" in normalized:
        flags.append("ユニット株疑い")
    if has_word("warrant") or has_word("warrants"):
        flags.append("ワラント疑い")
    if has_word("preferred") or " preference" in normalized:
        flags.append("優先株疑い")
    return " / ".join(flags) if flags else "なし"


def fetch_market_rows(
    api: Any,
    quote_ctx: Any,
    preset: MarketPreset,
    page_size: int,
    page_sleep: float,
    max_rows: int,
) -> list[dict[str, str]]:
    filters = {
        "price": make_simple_filter(api, ("CUR_PRICE",), preset.price_min, preset.price_max),
        "market_cap": make_simple_filter(api, ("MARKET_VAL",), preset.market_cap_min, preset.market_cap_max),
        "volume_ratio": make_simple_filter(api, ("VOLUME_RATIO",), preset.volume_ratio_min, None),
        "distance_to_52w_high_pct": make_simple_filter(
            api,
            ("CUR_PRICE_TO_HIGHEST52_WEEKS_RATIO", "CUR_PRICE_TO_HIGHEST_52WEEKS_RATIO"),
            preset.high_distance_min,
            0,
        ),
        "float_shares": make_simple_filter(api, ("FLOAT_SHARE",), 0, None),
    }

    filter_rows: list[dict[str, Any]] = []
    begin = 0
    api_market = enum_value(api.Market, preset.api_market)

    while True:
        try:
            ret, payload = quote_ctx.get_stock_filter(
                market=api_market,
                filter_list=list(filters.values()),
                begin=begin,
                num=page_size,
            )
        except Exception as exc:
            raise SystemExit(
                "OpenAPI get_stock_filter call failed. Check that moomoo OpenD is "
                f"running and logged in at the configured host/port. Detail: {exc}"
            ) from exc
        if ret != api.RET_OK:
            raise SystemExit(f"OpenAPI get_stock_filter failed for {preset.api_market}: {payload}")

        last_page, _all_count, stock_list = payload
        for item in stock_list:
            filter_rows.append(
                {
                    "symbol": stock_attr(item, "stock_code"),
                    "name": stock_attr(item, "stock_name"),
                    "market": preset.csv_market,
                    "price": field_value(item, filters["price"]),
                    "market_cap": field_value(item, filters["market_cap"]),
                    "volume_ratio": field_value(item, filters["volume_ratio"]),
                    "distance_to_52w_high_pct": field_value(item, filters["distance_to_52w_high_pct"]),
                    "float_shares": field_value(item, filters["float_shares"]),
                }
            )
            if len(filter_rows) >= max_rows:
                break

        if len(filter_rows) >= max_rows or last_page or not stock_list:
            break
        begin += len(stock_list)
        time.sleep(page_sleep)

    snapshots = fetch_snapshots(api, quote_ctx, [row["symbol"] for row in filter_rows], batch_size=100)
    plates = fetch_plate_info(api, quote_ctx, [row["symbol"] for row in filter_rows])
    rows: list[dict[str, str]] = []
    for row in filter_rows:
        snap = snapshots.get(row["symbol"], {})
        last_price = safe_float(snap.get("last_price")) or safe_float(row["price"])
        prev_close = safe_float(snap.get("prev_close_price"))
        open_price = safe_float(snap.get("open_price"))
        volume = safe_float(snap.get("volume"))
        turnover = safe_float(snap.get("turnover"))
        volume_ratio = safe_float(snap.get("volume_ratio")) or safe_float(row["volume_ratio"])
        avg_volume = volume / volume_ratio if volume and volume_ratio else 0.0
        change_pct = pct_change(last_price, prev_close)
        gap_pct = pct_change(open_price, prev_close)
        if change_pct < preset.change_min:
            continue
        symbol = str(row["symbol"])
        name = str(snap.get("name") or row["name"])
        exchange = str(snap.get("exchange_type") or "")
        industry = plates.get(symbol, {}).get("industry", "未取得")
        themes = plates.get(symbol, {}).get("themes", "未取得")
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "market": preset.csv_market,
                "exchange": exchange,
                "industry": industry,
                "themes": themes,
                "price": f"{last_price:.4f}",
                "market_cap": f"{(safe_float(snap.get('total_market_val')) or safe_float(row['market_cap'])):.0f}",
                "volume": f"{volume:.0f}",
                "turnover": f"{turnover:.0f}",
                "avg_volume_20d": f"{avg_volume:.0f}",
                "change_pct": f"{change_pct:.4f}",
                "distance_to_52w_high_pct": f"{safe_float(row['distance_to_52w_high_pct']):.4f}",
                "gap_pct": f"{gap_pct:.4f}",
                "catalyst": "",
                "float_shares": f"{safe_float(row['float_shares']):.0f}",
                "short_interest_pct": "",
                "risk_flags": risk_flags(symbol, name, exchange, industry, themes),
            }
        )
    return rows


def to_yahoo_symbol(symbol: str) -> str | None:
    """moomoo symbol -> Yahoo Finance symbol. US.PRSU->PRSU, JP.7716->7716.T, HK.00700->0700.HK"""
    if symbol.startswith("^"):
        return symbol
    if "." not in symbol:
        return None
    market, code = symbol.split(".", 1)
    market = market.upper()
    if market == "US":
        return code.replace(".", "-")
    if market == "JP":
        return f"{code}.T"
    if market == "HK":
        return f"{code[-4:]}.HK" if len(code) >= 4 else f"{code.zfill(4)}.HK"
    return None


def fetch_daily_bars_yahoo(symbol: str, timeout: float = 20.0, range_str: str = "2y") -> list[dict[str, float]]:
    """Daily bars from Yahoo Finance chart API (free, no quota). Raises RuntimeError on failure.
    range_str: Yahoo range token (e.g. '2y','5y','10y'). Default '2y' keeps existing callers unchanged."""
    import json as json_module
    import urllib.request

    yahoo_symbol = to_yahoo_symbol(symbol)
    if yahoo_symbol is None:
        raise RuntimeError(f"{symbol}: Yahooシンボルに変換できない")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?range={range_str}&interval=1d"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (bars-fetcher)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json_module.loads(response.read().decode("utf-8", errors="replace"))
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        adjcloses = result["indicators"].get("adjclose", [{}])[0].get("adjclose")
    except Exception as exc:
        raise RuntimeError(f"{symbol} ({yahoo_symbol}): Yahoo取得失敗: {exc}") from exc

    bars: list[dict[str, float]] = []
    for index, timestamp in enumerate(timestamps):
        values = {}
        for field in ("open", "high", "low", "close", "volume"):
            raw = quote.get(field, [None])[index] if index < len(quote.get(field, [])) else None
            if raw is None:
                values = {}
                break
            values[field] = float(raw)
        if not values or values["close"] <= 0:
            continue
        # 分割・配当の伪影を避けるため adjclose で全OHLCを調整する
        factor = 1.0
        if adjcloses is not None and index < len(adjcloses) and adjcloses[index]:
            factor = float(adjcloses[index]) / values["close"]
        bars.append(
            {
                "date": time.strftime("%Y-%m-%d", time.localtime(timestamp)),
                "open": values["open"] * factor,
                "high": values["high"] * factor,
                "low": values["low"] * factor,
                "close": values["close"] * factor,
                "volume": values["volume"] / factor if factor > 0 else values["volume"],
            }
        )
    if not bars:
        raise RuntimeError(f"{symbol} ({yahoo_symbol}): Yahooに有効データなし")
    return bars


def fetch_daily_bars(api: Any, quote_ctx: Any, symbol: str, start: str, end: str) -> list[dict[str, float]]:
    bars: list[dict[str, float]] = []
    page_req_key = None
    while True:
        ret, data, page_req_key = quote_ctx.request_history_kline(
            symbol,
            start=start,
            end=end,
            ktype=api.KLType.K_DAY,
            max_count=1000,
            page_req_key=page_req_key,
        )
        if ret != api.RET_OK:
            raise RuntimeError(f"{symbol}: request_history_kline failed: {data}")
        for row in data.to_dict("records"):
            bar = {
                "date": str(row.get("time_key", ""))[:10],
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "close": safe_float(row.get("close")),
                "volume": safe_float(row.get("volume")),
            }
            if bar["close"] > 0:
                bars.append(bar)
        if page_req_key is None:
            break
        time.sleep(0.5)
    return bars


def sma(closes: list[float], end_index: int, window: int) -> float:
    start = end_index - window + 1
    if start < 0:
        return 0.0
    sample = closes[start : end_index + 1]
    return sum(sample) / len(sample)


def period_return_pct(closes: list[float], end_index: int, lookback: int) -> float:
    start = end_index - lookback
    if start < 0 or closes[start] == 0:
        return 0.0
    return (closes[end_index] / closes[start] - 1) * 100


def compute_position_metrics(
    bars: list[dict[str, float]],
    benchmark_closes: list[float],
    breakout_window: int = 60,
    base_window: int = 35,
    rs_lookback: int = 126,
) -> dict[str, float] | None:
    if len(bars) < 210:
        return None
    i = len(bars) - 1
    closes = [bar["close"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]

    sma50 = sma(closes, i, 50)
    sma200 = sma(closes, i, 200)
    sma200_prev = sma(closes, i - 20, 200) if i - 20 >= 199 else sma200
    year_start = max(0, i - 251)
    high_52w = max(highs[year_start : i + 1])
    low_52w = min(lows[year_start : i + 1])

    stock_return = period_return_pct(closes, i, rs_lookback)
    bench_return = 0.0
    if len(benchmark_closes) > rs_lookback:
        j = len(benchmark_closes) - 1
        bench_return = period_return_pct(benchmark_closes, j, rs_lookback)
    rs_6m_pct = stock_return - bench_return

    base_start = max(0, i - base_window)
    base_high = max(highs[base_start:i])
    base_low = min(lows[base_start:i])
    base_depth_pct = (base_high / base_low - 1) * 100 if base_low > 0 else 999.0

    prior_high = max(highs[max(0, i - breakout_window) : i])
    breakout_new_high = 1.0 if closes[i] > prior_high else 0.0

    avg_volume_20d = sum(volumes[i - 20 : i]) / 20 if i >= 20 else 0.0
    volume_ratio_20d = volumes[i] / avg_volume_20d if avg_volume_20d > 0 else 0.0

    true_ranges = []
    for j in range(i - 13, i + 1):
        prev_close = closes[j - 1]
        true_ranges.append(
            max(highs[j] - lows[j], abs(highs[j] - prev_close), abs(lows[j] - prev_close))
        )
    atr14_pct = (sum(true_ranges) / len(true_ranges)) / closes[i] * 100 if closes[i] > 0 else 0.0

    return {
        "atr14_pct": atr14_pct,
        "sma50": sma50,
        "sma200": sma200,
        "sma200_prev": sma200_prev,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "rs_6m_pct": rs_6m_pct,
        "base_depth_pct": base_depth_pct,
        "base_len_days": float(base_window),
        "breakout_new_high": breakout_new_high,
        "volume_ratio_20d": volume_ratio_20d,
    }


def try_financial_filters(api: Any, market: str) -> list[Any]:
    """Revenue growth >= 15% via moomoo FinancialFilter when the SDK supports it.
    HK and A-share screening rejects MOST_RECENT_QUARTER, so those use ANNUAL."""
    quarter_names = ("ANNUAL",) if market.upper() in {"HK", "CN", "SH", "SZ"} else ("MOST_RECENT_QUARTER", "ANNUAL")
    try:
        item = api.FinancialFilter()
        item.stock_field = enum_value(
            api.StockField,
            "SUM_OF_BUSINESS_GROWTH",
            "OPERATING_REVENUE_GROWTH",
            "INCOME_GROWTH",
        )
        item.filter_min = 15.0
        item.is_no_filter = False
        item.quarter = enum_value(api.FinancialQuarter, *quarter_names)
        return [item]
    except (SystemExit, AttributeError) as exc:
        print(f"Financial filter unavailable, screening without it: {exc}", file=sys.stderr)
        return []


def load_fundamentals_csv(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if "symbol" not in (reader.fieldnames or []):
            raise SystemExit("--fundamentals-csv must contain a symbol column")
        return {row["symbol"].strip(): row for row in reader if row.get("symbol", "").strip()}


def _load_symbols(symbols_arg: str, symbols_csv: Path | None) -> list[str]:
    """Build a watchlist from --symbols and/or --symbols-csv. Returns [] if neither."""
    out: list[str] = []
    seen: set[str] = set()
    for token in symbols_arg.split(","):
        token = token.strip()
        if token and token not in seen:
            out.append(token)
            seen.add(token)
    if symbols_csv is not None and symbols_csv.exists():
        with symbols_csv.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if "symbol" not in (reader.fieldnames or []):
                raise SystemExit("--symbols-csv must contain a symbol column")
            for row in reader:
                token = row.get("symbol", "").strip()
                if token and token not in seen:
                    out.append(token)
                    seen.add(token)
    return out


def make_bars_fetcher(api: Any, quote_ctx: Any, bars_source: str, start_date: str, end_date: str):
    """Returns fetch(symbol) honoring the source priority. auto = Yahoo first (no quota), moomoo fallback."""

    def fetch(symbol: str) -> list[dict[str, float]]:
        if bars_source in {"yahoo", "auto"}:
            try:
                bars = fetch_daily_bars_yahoo(symbol)
                if len(bars) >= 210:
                    return bars
                if bars_source == "yahoo":
                    return bars
                print(f"{symbol}: Yahooのバー数不足 ({len(bars)}本) -> moomooにフォールバック", file=sys.stderr)
            except RuntimeError as exc:
                if bars_source == "yahoo":
                    raise
                print(f"{symbol}: {exc} -> moomooにフォールバック", file=sys.stderr)
        return fetch_daily_bars(api, quote_ctx, symbol, start_date, end_date)

    return fetch


def fetch_position_rows(
    api: Any,
    quote_ctx: Any,
    preset: MarketPreset,
    page_size: int,
    page_sleep: float,
    max_rows: int,
    history_days: int,
    symbol_sleep: float,
    fundamentals: dict[str, dict[str, str]],
    benchmark_override: str | None,
    bars_source: str = "auto",
    symbols: list[str] | None = None,
) -> list[dict[str, str]]:
    if symbols:
        # ウォッチリストモード: 明示銘柄を直接スキャン。市場フィルタ・時価総額上限・
        # 財務フィルタ・流動性予選を全てスキップし、大型銘柄(三菱重工等)も拾う。
        filter_rows: list[dict[str, Any]] = [
            {"symbol": s, "name": "", "market_cap": 0.0} for s in symbols
        ]
        print(
            f"{preset.csv_market}: ウォッチリスト{len(symbols)}銘柄を直接スキャン"
            f"（市場フィルタ・時価総額上限・予選なし）"
        )
    else:
        filters = {
            "price": make_simple_filter(api, ("CUR_PRICE",), preset.price_min, preset.price_max),
            "market_cap": make_simple_filter(api, ("MARKET_VAL",), preset.market_cap_min, preset.market_cap_max),
            "distance_to_52w_high_pct": make_simple_filter(
                api,
                ("CUR_PRICE_TO_HIGHEST52_WEEKS_RATIO", "CUR_PRICE_TO_HIGHEST_52WEEKS_RATIO"),
                preset.high_distance_min,
                0,
            ),
        }
        filter_list: list[Any] = list(filters.values()) + try_financial_filters(api, preset.csv_market)

        filter_rows = []
        begin = 0
        api_market = enum_value(api.Market, preset.api_market)
        while True:
            ret, payload = quote_ctx.get_stock_filter(
                market=api_market,
                filter_list=filter_list,
                begin=begin,
                num=page_size,
            )
            if ret != api.RET_OK:
                raise SystemExit(f"OpenAPI get_stock_filter failed for {preset.api_market}: {payload}")
            last_page, _all_count, stock_list = payload
            for item in stock_list:
                filter_rows.append(
                    {
                        "symbol": stock_attr(item, "stock_code"),
                        "name": stock_attr(item, "stock_name"),
                        "market_cap": field_value(item, filters["market_cap"]),
                    }
                )
                if len(filter_rows) >= max_rows:
                    break
            if len(filter_rows) >= max_rows or last_page or not stock_list:
                break
            begin += len(stock_list)
            time.sleep(page_sleep)

    end_date = time.strftime("%Y-%m-%d")
    start_date = time.strftime("%Y-%m-%d", time.localtime(time.time() - history_days * 86400))
    fetch_bars = make_bars_fetcher(api, quote_ctx, bars_source, start_date, end_date)

    if benchmark_override:
        benchmark_symbol = benchmark_override
    elif bars_source in {"yahoo", "auto"}:
        benchmark_symbol = YAHOO_BENCHMARKS.get(preset.csv_market, "")
    else:
        benchmark_symbol = BENCHMARKS.get(preset.csv_market, "")
    benchmark_closes: list[float] = []
    if benchmark_symbol:
        try:
            if benchmark_symbol.startswith("^"):
                benchmark_closes = [bar["close"] for bar in fetch_daily_bars_yahoo(benchmark_symbol)]
            else:
                benchmark_closes = [bar["close"] for bar in fetch_bars(benchmark_symbol)]
        except RuntimeError as exc:
            print(f"Benchmark fetch failed ({benchmark_symbol}); RS will use absolute return: {exc}", file=sys.stderr)

    snapshots = fetch_snapshots(api, quote_ctx, [row["symbol"] for row in filter_rows], batch_size=100)

    if not symbols:
        turnover_minimums = {"US": 5_000_000, "HK": 10_000_000, "JP": 300_000_000}
        turnover_min = turnover_minimums.get(preset.csv_market, 0)
        liquid_rows = []
        for row in filter_rows:
            snap = snapshots.get(str(row["symbol"]), {})
            if safe_float(snap.get("turnover")) >= turnover_min:
                liquid_rows.append(row)
        skipped = len(filter_rows) - len(liquid_rows)
        if skipped:
            print(
                f"{preset.csv_market}: {skipped}銘柄を流動性不足で除外 (歴史K線クォータ節約のため取得前にフィルタ)"
            )
        filter_rows = liquid_rows

    plates = fetch_plate_info(api, quote_ctx, [row["symbol"] for row in filter_rows])

    rows: list[dict[str, str]] = []
    for row in filter_rows:
        symbol = str(row["symbol"])
        try:
            bars = fetch_bars(symbol)
        except RuntimeError as exc:
            print(f"Skipping {symbol}: {exc}", file=sys.stderr)
            time.sleep(symbol_sleep)
            continue
        metrics = compute_position_metrics(bars, benchmark_closes)
        time.sleep(symbol_sleep)
        if metrics is None:
            continue

        snap = snapshots.get(symbol, {})
        name = str(snap.get("name") or row["name"])
        exchange = str(snap.get("exchange_type") or "")
        industry = plates.get(symbol, {}).get("industry", "未取得")
        themes = plates.get(symbol, {}).get("themes", "未取得")
        fundamental = fundamentals.get(symbol, {})
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "market": preset.csv_market,
                "exchange": exchange,
                "industry": industry,
                "themes": themes,
                "price": f"{bars[-1]['close']:.4f}",
                "market_cap": f"{(safe_float(snap.get('total_market_val')) or safe_float(row['market_cap'])):.0f}",
                "turnover": f"{safe_float(snap.get('turnover')):.0f}",
                "sma50": f"{metrics['sma50']:.4f}",
                "sma200": f"{metrics['sma200']:.4f}",
                "sma200_prev": f"{metrics['sma200_prev']:.4f}",
                "high_52w": f"{metrics['high_52w']:.4f}",
                "low_52w": f"{metrics['low_52w']:.4f}",
                "rs_6m_pct": f"{metrics['rs_6m_pct']:.4f}",
                "base_depth_pct": f"{metrics['base_depth_pct']:.4f}",
                "base_len_days": f"{metrics['base_len_days']:.0f}",
                "breakout_new_high": f"{metrics['breakout_new_high']:.0f}",
                "volume_ratio_20d": f"{metrics['volume_ratio_20d']:.4f}",
                "atr14_pct": f"{metrics['atr14_pct']:.4f}",
                "lot_size": f"{safe_float(snap.get('lot_size')):.0f}",
                "revenue_growth_pct": str(fundamental.get("revenue_growth_pct", "")),
                "revenue_accel_pp": str(fundamental.get("revenue_accel_pp", "")),
                "catalyst": str(fundamental.get("catalyst", "")),
                "risk_flags": risk_flags(symbol, name, exchange, industry, themes),
            }
        )
    return rows


def write_position_csv(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "symbol", "name", "market", "exchange", "industry", "themes",
        "price", "market_cap", "turnover",
        "sma50", "sma200", "sma200_prev", "high_52w", "low_52w",
        "rs_6m_pct", "base_depth_pct", "base_len_days",
        "breakout_new_high", "volume_ratio_20d", "atr14_pct", "lot_size",
        "revenue_growth_pct", "revenue_accel_pp", "catalyst", "risk_flags",
    ]
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_input_csv(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "symbol",
        "name",
        "market",
        "exchange",
        "industry",
        "themes",
        "price",
        "market_cap",
        "volume",
        "turnover",
        "avg_volume_20d",
        "change_pct",
        "distance_to_52w_high_pct",
        "gap_pct",
        "catalyst",
        "float_shares",
        "short_interest_pct",
        "risk_flags",
    ]
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_ranker(input_csv: Path, output_csv: Path, alerts_only: bool, mode: str = "runner") -> None:
    scanner = "position_trend_scanner.py" if mode == "position" else "short_term_runner_scanner.py"
    command = [
        sys.executable,
        str(Path(__file__).with_name(scanner)),
        str(input_csv),
        "-o",
        str(output_csv),
    ]
    if alerts_only:
        command.append("--alerts-only")
    subprocess.run(command, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Screen runner candidates with moomoo OpenAPI/OpenD.")
    parser.add_argument("--host", default="127.0.0.1", help="OpenD host")
    parser.add_argument("--port", type=int, default=11111, help="OpenD quote port")
    parser.add_argument(
        "--mode",
        choices=["runner", "position"],
        default="runner",
        help="runner=当日急騰スキャン, position=中期ブレイクアウトスキャン",
    )
    parser.add_argument(
        "--markets",
        default="US",
        help="Comma-separated markets to scan. Supported: US,JP,HK",
    )
    parser.add_argument("--page-size", type=int, default=200, help="OpenAPI page size")
    parser.add_argument("--page-sleep", type=float, default=3.0, help="Seconds between paged API calls")
    parser.add_argument("--max-rows-per-market", type=int, default=400, help="Limit rows per market")
    parser.add_argument("--history-days", type=int, default=420, help="Calendar days of daily bars for position mode")
    parser.add_argument("--symbol-sleep", type=float, default=0.6, help="Seconds between per-symbol K-line requests")
    parser.add_argument("--benchmark", default="", help="Benchmark symbol override for relative strength")
    parser.add_argument(
        "--bars-source",
        choices=["auto", "yahoo", "moomoo"],
        default="auto",
        help="日足の取得元。auto=Yahoo優先(クォータ消費なし)+moomooフォールバック",
    )
    parser.add_argument(
        "--fundamentals-csv",
        type=Path,
        help="Optional CSV: symbol,revenue_growth_pct,revenue_accel_pp,catalyst",
    )
    parser.add_argument(
        "--symbols-csv",
        type=Path,
        help="ウォッチリストCSV(symbol列)。指定すると市場フィルタを使わず明示銘柄を直接スキャン(大型も拾う)",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="カンマ区切りのウォッチリスト銘柄。例: JP.7011,JP.7012,JP.7013",
    )
    parser.add_argument("--input-output", type=Path, default=DEFAULT_OUTPUT, help="Generated scanner input CSV")
    parser.add_argument(
        "--ranked-output",
        type=Path,
        default=Path("outputs/moomoo_runner_candidates.csv"),
        help="Ranked ALERT/WATCH output CSV",
    )
    parser.add_argument("--no-rank", action="store_true", help="Only fetch OpenAPI data; do not run ranker")
    parser.add_argument("--all-ranked", action="store_true", help="Keep IGNORE rows in ranked output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selected_markets = [market.strip().upper() for market in args.markets.split(",") if market.strip()]
    unsupported = sorted(set(selected_markets).difference(PRESETS))
    if unsupported:
        raise SystemExit(f"Unsupported market(s): {', '.join(unsupported)}. Supported: {', '.join(PRESETS)}")

    api = import_openapi()
    try:
        quote_ctx = api.OpenQuoteContext(host=args.host, port=args.port)
    except Exception as exc:
        raise SystemExit(
            "Could not create OpenQuoteContext. Start moomoo OpenD, log in, and "
            f"confirm the quote port. Detail: {exc}"
        ) from exc
    try:
        rows: list[dict[str, str]] = []
        if args.mode == "position":
            fundamentals = load_fundamentals_csv(args.fundamentals_csv)
            watchlist = _load_symbols(args.symbols, args.symbols_csv)
            for market in selected_markets:
                market_symbols = (
                    [s for s in watchlist if s.upper().startswith(f"{market}.")] if watchlist else None
                )
                if watchlist and not market_symbols:
                    continue
                try:
                    market_rows = fetch_position_rows(
                        api=api,
                        quote_ctx=quote_ctx,
                        preset=POSITION_PRESETS[market],
                        page_size=args.page_size,
                        page_sleep=args.page_sleep,
                        max_rows=args.max_rows_per_market,
                        history_days=args.history_days,
                        symbol_sleep=args.symbol_sleep,
                        fundamentals=fundamentals,
                        benchmark_override=args.benchmark or None,
                        bars_source=args.bars_source,
                        symbols=market_symbols,
                    )
                except (SystemExit, RuntimeError) as exc:
                    print(f"{market}: スキャン失敗、他市場は継続 ({exc})", file=sys.stderr)
                    continue
                rows.extend(market_rows)
                print(f"{market}: fetched {len(market_rows)} position candidate rows")
            write_position_csv(rows, args.input_output)
        else:
            for market in selected_markets:
                market_rows = fetch_market_rows(
                    api=api,
                    quote_ctx=quote_ctx,
                    preset=PRESETS[market],
                    page_size=args.page_size,
                    page_sleep=args.page_sleep,
                    max_rows=args.max_rows_per_market,
                )
                rows.extend(market_rows)
                print(f"{market}: fetched {len(market_rows)} candidate rows")
            write_input_csv(rows, args.input_output)
        print(f"Wrote OpenAPI scanner input: {args.input_output}")
    finally:
        quote_ctx.close()

    if not args.no_rank:
        run_ranker(args.input_output, args.ranked_output, alerts_only=not args.all_ranked, mode=args.mode)


if __name__ == "__main__":
    main()
