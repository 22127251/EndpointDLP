from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

ActionType = Literal["block", "allow_log", "allow"]
PolicyType = Literal["regex", "denylist"]

ACTION_RANK: dict[str, int] = {"block": 2, "allow_log": 1, "allow": 0}


@dataclass
class Policy:
    id: str
    name: str
    channels: list[str]
    type: PolicyType
    patterns: list[str]      # non-empty when type == "regex"
    keywords: list[str]      # non-empty when type == "denylist"
    context_words: list[str]
    context_range: int
    # --- Confidence-scoring (the ONLY action mechanism) ------------------
    # A shape match is always detected + counted. Context (a context_word found
    # nearby/in-cell/header/row) RAISES the score; it never drops a match. The
    # `actions` ladder maps the resulting score → action. "Require context" is
    # expressed as a ladder whose no-context band resolves to "allow" (no-op).
    score_base: float = 0.5
    score_context_boost: float = 0.5
    actions: list[tuple[float, str]] = field(default_factory=list)  # (min_score, action), high→low

    def resolve_action(self, score: float) -> str:
        """Map a confidence score to an action via the threshold ladder.
        Returns "allow" when no threshold matches (ladders normally include a
        min_score: 0.0 floor)."""
        for min_score, act in self.actions:  # sorted high→low at load time
            if score >= min_score:
                return act
        return "allow"


def load_policies(yaml_path: str | Path) -> list[Policy]:
    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    policies: list[Policy] = []
    for entry in raw.get("policies", []):
        actions_raw = entry.get("actions") or []
        actions = sorted(
            ((float(a["min_score"]), str(a["action"])) for a in actions_raw),
            key=lambda t: t[0],
            reverse=True,
        )
        if not actions:
            actions = [(0.0, "allow")]  # no-op floor for a misconfigured policy
        policies.append(Policy(
            id=entry["id"],
            name=entry["name"],
            channels=list(entry.get("channels", [])),
            type=entry["type"],
            patterns=list(entry.get("patterns", [])),
            keywords=list(entry.get("keywords", [])),
            context_words=list(entry.get("context_words", [])),
            context_range=int(entry.get("context_range", 0)),
            score_base=float(entry.get("score_base", 0.5)),
            score_context_boost=float(entry.get("score_context_boost", 0.5)),
            actions=actions,
        ))
    return policies
