#!/usr/bin/env python3
"""
Backtest short-term runner signals with moomoo OpenAPI historical K-lines.

This is a research tool, not financial advice and not an order bot. It fetches
daily bars from moomoo OpenD, creates momentum signals, then simulates simple
next-day entries with take-profit, stop-loss, and max-hold exits.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SignalConfig:
    min_change_pct: float
    min_relative_volume: float
    min_gap_pct: float
    min_high_distance_pct: float
    avg_volume_window: int
    high_window: int


@dataclass(frozen=True)
class TradeConfig:
    take_profit_pct: float
    stop_loss_pct: float
    max_hold_days: int
    slippage_pct: float


@dataclass(frozen=True)
class PositionSignalConfig:
    breakout_window: int
    base_window: int
    base_depth_max_pct: float
    min_volume_ratio: float
    avg_volume_window: int
    rs_lookback: int


@dataclass(frozen=True)
class PositionTradeConfig:
    stop_loss_pct: float
    trail_sma_window: int
    max_hold_days: int
    slippage_pct: float
    take_profit_pct: float


@dataclass
class Trade:
    symbol: str
    signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_pct: float
    exit_reason: str
    hold_days: int
    signal_change_pct: float
    signal_relative_volume: float
    signal_gap_pct: float
    signal_high_distance_pct: float


def import_openapi() -> Any:
    try:
        import moomoo as api  # type: ignore

        return api
    except ImportError as exc:
        raise SystemExit("moomoo-api is not installed. Run: python3 -m pip install -r outputs/requirements.txt") from exc


def safe_float(value: Any) -> float:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return ((current - previous) / previous) * 100


def fetch_history(api: Any, quote_ctx: Any, symbol: str, start: str, end: str, page_sleep: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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
        rows.extend(data.to_dict("records"))
        if page_req_key is None:
            break
        time.sleep(page_sleep)
    return rows


def normalize_bars(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bars = []
    for row in raw_rows:
        bars.append(
            {
                "date": str(row["time_key"])[:10],
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "close": safe_float(row.get("close")),
                "volume": safe_float(row.get("volume")),
                "last_close": safe_float(row.get("last_close")),
            }
        )
    return [bar for bar in bars if bar["open"] and bar["high"] and bar["low"] and bar["close"]]


def rolling_average(values: list[float], end_index: int, window: int) -> float:
    start = max(0, end_index - window)
    sample = values[start:end_index]
    if not sample:
        return 0.0
    return sum(sample) / len(sample)


def rolling_high(values: list[float], end_index: int, window: int) -> float:
    start = max(0, end_index - window)
    sample = values[start:end_index]
    return max(sample) if sample else 0.0


def make_signals(bars: list[dict[str, Any]], config: SignalConfig) -> list[dict[str, Any]]:
    signals = []
    volumes = [bar["volume"] for bar in bars]
    highs = [bar["high"] for bar in bars]

    for i, bar in enumerate(bars):
        if i < max(config.avg_volume_window, 2):
            continue
        previous_close = bar["last_close"] or bars[i - 1]["close"]
        avg_volume = rolling_average(volumes, i, config.avg_volume_window)
        prior_high = rolling_high(highs, i, config.high_window)
        if avg_volume <= 0 or prior_high <= 0:
            continue

        change_pct = pct_change(bar["close"], previous_close)
        relative_volume = bar["volume"] / avg_volume
        gap_pct = pct_change(bar["open"], previous_close)
        high_distance_pct = pct_change(bar["close"], prior_high)

        if (
            change_pct >= config.min_change_pct
            and relative_volume >= config.min_relative_volume
            and gap_pct >= config.min_gap_pct
            and high_distance_pct >= config.min_high_distance_pct
        ):
            signals.append(
                {
                    "index": i,
                    "date": bar["date"],
                    "change_pct": change_pct,
                    "relative_volume": relative_volume,
                    "gap_pct": gap_pct,
                    "high_distance_pct": high_distance_pct,
                }
            )
    return signals


def sma_at(closes: list[float], index: int, window: int) -> float:
    start = index - window + 1
    if start < 0:
        return 0.0
    sample = closes[start : index + 1]
    return sum(sample) / len(sample)


def make_benchmark_lookup(bench_bars: list[dict[str, Any]]) -> dict[str, float]:
    return {bar["date"]: bar["close"] for bar in bench_bars}


def benchmark_return_pct(
    lookup: dict[str, float], sorted_dates: list[str], start_date: str, end_date: str
) -> float:
    import bisect

    def close_on_or_before(date: str) -> float:
        index = bisect.bisect_right(sorted_dates, date) - 1
        if index < 0:
            return 0.0
        return lookup[sorted_dates[index]]

    start_close = close_on_or_before(start_date)
    end_close = close_on_or_before(end_date)
    if start_close <= 0:
        return 0.0
    return (end_close / start_close - 1) * 100


def make_position_signals(
    bars: list[dict[str, Any]],
    config: PositionSignalConfig,
    bench_lookup: dict[str, float] | None,
    bench_dates: list[str] | None,
) -> list[dict[str, Any]]:
    signals = []
    closes = [bar["close"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    minimum_history = max(200 + 20, config.rs_lookback + 1, config.breakout_window + 1)

    for i, bar in enumerate(bars):
        if i < minimum_history:
            continue

        sma50 = sma_at(closes, i, 50)
        sma200 = sma_at(closes, i, 200)
        sma200_prev = sma_at(closes, i - 20, 200)
        if not (bar["close"] > sma50 > sma200 > 0 and sma200 > sma200_prev):
            continue

        base_start = i - config.base_window
        base_high = max(highs[base_start:i])
        base_low = min(lows[base_start:i])
        if base_low <= 0:
            continue
        base_depth_pct = (base_high / base_low - 1) * 100
        if base_depth_pct > config.base_depth_max_pct:
            continue

        prior_high = max(highs[i - config.breakout_window : i])
        if bar["close"] <= prior_high:
            continue

        avg_volume = rolling_average(volumes, i, config.avg_volume_window)
        if avg_volume <= 0:
            continue
        volume_ratio = bar["volume"] / avg_volume
        if volume_ratio < config.min_volume_ratio:
            continue

        stock_return = (
            (closes[i] / closes[i - config.rs_lookback] - 1) * 100
            if closes[i - config.rs_lookback] > 0
            else 0.0
        )
        rs_pct = stock_return
        if bench_lookup and bench_dates:
            rs_pct = stock_return - benchmark_return_pct(
                bench_lookup, bench_dates, bars[i - config.rs_lookback]["date"], bar["date"]
            )
        if rs_pct <= 0:
            continue

        signals.append(
            {
                "index": i,
                "date": bar["date"],
                "rs_pct": rs_pct,
                "base_depth_pct": base_depth_pct,
                "volume_ratio": volume_ratio,
                "breakout_margin_pct": pct_change(bar["close"], prior_high),
            }
        )
    return signals


def simulate_position_trade(
    symbol: str,
    bars: list[dict[str, Any]],
    signal: dict[str, Any],
    config: PositionTradeConfig,
) -> Trade | None:
    entry_index = signal["index"] + 1
    if entry_index >= len(bars):
        return None

    closes = [bar["close"] for bar in bars]
    entry_bar = bars[entry_index]
    entry_price = entry_bar["open"] * (1 + config.slippage_pct / 100)
    stop_loss_price = entry_price * (1 - config.stop_loss_pct / 100)
    take_profit_price = (
        entry_price * (1 + config.take_profit_pct / 100) if config.take_profit_pct > 0 else None
    )
    last_exit_index = min(len(bars) - 1, entry_index + config.max_hold_days - 1)

    exit_price = bars[last_exit_index]["close"] * (1 - config.slippage_pct / 100)
    exit_reason = "期限決済"
    exit_index = last_exit_index

    for i in range(entry_index, last_exit_index + 1):
        bar = bars[i]
        if bar["low"] <= stop_loss_price:
            exit_price = stop_loss_price * (1 - config.slippage_pct / 100)
            exit_reason = "損切り"
            exit_index = i
            break
        if take_profit_price is not None and bar["high"] >= take_profit_price:
            exit_price = take_profit_price * (1 - config.slippage_pct / 100)
            exit_reason = "利確"
            exit_index = i
            break
        trail_sma = sma_at(closes, i, config.trail_sma_window)
        if trail_sma > 0 and bar["close"] < trail_sma:
            exit_price = bar["close"] * (1 - config.slippage_pct / 100)
            exit_reason = f"{config.trail_sma_window}日線割れ"
            exit_index = i
            break

    return_pct = pct_change(exit_price, entry_price)
    return Trade(
        symbol=symbol,
        signal_date=signal["date"],
        entry_date=entry_bar["date"],
        exit_date=bars[exit_index]["date"],
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=return_pct,
        exit_reason=exit_reason,
        hold_days=exit_index - entry_index + 1,
        signal_change_pct=signal["breakout_margin_pct"],
        signal_relative_volume=signal["volume_ratio"],
        signal_gap_pct=signal["rs_pct"],
        signal_high_distance_pct=signal["base_depth_pct"],
    )


def simulate_position_trades_no_overlap(
    symbol: str,
    bars: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    config: PositionTradeConfig,
) -> list[Trade]:
    trades: list[Trade] = []
    blocked_until_index = -1
    date_to_index = {bar["date"]: i for i, bar in enumerate(bars)}
    for signal in signals:
        if signal["index"] <= blocked_until_index:
            continue
        trade = simulate_position_trade(symbol, bars, signal, config)
        if trade is None:
            continue
        trades.append(trade)
        blocked_until_index = date_to_index.get(trade.exit_date, signal["index"])
    return trades


def simulate_trade(symbol: str, bars: list[dict[str, Any]], signal: dict[str, Any], config: TradeConfig) -> Trade | None:
    entry_index = signal["index"] + 1
    if entry_index >= len(bars):
        return None

    entry_bar = bars[entry_index]
    entry_price = entry_bar["open"] * (1 + config.slippage_pct / 100)
    take_profit_price = entry_price * (1 + config.take_profit_pct / 100)
    stop_loss_price = entry_price * (1 - config.stop_loss_pct / 100)
    last_exit_index = min(len(bars) - 1, entry_index + config.max_hold_days - 1)

    exit_price = bars[last_exit_index]["close"] * (1 - config.slippage_pct / 100)
    exit_reason = "期限決済"
    exit_index = last_exit_index

    for i in range(entry_index, last_exit_index + 1):
        bar = bars[i]
        hit_stop = bar["low"] <= stop_loss_price
        hit_take = bar["high"] >= take_profit_price
        if hit_stop and hit_take:
            exit_price = stop_loss_price * (1 - config.slippage_pct / 100)
            exit_reason = "損切り優先"
            exit_index = i
            break
        if hit_stop:
            exit_price = stop_loss_price * (1 - config.slippage_pct / 100)
            exit_reason = "損切り"
            exit_index = i
            break
        if hit_take:
            exit_price = take_profit_price * (1 - config.slippage_pct / 100)
            exit_reason = "利確"
            exit_index = i
            break

    return_pct = pct_change(exit_price, entry_price)
    return Trade(
        symbol=symbol,
        signal_date=signal["date"],
        entry_date=entry_bar["date"],
        exit_date=bars[exit_index]["date"],
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=return_pct,
        exit_reason=exit_reason,
        hold_days=exit_index - entry_index + 1,
        signal_change_pct=signal["change_pct"],
        signal_relative_volume=signal["relative_volume"],
        signal_gap_pct=signal["gap_pct"],
        signal_high_distance_pct=signal["high_distance_pct"],
    )


def write_trades(trades: list[Trade], output: Path, strategy: str = "runner") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if strategy == "position":
        signal_fields = [
            "signal_breakout_margin_pct",
            "signal_volume_ratio",
            "signal_rs_pct",
            "signal_base_depth_pct",
        ]
    else:
        signal_fields = [
            "signal_change_pct",
            "signal_relative_volume",
            "signal_gap_pct",
            "signal_high_distance_pct",
        ]
    fieldnames = [
        "symbol",
        "signal_date",
        "entry_date",
        "exit_date",
        "entry_price",
        "exit_price",
        "return_pct",
        "exit_reason",
        "hold_days",
    ] + signal_fields
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for trade in trades:
            row = {
                "symbol": trade.symbol,
                "signal_date": trade.signal_date,
                "entry_date": trade.entry_date,
                "exit_date": trade.exit_date,
                "entry_price": f"{trade.entry_price:.4f}",
                "exit_price": f"{trade.exit_price:.4f}",
                "return_pct": f"{trade.return_pct:.4f}",
                "exit_reason": trade.exit_reason,
                "hold_days": str(trade.hold_days),
            }
            signal_values = [
                f"{trade.signal_change_pct:.4f}",
                f"{trade.signal_relative_volume:.4f}",
                f"{trade.signal_gap_pct:.4f}",
                f"{trade.signal_high_distance_pct:.4f}",
            ]
            row.update(dict(zip(signal_fields, signal_values)))
            writer.writerow(row)


def summary_rows(trades: list[Trade], segment: str = "全期間") -> list[dict[str, str]]:
    if not trades:
        return [{"segment": segment, "metric": "取引数", "value": "0"}]
    returns = [trade.return_pct for trade in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / len(trades)
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    cumulative = 1.0
    for value in returns:
        cumulative *= 1 + value / 100
    sorted_returns = sorted(returns)
    rows = [
        {"metric": "取引数", "value": str(len(trades))},
        {"metric": "勝率", "value": f"{win_rate * 100:.2f}%"},
        {"metric": "平均リターン", "value": f"{statistics.mean(returns):.2f}%"},
        {"metric": "中央値リターン", "value": f"{statistics.median(returns):.2f}%"},
        {"metric": "勝ち平均", "value": f"{avg_win:.2f}%"},
        {"metric": "負け平均", "value": f"{avg_loss:.2f}%"},
        {"metric": "期待値(1取引あたり)", "value": f"{expectancy:.2f}%"},
        {"metric": "最大利益", "value": f"{max(returns):.2f}%"},
        {"metric": "最大損失", "value": f"{min(returns):.2f}%"},
        {"metric": "損益係数", "value": "N/A" if gross_loss == 0 else f"{gross_profit / gross_loss:.2f}"},
        {"metric": "下位25%リターン", "value": f"{sorted_returns[max(0, len(sorted_returns) // 4 - 1)]:.2f}%"},
        {"metric": "等金額・逐次運用リターン", "value": f"{(cumulative - 1) * 100:.2f}%"},
        {"metric": "平均保有日数", "value": f"{statistics.mean([trade.hold_days for trade in trades]):.2f}"},
    ]
    return [{"segment": segment, **row} for row in rows]


def write_summary(trades: list[Trade], output: Path, split_date: str | None = None) -> list[dict[str, str]]:
    rows = summary_rows(trades)
    if split_date:
        in_sample = [trade for trade in trades if trade.signal_date < split_date]
        out_sample = [trade for trade in trades if trade.signal_date >= split_date]
        rows += summary_rows(in_sample, segment=f"検証期間 (<{split_date})")
        rows += summary_rows(out_sample, segment=f"未知期間 (>={split_date})")
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["segment", "metric", "value"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def parse_symbols(args: argparse.Namespace) -> list[str]:
    symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
    if args.symbols_csv:
        with args.symbols_csv.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if "symbol" not in (reader.fieldnames or []):
                raise SystemExit("--symbols-csv must contain a symbol column")
            symbols.extend(row["symbol"].strip() for row in reader if row.get("symbol", "").strip())
    unique: list[str] = []
    seen = set()
    for symbol in symbols:
        if symbol not in seen:
            unique.append(symbol)
            seen.add(symbol)
    if not unique:
        raise SystemExit("Provide --symbols or --symbols-csv.")
    return unique


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="moomoo歴史K線で短期急騰戦略をバックテストします。")
    parser.add_argument("--host", default="127.0.0.1", help="OpenD host")
    parser.add_argument("--port", type=int, default=11111, help="OpenD quote port")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols, e.g. US.FAC,JP.6336")
    parser.add_argument("--symbols-csv", type=Path, help="CSV with a symbol column")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--strategy",
        choices=["runner", "position"],
        default="runner",
        help="runner=翌日反発狙いの急騰追随, position=ベース突破の中期トレンドフォロー",
    )
    parser.add_argument("--benchmark", default="", help="Relative strength benchmark, e.g. US.SPY / JP.1306 / HK.02800")
    parser.add_argument("--split-date", default="", help="Walk-forward split date YYYY-MM-DD (in-sample before, out-of-sample after)")
    parser.add_argument("--breakout-window", type=int, default=60, help="Position: prior-high lookback days")
    parser.add_argument("--base-window", type=int, default=35, help="Position: consolidation lookback days")
    parser.add_argument("--base-depth-max-pct", type=float, default=25.0, help="Position: max base depth percent")
    parser.add_argument("--min-volume-ratio", type=float, default=1.5, help="Position: breakout day volume vs 20d average")
    parser.add_argument("--rs-lookback", type=int, default=126, help="Position: relative strength lookback days")
    parser.add_argument("--trail-sma-window", type=int, default=50, help="Position: trailing exit SMA window")
    parser.add_argument("--position-stop-loss-pct", type=float, default=15.0, help="Position: hard stop percent")
    parser.add_argument("--position-max-hold-days", type=int, default=60, help="Position: max holding days")
    parser.add_argument("--position-take-profit-pct", type=float, default=0.0, help="Position: take-profit percent, 0 disables")
    parser.add_argument("--min-change-pct", type=float, default=8.0, help="Signal day minimum close-to-close rise")
    parser.add_argument("--min-relative-volume", type=float, default=2.0, help="Signal day minimum volume/average volume")
    parser.add_argument("--min-gap-pct", type=float, default=0.0, help="Signal day minimum open gap")
    parser.add_argument("--min-high-distance-pct", type=float, default=-5.0, help="Signal close vs prior rolling high")
    parser.add_argument("--avg-volume-window", type=int, default=20, help="Average volume lookback")
    parser.add_argument("--high-window", type=int, default=252, help="Prior high lookback")
    parser.add_argument("--take-profit-pct", type=float, default=30.0, help="Take-profit threshold")
    parser.add_argument("--stop-loss-pct", type=float, default=10.0, help="Stop-loss threshold")
    parser.add_argument("--max-hold-days", type=int, default=10, help="Max holding days")
    parser.add_argument("--slippage-pct", type=float, default=0.2, help="Slippage applied to entries and exits")
    parser.add_argument("--page-sleep", type=float, default=0.5, help="Seconds between paged API requests")
    parser.add_argument("--symbol-sleep", type=float, default=1.0, help="Seconds between symbols")
    parser.add_argument("--trades-output", type=Path, default=Path("outputs/moomoo_backtest_trades.csv"))
    parser.add_argument("--summary-output", type=Path, default=Path("outputs/moomoo_backtest_summary.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbols = parse_symbols(args)
    signal_config = SignalConfig(
        min_change_pct=args.min_change_pct,
        min_relative_volume=args.min_relative_volume,
        min_gap_pct=args.min_gap_pct,
        min_high_distance_pct=args.min_high_distance_pct,
        avg_volume_window=args.avg_volume_window,
        high_window=args.high_window,
    )
    trade_config = TradeConfig(
        take_profit_pct=args.take_profit_pct,
        stop_loss_pct=args.stop_loss_pct,
        max_hold_days=args.max_hold_days,
        slippage_pct=args.slippage_pct,
    )
    position_signal_config = PositionSignalConfig(
        breakout_window=args.breakout_window,
        base_window=args.base_window,
        base_depth_max_pct=args.base_depth_max_pct,
        min_volume_ratio=args.min_volume_ratio,
        avg_volume_window=args.avg_volume_window,
        rs_lookback=args.rs_lookback,
    )
    position_trade_config = PositionTradeConfig(
        stop_loss_pct=args.position_stop_loss_pct,
        trail_sma_window=args.trail_sma_window,
        max_hold_days=args.position_max_hold_days,
        slippage_pct=args.slippage_pct,
        take_profit_pct=args.position_take_profit_pct,
    )

    api = import_openapi()
    quote_ctx = api.OpenQuoteContext(host=args.host, port=args.port)
    trades: list[Trade] = []
    try:
        bench_lookup: dict[str, float] | None = None
        bench_dates: list[str] | None = None
        if args.strategy == "position" and args.benchmark:
            try:
                bench_bars = normalize_bars(
                    fetch_history(api, quote_ctx, args.benchmark, args.start, args.end, args.page_sleep)
                )
                bench_lookup = make_benchmark_lookup(bench_bars)
                bench_dates = sorted(bench_lookup)
                print(f"ベンチマーク {args.benchmark}: K線 {len(bench_bars)}本")
            except Exception as exc:
                print(f"ベンチマーク取得失敗、絶対リターンでRS判定します ({exc})")

        for symbol in symbols:
            try:
                bars = normalize_bars(fetch_history(api, quote_ctx, symbol, args.start, args.end, args.page_sleep))
                if args.strategy == "position":
                    signals = make_position_signals(bars, position_signal_config, bench_lookup, bench_dates)
                    symbol_trades = simulate_position_trades_no_overlap(
                        symbol, bars, signals, position_trade_config
                    )
                else:
                    signals = make_signals(bars, signal_config)
                    symbol_trades = [
                        trade
                        for signal in signals
                        if (trade := simulate_trade(symbol, bars, signal, trade_config)) is not None
                    ]
                trades.extend(symbol_trades)
                print(f"{symbol}: K線 {len(bars)}本, シグナル {len(signals)}件, 取引 {len(symbol_trades)}件")
            except Exception as exc:
                print(f"{symbol}: スキップ ({exc})")
            time.sleep(args.symbol_sleep)
    finally:
        quote_ctx.close()

    trades.sort(key=lambda trade: (trade.signal_date, trade.symbol))
    write_trades(trades, args.trades_output, strategy=args.strategy)
    rows = write_summary(trades, args.summary_output, split_date=args.split_date or None)
    print(f"取引明細CSV: {args.trades_output}")
    print(f"サマリーCSV: {args.summary_output}")
    for row in rows:
        print(f"[{row['segment']}] {row['metric']}: {row['value']}")


if __name__ == "__main__":
    main()
