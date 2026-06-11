#!/usr/bin/env python3
"""
Short-term runner stock scanner.

This tool ranks stocks that may have short-term momentum potential. It does not
place trades. Use it as an alert/watchlist layer before checking charts, news,
and liquidity in moomoo or SBI Securities.
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CATALYST_KEYWORDS = {
    "earnings": 18,
    "guidance": 18,
    "upward revision": 18,
    "fda": 18,
    "clinical": 18,
    "approval": 16,
    "ai": 14,
    "semiconductor": 14,
    "defense": 14,
    "order": 14,
    "contract": 14,
    "m&a": 16,
    "tob": 16,
    "buyout": 16,
    "short squeeze": 16,
    "決算": 18,
    "上方修正": 18,
    "承認": 16,
    "臨床": 18,
    "半導体": 14,
    "防衛": 14,
    "受注": 14,
    "買収": 16,
}


@dataclass
class Candidate:
    symbol: str
    market: str
    name: str
    exchange: str
    industry: str
    themes: str
    price: float
    market_cap: float
    volume: float
    turnover: float
    avg_volume_20d: float
    change_pct: float
    distance_to_52w_high_pct: float
    gap_pct: float
    catalyst: str
    float_shares: float | None
    short_interest_pct: float | None
    risk_flags: str

    @property
    def relative_volume(self) -> float:
        if self.avg_volume_20d <= 0:
            return 0.0
        return self.volume / self.avg_volume_20d


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


def parse_optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    parsed = parse_float(value, default=math.nan)
    return None if math.isnan(parsed) else parsed


def load_candidates(path: Path) -> list[Candidate]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {
            "symbol",
            "market",
            "price",
            "market_cap",
            "volume",
            "avg_volume_20d",
            "change_pct",
            "distance_to_52w_high_pct",
            "gap_pct",
            "catalyst",
        }
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"Missing required columns: {', '.join(sorted(missing))}")

        return [
            Candidate(
                symbol=row["symbol"].strip(),
                market=row["market"].strip(),
                name=row.get("name", "").strip(),
                exchange=row.get("exchange", "").strip(),
                industry=row.get("industry", "").strip(),
                themes=row.get("themes", "").strip(),
                price=parse_float(row.get("price")),
                market_cap=parse_float(row.get("market_cap")),
                volume=parse_float(row.get("volume")),
                turnover=parse_float(row.get("turnover")),
                avg_volume_20d=parse_float(row.get("avg_volume_20d")),
                change_pct=parse_float(row.get("change_pct")),
                distance_to_52w_high_pct=parse_float(row.get("distance_to_52w_high_pct")),
                gap_pct=parse_float(row.get("gap_pct")),
                catalyst=row.get("catalyst", "").strip(),
                float_shares=parse_optional_float(row.get("float_shares")),
                short_interest_pct=parse_optional_float(row.get("short_interest_pct")),
                risk_flags=row.get("risk_flags", "").strip(),
            )
            for row in reader
            if row.get("symbol", "").strip()
        ]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def catalyst_score(text: str) -> int:
    normalized = text.lower()
    score = 0
    for keyword, points in CATALYST_KEYWORDS.items():
        if keyword.lower() in normalized:
            score = max(score, points)
    return score


def market_cap_score(candidate: Candidate) -> int:
    cap = candidate.market_cap
    market = candidate.market.upper()
    if market in {"US", "USA", "NASDAQ", "NYSE", "AMEX"}:
        if 50_000_000 <= cap <= 2_000_000_000:
            return 15
        if 2_000_000_000 < cap <= 5_000_000_000:
            return 7
        return 0
    if market in {"HK", "HKG"}:
        if 300_000_000 <= cap <= 30_000_000_000:
            return 15
        if 30_000_000_000 < cap <= 100_000_000_000:
            return 7
        return 0
    if 5_000_000_000 <= cap <= 100_000_000_000:
        return 15
    if 100_000_000_000 < cap <= 300_000_000_000:
        return 7
    return 0


def turnover_score(candidate: Candidate) -> tuple[int, str | None]:
    turnover = candidate.turnover
    market = candidate.market.upper()
    if turnover <= 0:
        return -6, "売買代金が未取得"
    if market in {"US", "USA", "NASDAQ", "NYSE", "AMEX"}:
        if turnover >= 10_000_000:
            return 12, "売買代金が十分"
        if turnover >= 2_000_000:
            return 5, "最低限の売買代金"
        return -10, "売買代金が薄い"
    if market in {"HK", "HKG"}:
        if turnover >= 20_000_000:
            return 12, "売買代金が十分"
        if turnover >= 5_000_000:
            return 5, "最低限の売買代金"
        return -10, "売買代金が薄い"
    if turnover >= 500_000_000:
        return 12, "売買代金が十分"
    if turnover >= 100_000_000:
        return 5, "最低限の売買代金"
    return -10, "売買代金が薄い"


def risk_penalty(candidate: Candidate) -> tuple[int, list[str]]:
    flags = candidate.risk_flags
    if not flags or flags == "なし":
        return 0, []
    penalty = 0
    reasons = []
    if "OTC" in flags or "PINK" in flags:
        penalty -= 25
        reasons.append("OTC/PINKリスク")
    if "SPAC" in flags or "シェル" in flags:
        penalty -= 20
        reasons.append("SPAC/シェルリスク")
    if "ユニット" in flags:
        penalty -= 12
        reasons.append("ユニット株リスク")
    if "ワラント" in flags:
        penalty -= 15
        reasons.append("ワラントリスク")
    if "優先株" in flags:
        penalty -= 10
        reasons.append("優先株リスク")
    return penalty, reasons


def theme_score(candidate: Candidate) -> tuple[int, str | None]:
    themes = candidate.themes
    industry = candidate.industry
    if "未取得" in f"{themes}{industry}" or not f"{themes}{industry}".strip():
        return 0, None
    if any(keyword in themes for keyword in ["注目株", "前日上昇率上位", "グロース", "IPO"]):
        return 5, "テーマ/業界にも資金流入"
    return 2, "業界分類あり"


def score(candidate: Candidate) -> tuple[int, list[str]]:
    reasons: list[str] = []
    points = 0

    rv = candidate.relative_volume
    rv_points = int(clamp((rv - 1) * 10, 0, 25))
    points += rv_points
    if rv >= 2:
        reasons.append(f"相対出来高 {rv:.1f}倍")

    t_points, t_reason = turnover_score(candidate)
    points += t_points
    if t_reason:
        reasons.append(t_reason)

    cap_points = market_cap_score(candidate)
    points += cap_points
    if cap_points >= 15:
        reasons.append("小型/中型株の急騰候補レンジ")

    if 1 <= candidate.price <= 20:
        points += 10
        reasons.append("急騰しやすい価格帯")
    elif 20 < candidate.price <= 50:
        points += 5

    if candidate.change_pct >= 8:
        momentum_points = int(clamp(candidate.change_pct, 0, 20))
        points += momentum_points
        reasons.append(f"当日上昇率が強い {candidate.change_pct:.1f}%")

    if candidate.distance_to_52w_high_pct >= -5:
        points += 12
        reasons.append("52週高値に接近")
    elif candidate.distance_to_52w_high_pct >= -15:
        points += 6

    if candidate.gap_pct >= 5:
        points += int(clamp(candidate.gap_pct, 0, 12))
        reasons.append(f"ギャップアップ {candidate.gap_pct:.1f}%")

    c_points = catalyst_score(candidate.catalyst)
    points += c_points
    if c_points:
        reasons.append(f"材料: {candidate.catalyst}")

    if candidate.float_shares is not None and candidate.float_shares <= 50_000_000:
        points += 8
        reasons.append("浮動株が少ない")

    if candidate.short_interest_pct is not None and candidate.short_interest_pct >= 15:
        points += 8
        reasons.append(f"空売り比率 {candidate.short_interest_pct:.1f}%")

    theme_points, theme_reason = theme_score(candidate)
    points += theme_points
    if theme_reason:
        reasons.append(theme_reason)

    penalty, penalty_reasons = risk_penalty(candidate)
    points += penalty
    reasons.extend(penalty_reasons)

    return int(clamp(points, 0, 100)), reasons


def rank(candidates: Iterable[Candidate]) -> list[dict[str, str]]:
    rows = []
    for candidate in candidates:
        total, reasons = score(candidate)
        if total >= 75:
            status = "ALERT"
        elif total >= 60:
            status = "WATCH"
        else:
            status = "IGNORE"
        rows.append(
            {
                "status": status,
                "score": str(total),
                "symbol": candidate.symbol,
                "market": candidate.market,
                "name": candidate.name,
                "exchange": candidate.exchange,
                "industry": candidate.industry,
                "themes": candidate.themes,
                "price": f"{candidate.price:.2f}",
                "turnover": f"{candidate.turnover:.0f}",
                "change_pct": f"{candidate.change_pct:.2f}",
                "relative_volume": f"{candidate.relative_volume:.2f}",
                "distance_to_52w_high_pct": f"{candidate.distance_to_52w_high_pct:.2f}",
                "gap_pct": f"{candidate.gap_pct:.2f}",
                "risk_flags": candidate.risk_flags or "なし",
                "reasons": "; ".join(reasons),
            }
        )
    return sorted(rows, key=lambda row: int(row["score"]), reverse=True)


def write_rows(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "status",
        "score",
        "symbol",
        "market",
        "name",
        "exchange",
        "industry",
        "themes",
        "price",
        "turnover",
        "change_pct",
        "relative_volume",
        "distance_to_52w_high_pct",
        "gap_pct",
        "risk_flags",
        "reasons",
    ]
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank short-term momentum candidates.")
    parser.add_argument("input_csv", type=Path, help="CSV exported from moomoo/SBI/watchlist data")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("outputs/runner_candidates.csv"),
        help="Output CSV path",
    )
    parser.add_argument(
        "--alerts-only",
        action="store_true",
        help="Write only ALERT/WATCH rows",
    )
    args = parser.parse_args()

    rows = rank(load_candidates(args.input_csv))
    if args.alerts_only:
        rows = [row for row in rows if row["status"] in {"ALERT", "WATCH"}]
    write_rows(rows, args.output)

    for row in rows[:20]:
        print(
            f"{row['status']:6} {row['score']:>3} {row['symbol']:<10} "
            f"上昇率={row['change_pct']:>6}% 相対出来高={row['relative_volume']:>5} "
            f"売買代金={row['turnover']} "
            f"{row['reasons']}"
        )


if __name__ == "__main__":
    main()
