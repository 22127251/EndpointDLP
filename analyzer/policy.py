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
    action: ActionType
    type: PolicyType
    patterns: list[str]      # non-empty when type == "regex"
    keywords: list[str]      # non-empty when type == "denylist"
    context_words: list[str]
    context_range: int


def load_policies(yaml_path: str | Path) -> list[Policy]:
    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    policies: list[Policy] = []
    for entry in raw.get("policies", []):
        policy_type: PolicyType = entry["type"]
        policies.append(Policy(
            id=entry["id"],
            name=entry["name"],
            channels=list(entry.get("channels", [])),
            action=entry["action"],
            type=policy_type,
            patterns=list(entry.get("patterns", [])),
            keywords=list(entry.get("keywords", [])),
            context_words=list(entry.get("context_words", [])),
            context_range=int(entry.get("context_range", 0)),
        ))
    return policies
