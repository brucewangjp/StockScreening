#!/usr/bin/env python3
"""
Position/trend scanner for medium-term breakout candidates (weeks to months).

Institutional-style pipeline, applied per candidate row:
  1. Hard gates (any failure -> IGNORE with reason):
       liquidity, trend template (close > SMA50 > SMA200, SMA200 rising),
       52-week position (near high, well off low), positive relative strength,
       structural risk flags (OTC/SPAC etc.).
  2. Composite score 0-100 with explicit factor weights:
       relative strength 25, base quality 20, breakout confirmation 15,
       fundamentals 25 (revenue growth + acceleration), distance-to-high 10,
       catalyst 5.
  3. Status: ALERT = score >= 70 and breakout confirmed today,
             WATCH = score >= 55 (setup forming), otherwise IGNORE.

Policy theme (Japan 17 strategic fields) is computed as a SEPARATE 0-20
industry-beta score. It is NOT folded into the 0-100 buy score and never
changes status: a stock is promoted to ALERT only by trend/fundamentals/
volume, never by belonging to a government-backed field. The policy score
is used solely as a secondary sort key and a medium-term tailwind / 分散
flag. See src/policy_theme_score.py.

This tool ranks only. It does not place trades.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
try:
    from policy_theme_score import EMPTY_RESULT, load_policy_config, score_policy_theme
    _POLICY_AVAILABLE = True
except Exception as _policy_exc:  # noqa: BLE001 - policy layer is optional
    _POLICY_AVAILABLE = False
    _POLICY_IMPORT_ERROR = _policy_exc

ALERT_THRESHOLD = 70
WATCH_THRESHOLD = 55

WEIGHT_RS = 25
WEIGHT_BASE = 20
WEIGHT_BREAKOUT = 15
WEIGHT_FUNDAMENTALS = 25
WEIGHT_HIGH_DISTANCE = 10
WEIGHT_CATALYST = 5

CATALYST_KEYWORDS = (
    "earnings", "guidance", "upward revision", "fda", "clinical", "approval",
    "order", "contract", "m&a", "tob", "buyout",
    "決算", "上方修正", "承認", "臨床", "受注", "買収", "増配", "自社株買い",
)


@dataclass
class Candidate:
    symbol: str
    market: str
    name: str
    industry: str
    themes: str
    price: float
    market_cap: float
    turnover: float
    sma50: float
    sma200: float
    sma200_prev: float
    high_52w: float
    low_52w: float
    rs_6m_pct: float
    base_depth_pct: float
    base_len_days: float
    breakout_new_high: bool
    volume_ratio_20d: float
    atr14_pct: float | None
    lot_size: float | None
    revenue_growth_pct: float | None
    revenue_accel_pp: float | None
    catalyst: str
    risk_flags: str


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
            "symbol", "market", "price", "turnover",
            "sma50", "sma200", "sma200_prev", "high_52w", "low_52w",
            "rs_6m_pct", "base_depth_pct", "base_len_days",
            "breakout_new_high", "volume_ratio_20d",
        }
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"Missing required columns: {', '.join(sorted(missing))}")
        return [
            Candidate(
                symbol=row["symbol"].strip(),
                market=row["market"].strip(),
                name=row.get("name", "").strip(),
                industry=row.get("industry", "").strip(),
                themes=row.get("themes", "").strip(),
                price=parse_float(row.get("price")),
                market_cap=parse_float(row.get("market_cap")),
                turnover=parse_float(row.get("turnover")),
                sma50=parse_float(row.get("sma50")),
                sma200=parse_float(row.get("sma200")),
                sma200_prev=parse_float(row.get("sma200_prev")),
                high_52w=parse_float(row.get("high_52w")),
                low_52w=parse_float(row.get("low_52w")),
                rs_6m_pct=parse_float(row.get("rs_6m_pct")),
                base_depth_pct=parse_float(row.get("base_depth_pct")),
                base_len_days=parse_float(row.get("base_len_days")),
                breakout_new_high=parse_float(row.get("breakout_new_high")) >= 1,
                volume_ratio_20d=parse_float(row.get("volume_ratio_20d")),
                atr14_pct=parse_optional_float(row.get("atr14_pct")),
                lot_size=parse_optional_float(row.get("lot_size")),
                revenue_growth_pct=parse_optional_float(row.get("revenue_growth_pct")),
                revenue_accel_pp=parse_optional_float(row.get("revenue_accel_pp")),
                catalyst=row.get("catalyst", "").strip(),
                risk_flags=row.get("risk_flags", "").strip(),
            )
            for row in reader
            if row.get("symbol", "").strip()
        ]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def turnover_gate(candidate: Candidate) -> str | None:
    market = candidate.market.upper()
    minimums = {"US": 5_000_000, "HK": 10_000_000}
    minimum = minimums.get(market, 300_000_000)
    if candidate.turnover < minimum:
        return f"売買代金不足 ({candidate.turnover:,.0f})"
    return None


def hard_gates(candidate: Candidate) -> list[str]:
    failures: list[str] = []

    liquidity = turnover_gate(candidate)
    if liquidity:
        failures.append(liquidity)

    flags = candidate.risk_flags
    if flags and flags != "なし":
        if any(key in flags for key in ("OTC", "PINK", "SPAC", "シェル")):
            failures.append(f"構造リスク: {flags}")

    if not (candidate.price > candidate.sma50 > candidate.sma200 > 0):
        failures.append("トレンド不成立 (株価>50日線>200日線でない)")
    elif candidate.sma200 <= candidate.sma200_prev:
        failures.append("200日線が上向きでない")

    if candidate.high_52w > 0:
        off_high_pct = (candidate.price / candidate.high_52w - 1) * 100
        if off_high_pct < -25:
            failures.append(f"52週高値から{off_high_pct:.0f}%と遠い")
    else:
        failures.append("52週高値データなし")

    if candidate.low_52w > 0:
        above_low_pct = (candidate.price / candidate.low_52w - 1) * 100
        if above_low_pct < 30:
            failures.append(f"52週安値から+{above_low_pct:.0f}%のみ (上昇初動でない)")

    if candidate.rs_6m_pct <= 0:
        failures.append(f"相対強度マイナス ({candidate.rs_6m_pct:.1f}%)")

    return failures


def score_rs(candidate: Candidate) -> tuple[float, str | None]:
    points = clamp(candidate.rs_6m_pct, 0, 50) / 50 * WEIGHT_RS
    reason = f"6ヶ月相対強度 +{candidate.rs_6m_pct:.1f}%" if candidate.rs_6m_pct >= 10 else None
    return points, reason


def score_base(candidate: Candidate) -> tuple[float, str | None]:
    depth = candidate.base_depth_pct
    if depth <= 0:
        return 0.0, None
    if depth <= 15:
        depth_points = 12.0
    elif depth <= 25:
        depth_points = 8.0
    elif depth <= 35:
        depth_points = 3.0
    else:
        depth_points = 0.0
    length_points = clamp(candidate.base_len_days / 40, 0, 1) * 8
    points = depth_points + length_points
    reason = None
    if depth_points >= 8:
        reason = f"タイトなベース (深さ{depth:.0f}%・{candidate.base_len_days:.0f}日)"
    return points, reason


def score_breakout(candidate: Candidate) -> tuple[float, str | None]:
    if not candidate.breakout_new_high:
        return 0.0, None
    ratio = candidate.volume_ratio_20d
    if ratio >= 1.5:
        return float(WEIGHT_BREAKOUT), f"出来高{ratio:.1f}倍でブレイクアウト"
    if ratio >= 1.2:
        return 8.0, f"ブレイクアウト (出来高{ratio:.1f}倍とやや弱い)"
    return 4.0, "ブレイクアウト (出来高確認なし)"


def score_fundamentals(candidate: Candidate) -> tuple[float, str | None]:
    growth = candidate.revenue_growth_pct
    accel = candidate.revenue_accel_pp
    if growth is None:
        return 0.0, "売上データ未取得 (要手動確認)"
    growth_points = clamp(growth, 0, 40) / 40 * 15
    accel_points = clamp(accel, 0, 10) if accel is not None else 0.0
    points = growth_points + accel_points
    reason = None
    if growth >= 20:
        accel_part = f"・加速+{accel:.1f}pp" if accel is not None and accel > 0 else ""
        reason = f"売上成長+{growth:.0f}%{accel_part}"
    return points, reason


def score_high_distance(candidate: Candidate) -> tuple[float, str | None]:
    if candidate.high_52w <= 0:
        return 0.0, None
    off_high_pct = (candidate.price / candidate.high_52w - 1) * 100
    if off_high_pct >= -5:
        return float(WEIGHT_HIGH_DISTANCE), "52週高値圏"
    if off_high_pct >= -15:
        return 6.0, None
    return 2.0, None


def score_catalyst(candidate: Candidate) -> tuple[float, str | None]:
    text = candidate.catalyst.lower()
    if not text:
        return 0.0, None
    if any(keyword in text for keyword in CATALYST_KEYWORDS):
        return float(WEIGHT_CATALYST), f"材料: {candidate.catalyst}"
    return 2.0, f"材料(未分類): {candidate.catalyst}"


def score(candidate: Candidate) -> tuple[int, str, list[str]]:
    gate_failures = hard_gates(candidate)
    if gate_failures:
        return 0, "IGNORE", gate_failures

    reasons: list[str] = []
    total = 0.0
    for scorer in (
        score_rs,
        score_base,
        score_breakout,
        score_fundamentals,
        score_high_distance,
        score_catalyst,
    ):
        points, reason = scorer(candidate)
        total += points
        if reason:
            reasons.append(reason)

    total_int = int(clamp(total, 0, 100))
    if total_int >= ALERT_THRESHOLD and candidate.breakout_new_high:
        status = "ALERT"
    elif total_int >= WATCH_THRESHOLD:
        status = "WATCH" if candidate.breakout_new_high else "SETUP"
    else:
        status = "IGNORE"
    if status == "SETUP":
        reasons.append("条件成立・ブレイクアウト待ち")
    return total_int, status, reasons


def _load_policy():
    if not _POLICY_AVAILABLE:
        print(
            f"政策テーマ層を読み込めませんでした（産業ベータ加点をスキップ）: {_POLICY_IMPORT_ERROR}",
            file=sys.stderr,
        )
        return None
    try:
        return load_policy_config()
    except Exception as exc:  # noqa: BLE001
        print(f"政策テーマconfig読み込み失敗（スキップ）: {exc}", file=sys.stderr)
        return None


def rank(candidates: Iterable[Candidate]) -> list[dict[str, str]]:
    policy_config = _load_policy()
    rows = []
    for candidate in candidates:
        total, status, reasons = score(candidate)
        off_high = (
            (candidate.price / candidate.high_52w - 1) * 100 if candidate.high_52w > 0 else 0.0
        )
        if policy_config is not None:
            policy = score_policy_theme(
                f"{candidate.industry} {candidate.themes} {candidate.name}", policy_config
            )
        else:
            policy = EMPTY_RESULT if _POLICY_AVAILABLE else None
        rows.append(
            {
                "status": status,
                "score": str(total),
                "symbol": candidate.symbol,
                "market": candidate.market,
                "name": candidate.name,
                "industry": candidate.industry,
                "themes": candidate.themes,
                "policy_theme_score": "" if policy is None else str(policy.score),
                "policy_theme_main": "" if policy is None else policy.main_field,
                "policy_theme_rank": "" if policy is None else policy.rank,
                "policy_theme_sub": "" if policy is None else policy.sub_fields,
                "policy_theme_reason": "" if policy is None else policy.reason,
                "policy_theme_keywords_hit": "" if policy is None else policy.keywords_hit,
                "price": f"{candidate.price:.2f}",
                "turnover": f"{candidate.turnover:.0f}",
                "rs_6m_pct": f"{candidate.rs_6m_pct:.2f}",
                "off_52w_high_pct": f"{off_high:.2f}",
                "base_depth_pct": f"{candidate.base_depth_pct:.2f}",
                "base_len_days": f"{candidate.base_len_days:.0f}",
                "breakout_new_high": "1" if candidate.breakout_new_high else "0",
                "volume_ratio_20d": f"{candidate.volume_ratio_20d:.2f}",
                "atr14_pct": "" if candidate.atr14_pct is None else f"{candidate.atr14_pct:.2f}",
                "lot_size": "" if candidate.lot_size is None else f"{candidate.lot_size:.0f}",
                "revenue_growth_pct": (
                    "" if candidate.revenue_growth_pct is None else f"{candidate.revenue_growth_pct:.2f}"
                ),
                "revenue_accel_pp": (
                    "" if candidate.revenue_accel_pp is None else f"{candidate.revenue_accel_pp:.2f}"
                ),
                "risk_flags": candidate.risk_flags or "なし",
                "reasons": "; ".join(reasons),
            }
        )
    order = {"ALERT": 0, "WATCH": 1, "SETUP": 2, "IGNORE": 3}

    def policy_value(row: dict[str, str]) -> int:
        raw = row.get("policy_theme_score", "")
        return int(raw) if raw else 0

    # 並び順: ステータス -> 技術スコア -> 政策テーマスコア(同点時の分散優先のタイブレーク)
    # 政策スコアは技術スコアの「後」に効く。買い判定はあくまで技術スコアが主。
    return sorted(
        rows,
        key=lambda row: (order.get(row["status"], 9), -int(row["score"]), -policy_value(row)),
    )


def write_rows(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "status", "score", "symbol", "market", "name", "industry", "themes",
        "policy_theme_score", "policy_theme_main", "policy_theme_rank",
        "policy_theme_sub", "policy_theme_reason", "policy_theme_keywords_hit",
        "price", "turnover", "rs_6m_pct", "off_52w_high_pct",
        "base_depth_pct", "base_len_days", "breakout_new_high", "volume_ratio_20d",
        "atr14_pct", "lot_size", "revenue_growth_pct", "revenue_accel_pp", "risk_flags", "reasons",
    ]
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank medium-term breakout candidates.")
    parser.add_argument("input_csv", type=Path, help="CSV produced by moomoo_openapi_screener.py --mode position")
    parser.add_argument(
        "-o", "--output", type=Path,
        default=Path("outputs/position_candidates.csv"), help="Output CSV path",
    )
    parser.add_argument("--alerts-only", action="store_true", help="Write only ALERT/WATCH/SETUP rows")
    args = parser.parse_args()

    rows = rank(load_candidates(args.input_csv))
    if args.alerts_only:
        rows = [row for row in rows if row["status"] != "IGNORE"]
    write_rows(rows, args.output)

    for row in rows[:20]:
        policy = row.get("policy_theme_score", "")
        policy_tag = (
            f"政策{policy}[{row.get('policy_theme_main', '')}] " if policy and policy != "0" else ""
        )
        print(
            f"{row['status']:6} {row['score']:>3} {row['symbol']:<10} "
            f"RS={row['rs_6m_pct']:>7}% 高値比={row['off_52w_high_pct']:>7}% "
            f"ベース深さ={row['base_depth_pct']:>6}% 突破={row['breakout_new_high']} "
            f"{policy_tag}{row['reasons']}"
        )


if __name__ == "__main__":
    main()
