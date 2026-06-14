#!/usr/bin/env python3
"""
Policy theme scoring against Japan's 17 strategic growth fields.

IMPORTANT: This is an INDUSTRY-BETA layer, not a buy signal. The score it
produces is kept SEPARATE from the technical/fundamental score that drives
ALERT/WATCH/IGNORE. A stock belonging to a government-backed field gets a
tailwind flag for medium-term holding and diversification prioritization,
but the buy decision is still made purely on trend, fundamentals, volume,
and valuation. A downtrending or illiquid stock is never promoted to BUY by
this score.

Score (max 20):
  base_score:  A=10, B=6, C=3
  rank_bonus:  A=+5, B=+2, C=+0
  keyword_hit: +1 per matched keyword, capped at +5
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


def _keyword_matches(keyword: str, text: str) -> bool:
    """ASCII keywords use word-boundary matching to avoid substring false
    positives (e.g. 'ip' in 'shipbuilding', 'ai' in 'entertainment').
    Japanese keywords have no word boundaries, so they use substring matching."""
    if keyword.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    return keyword in text

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "japan_growth_strategy_17fields.yaml"


@dataclass(frozen=True)
class PolicyField:
    name: str
    rank: str
    keywords: tuple[str, ...]
    budget_tier: str = "計画"
    budget_note: str = ""


@dataclass(frozen=True)
class PolicyConfig:
    base_score: dict[str, int]
    rank_bonus: dict[str, int]
    keyword_hit_bonus_per_hit: int
    keyword_hit_bonus_max: int
    budget_bonus: dict[str, int]
    total_cap: int
    fields: tuple[PolicyField, ...]


@dataclass(frozen=True)
class PolicyResult:
    score: int
    main_field: str
    rank: str
    sub_fields: str
    reason: str
    keywords_hit: str
    budget_tier: str = ""
    budget_note: str = ""


EMPTY_RESULT = PolicyResult(0, "", "", "", "", "", "", "")


@dataclass(frozen=True)
class LensResult:
    """Uniform result returned by every market lens (JP/US/HK).

    Kept SEPARATE from the 0-100 technical buy score. score is a 0-N
    structural-beta value; flag is a non-scoring warning (e.g. HK liquidity).
    """

    lens_type: str
    score: int
    main: str
    detail: str
    flag: str
    keywords: str


EMPTY_LENS = LensResult("", 0, "", "", "", "")


def candidate_text(candidate: object) -> str:
    """industry + themes + name from a Candidate-like object or dict."""
    if isinstance(candidate, dict):
        get = candidate.get
    else:
        get = lambda key, default="": getattr(candidate, key, default)  # noqa: E731
    return f"{get('industry', '')} {get('themes', '')} {get('name', '')}"


_JP_CONFIG: PolicyConfig | None = None


def japan_config() -> PolicyConfig:
    global _JP_CONFIG
    if _JP_CONFIG is None:
        _JP_CONFIG = load_policy_config(DEFAULT_CONFIG)
    return _JP_CONFIG


def score_japan_lens(candidate: object) -> LensResult:
    result = score_policy_theme(candidate_text(candidate), japan_config())
    if result.score == 0:
        return LensResult("JP政策17分野", 0, "", "", "", "")
    return LensResult(
        lens_type="JP政策17分野",
        score=result.score,
        main=f"{result.main_field}(予算{result.budget_tier})",
        detail=result.reason,
        flag="",
        keywords=result.keywords_hit,
    )


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError:
        # PyYAMLが無い環境向けフォールバック: 同名の .json があれば使う
        json_path = path.with_suffix(".json")
        if json_path.exists():
            return json.loads(json_path.read_text(encoding="utf-8"))
        raise SystemExit(
            "PyYAML未導入かつ .json フォールバックも無し。`pip install pyyaml` または "
            f"{json_path} を用意してください。"
        )


def load_policy_config(path: Path | None = None) -> PolicyConfig:
    path = path or DEFAULT_CONFIG
    data = _load_yaml(path)
    scoring = data.get("scoring", {})
    fields = tuple(
        PolicyField(
            name=str(item["name"]),
            rank=str(item.get("rank", "C")).upper(),
            keywords=tuple(str(k).lower() for k in item.get("keywords", [])),
            budget_tier=str(item.get("budget_tier", "計画")),
            budget_note=str(item.get("budget_note", "")),
        )
        for item in data.get("fields", [])
    )
    return PolicyConfig(
        base_score=dict(scoring.get("base_score", {"A": 10, "B": 6, "C": 3})),
        rank_bonus=dict(scoring.get("rank_bonus", {"A": 5, "B": 2, "C": 0})),
        keyword_hit_bonus_per_hit=int(scoring.get("keyword_hit_bonus_per_hit", 1)),
        keyword_hit_bonus_max=int(scoring.get("keyword_hit_bonus_max", 5)),
        budget_bonus=dict(scoring.get("budget_bonus", {"確定": 3, "具体化": 1, "計画": 0})),
        total_cap=int(scoring.get("total_cap", 20)),
        fields=fields,
    )


def _rank_order(rank: str) -> int:
    return {"A": 0, "B": 1, "C": 2}.get(rank, 3)


def score_policy_theme(text: str, config: PolicyConfig) -> PolicyResult:
    """Match a candidate's industry/themes/name text against the 17 fields.

    The main field is the highest-ranked field with the most keyword hits.
    All matched fields are reported in sub_fields. Score uses only the main
    field's rank, plus a keyword-hit bonus from the main field's matches.
    """
    normalized = text.lower()
    matches: list[tuple[PolicyField, list[str]]] = []
    for field in config.fields:
        hits = [kw for kw in field.keywords if kw and _keyword_matches(kw, normalized)]
        if hits:
            matches.append((field, hits))

    if not matches:
        return EMPTY_RESULT

    # 主分野 = ランクが高く、ヒット数が多い分野
    matches.sort(key=lambda m: (_rank_order(m[0].rank), -len(m[1])))
    main_field, main_hits = matches[0]

    base = config.base_score.get(main_field.rank, 0)
    bonus = config.rank_bonus.get(main_field.rank, 0)
    keyword_bonus = min(
        len(main_hits) * config.keyword_hit_bonus_per_hit,
        config.keyword_hit_bonus_max,
    )
    budget_bonus = config.budget_bonus.get(main_field.budget_tier, 0)
    total = min(base + bonus + keyword_bonus + budget_bonus, config.total_cap)

    sub_fields = " / ".join(field.name for field, _ in matches[1:]) if len(matches) > 1 else ""
    all_hits: list[str] = []
    for _, hits in matches:
        for hit in hits:
            if hit not in all_hits:
                all_hits.append(hit)
    reason = (
        f"政策テーマ「{main_field.name}」(ランク{main_field.rank}/予算{main_field.budget_tier}) "
        f"基礎{base}+ランク{bonus}+キーワード{keyword_bonus}+予算{budget_bonus}"
    )
    return PolicyResult(
        score=total,
        main_field=main_field.name,
        rank=main_field.rank,
        sub_fields=sub_fields,
        reason=reason,
        keywords_hit=", ".join(all_hits),
        budget_tier=main_field.budget_tier,
        budget_note=main_field.budget_note,
    )


if __name__ == "__main__":
    config = load_policy_config()
    samples = [
        ("Mitsubishi Heavy Industries", "Aerospace & Defense / Shipbuilding heavy industries"),
        ("Toei Animation", "Content anime entertainment"),
        ("Horiba", "Precision Instruments / 半導体 検査装置"),
        ("Some Bank", "Regional Bank"),
        ("Quantum Startup", "quantum computing"),
    ]
    for name, text in samples:
        result = score_policy_theme(f"{name} {text}", config)
        print(
            f"{name:<32} score={result.score:>2} field={result.main_field or '該当なし'} "
            f"({result.rank}/予算{result.budget_tier or '-'}) hits=[{result.keywords_hit}]"
        )
