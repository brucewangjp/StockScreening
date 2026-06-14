#!/usr/bin/env python3
"""
HK China-policy + liquidity lens.

Hong Kong is effectively a China-policy market. The structural tailwind is the
15th Five-Year Plan (2026-2030) priority industries. But HK small caps are thin
and whippy, so "can I get out" matters as much as "will it go up" — liquidity
runs alongside as a WARNING FLAG (it does not lower the score).

theme score ≤12 (config). Liquidity tier comes from turnover (HKD); a thin tape
sets a flag only. Separate structural-beta score, never folded into the 0-100
buy score.

Phase 2 (not implemented — needs data the free feed lacks): southbound Stock
Connect flow, buyback amount, dividend yield. Bridge later via a manual CSV.
"""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore

from policy_theme_score import (
    LensResult,
    PolicyConfig,
    candidate_text,
    load_policy_config,
    score_policy_theme,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "hk_china_themes.yaml"

_HK_CONFIG: PolicyConfig | None = None
_HK_LIQUIDITY: dict | None = None


def hk_config() -> PolicyConfig:
    global _HK_CONFIG
    if _HK_CONFIG is None:
        _HK_CONFIG = load_policy_config(CONFIG_PATH)
    return _HK_CONFIG


def _liquidity_tiers() -> dict:
    global _HK_LIQUIDITY
    if _HK_LIQUIDITY is None:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        tiers = data.get("liquidity_tiers", {})
        _HK_LIQUIDITY = {
            "high_min": float(tiers.get("high_min", 50_000_000)),
            "mid_min": float(tiers.get("mid_min", 10_000_000)),
        }
    return _HK_LIQUIDITY


def _get(candidate: object, key: str):
    if isinstance(candidate, dict):
        return candidate.get(key)
    return getattr(candidate, key, None)


def _liquidity(candidate: object) -> tuple[str, str]:
    """Returns (tier_label, flag). flag is non-empty only when the tape is thin."""
    try:
        turnover = float(_get(candidate, "turnover"))
    except (TypeError, ValueError):
        return "不明", "流動性リスク: 売買代金未取得"
    tiers = _liquidity_tiers()
    if turnover >= tiers["high_min"]:
        return "厚い", ""
    if turnover >= tiers["mid_min"]:
        return "中", ""
    return "薄い", f"流動性リスク: 売買代金薄い({turnover:,.0f})"


def score_hk_lens(candidate: object) -> LensResult:
    theme = score_policy_theme(candidate_text(candidate), hk_config())
    tier_label, flag = _liquidity(candidate)

    detail_parts = []
    if theme.score > 0:
        detail_parts.append(theme.reason)
    detail_parts.append(f"流動性: {tier_label}")
    main = theme.main_field if theme.main_field else "政策テーマ該当なし"
    return LensResult(
        lens_type="HK中国政策",
        score=theme.score,
        main=main,
        detail=" / ".join(detail_parts),
        flag=flag,
        keywords=theme.keywords_hit,
    )


if __name__ == "__main__":
    samples = [
        {"name": "BYD", "industry": "Auto", "themes": "EV 新能源汽车 battery", "turnover": "800000000"},
        {"name": "SMIC", "industry": "Semiconductors", "themes": "半导体 国产化 芯片", "turnover": "300000000"},
        {"name": "EHang", "industry": "Aerospace", "themes": "低空经济 evtol 无人机", "turnover": "40000000"},
        {"name": "Thin SmallCap", "industry": "Machinery", "themes": "高端制造", "turnover": "3000000"},
        {"name": "Some HK Co", "industry": "Real Estate", "themes": "", "turnover": "60000000"},
    ]
    for sample in samples:
        result = score_hk_lens(sample)
        flag = f"  ⚠ {result.flag}" if result.flag else ""
        print(f"{sample['name']:<14} score={result.score:>2} [{result.main}] {result.detail}{flag}")
