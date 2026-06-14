#!/usr/bin/env python3
"""
Market-lens dispatcher.

Routes each candidate to the structural-tailwind lens for its market and
returns a uniform LensResult. The lens is a NON-MACRO structural-beta layer
kept separate from the 0-100 technical buy score — same philosophy across all
three markets, but a different lens per market:

  JP -> policy_theme_score.score_japan_lens      (government 17 strategic fields)
  US -> structural_growth_score.score_us_lens    (tech-cycle theme + revenue re-accel)
  HK -> china_policy_score.score_hk_lens          (China policy theme + liquidity flag)
"""

from __future__ import annotations

from china_policy_score import score_hk_lens
from policy_theme_score import EMPTY_LENS, LensResult, score_japan_lens
from structural_growth_score import score_us_lens

_DISPATCH = {
    "JP": score_japan_lens,
    "US": score_us_lens,
    "HK": score_hk_lens,
}


def score_market_lens(candidate: object) -> LensResult:
    market = candidate.get("market") if isinstance(candidate, dict) else getattr(candidate, "market", "")
    scorer = _DISPATCH.get(str(market).upper())
    if scorer is None:
        return EMPTY_LENS
    return scorer(candidate)


if __name__ == "__main__":
    samples = [
        {"market": "JP", "name": "三菱重工", "industry": "Aerospace & Defense", "themes": "Shipbuilding"},
        {"market": "US", "name": "NVIDIA", "industry": "Semiconductors", "themes": "AI data center", "revenue_accel_pp": "30"},
        {"market": "HK", "name": "BYD", "industry": "Auto", "themes": "EV 新能源", "turnover": "800000000"},
        {"market": "HK", "name": "Thin Co", "industry": "Machinery", "themes": "高端制造", "turnover": "2000000"},
    ]
    for sample in samples:
        result = score_market_lens(sample)
        flag = f"  ⚠ {result.flag}" if result.flag else ""
        print(f"[{sample['market']}] {sample['name']:<10} {result.lens_type} score={result.score:>2} [{result.main}]{flag}")
