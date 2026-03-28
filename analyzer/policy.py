"""
Policy dataclasses and YAML loader.

Policy types:
  - RegexPolicy:    one or more regex patterns; optional context words
  - DenylistPolicy: exact-match keyword list
  - NerEntityPolicy: NER entity type(s) from the Stanza model
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Union

import yaml

Action = Literal["block", "allow_log", "allow_no_log"]
Channel = Literal["clipboard", "browser", "peripheral"]


@dataclass
class RegexPattern:
    name: str
    regex: str
    score: float = 0.8


@dataclass
class RegexPolicy:
    id: str
    name: str
    channels: list[Channel]
    action: Action
    patterns: list[RegexPattern]
    context: list[str] = field(default_factory=list)


@dataclass
class DenylistPolicy:
    id: str
    name: str
    channels: list[Channel]
    action: Action
    deny_list: list[str]
    context: list[str] = field(default_factory=list)


@dataclass
class NerEntityPolicy:
    id: str
    name: str
    channels: list[Channel]
    action: Action
    entity_types: list[str]  # e.g. ["PERSON", "ORGANIZATION"]
    min_score: float = 0
    language: str | None = None  # None = applies to both en and vi


Policy = Union[RegexPolicy, DenylistPolicy, NerEntityPolicy]

_ACTION_RANK: dict[str, int] = {
    "block": 3,
    "allow_log": 2,
    "allow_no_log": 1,
}


def strongest_action(actions: list[Action]) -> Action:
    """Return the highest-priority action from a list."""
    if not actions:
        return "allow_no_log"
    return max(actions, key=lambda a: _ACTION_RANK.get(a, 0))


def load_policies(path: str) -> list[Policy]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Policy file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    policies: list[Policy] = []
    for raw in data.get("policies", []):
        policy_type = raw.get("type")
        common = dict(
            id=raw["id"],
            name=raw["name"],
            channels=raw.get("channels", []),
            action=raw.get("action", "allow_log"),
        )

        if policy_type == "regex":
            patterns = [
                RegexPattern(
                    name=p.get("name", ""),
                    regex=p["regex"],
                    score=float(p.get("score", 0.8)),
                )
                for p in raw.get("patterns", [])
            ]
            policies.append(RegexPolicy(
                **common,
                patterns=patterns,
                context=raw.get("context", []),
            ))

        elif policy_type == "denylist":
            policies.append(DenylistPolicy(
                **common,
                deny_list=raw.get("deny_list", []),
                context=raw.get("context", []),
            ))

        elif policy_type == "ner_entity":
            policies.append(NerEntityPolicy(
                **common,
                entity_types=raw.get("entity_types", []),
                min_score=float(raw.get("min_score", 0.7)),
                language=raw.get("language"),
            ))

        else:
            raise ValueError(f"Unknown policy type '{policy_type}' for policy id='{raw.get('id')}'")

    return policies
