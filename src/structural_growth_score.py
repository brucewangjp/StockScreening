#!/usr/bin/env python3
"""
US structural-growth lens.

For the US — the most efficient major market — government policy themes are
priced in fast via price (RS) and earnings (the fundamentals gate). So this
lens stays SMALL: a tech-adoption-cycle sector theme (≤10) plus a revenue
re-acceleration bonus (≤5), capped at 15. It is a separate structural-beta
score, never folded into the 0-100 buy score, used only as a tiebreaker /
tailwind tag.

Phase 2 (not implemented — needs data the free moomoo/Yahoo feed lacks):
earnings-estimate revision, institutional accumulation, short interest,
option-volume spike. Bridge these later via a manual CSV like fundamentals_us.csv.
"""

from __future__ import annotations

from pathlib import Path

from policy_theme_score import (
    LensResult,
    PolicyConfig,
    candidate_text,
    load_policy_config,
    score_policy_theme,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "us_structural_themes.yaml"
ACCEL_BONUS_MAX = 5

_US_CONFIG: PolicyConfig | None = None


def us_config() -> PolicyConfig:
    global _US_CONFIG
    if _US_CONFIG is None:
        _US_CONFIG = load_policy_config(CONFIG_PATH)
    return _US_CONFIG


def _get(candidate: object, key: str):
    if isinstance(candidate, dict):
        return candidate.get(key)
    return getattr(candidate, key, None)


def _revenue_accel_bonus(candidate: object) -> tuple[int, str]:
    """Bonus for re-accelerating revenue. accel_pp is quarter-over-quarter
    change in YoY growth (we already collect it for the fundamentals gate)."""
    accel = _get(candidate, "revenue_accel_pp")
    try:
        accel = float(accel)
    except (TypeError, ValueError):
        return 0, ""
    if accel <= 0:
        return 0, ""
    bonus = min(round(accel / 2), ACCEL_BONUS_MAX)
    if bonus <= 0:
        return 0, ""
    return bonus, f"売上再加速+{accel:.0f}pp(+{bonus})"


def score_us_lens(candidate: object) -> LensResult:
    theme = score_policy_theme(candidate_text(candidate), us_config())
    accel_bonus, accel_detail = _revenue_accel_bonus(candidate)
    total = min(theme.score + accel_bonus, us_config().total_cap + ACCEL_BONUS_MAX)

    if total == 0:
        return LensResult("US構造成長", 0, "", "", "", "")

    detail_parts = []
    if theme.score > 0:
        detail_parts.append(theme.reason)
    if accel_detail:
        detail_parts.append(accel_detail)
    main = theme.main_field if theme.main_field else "売上再加速のみ"
    return LensResult(
        lens_type="US構造成長",
        score=total,
        main=main,
        detail=" / ".join(detail_parts),
        flag="",
        keywords=theme.keywords_hit,
    )


if __name__ == "__main__":
    samples = [
        {"name": "NVIDIA", "industry": "Semiconductors", "themes": "AI data center GPU", "revenue_accel_pp": "30"},
        {"name": "Eli Lilly", "industry": "Pharmaceuticals", "themes": "GLP-1 obesity", "revenue_accel_pp": "8"},
        {"name": "CrowdStrike", "industry": "Software", "themes": "cybersecurity endpoint", "revenue_accel_pp": ""},
        {"name": "Some Bank", "industry": "Regional Banks", "themes": "", "revenue_accel_pp": ""},
        {"name": "Boring Co", "industry": "Industrials", "themes": "", "revenue_accel_pp": "12"},
    ]
    for sample in samples:
        result = score_us_lens(sample)
        print(f"{sample['name']:<14} score={result.score:>2} [{result.main or '該当なし'}] {result.detail}")
