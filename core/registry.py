"""Agent registry.

This is where you register more agents. Add a file under agents/, import the
class, list it below -- that is all. The interface picks it up automatically.
"""

from __future__ import annotations
from agents.packaging_designer import PackagingDesigner

_CLASSES = [
    PackagingDesigner,
    # Add more agents here, for example:
    # from agents.label_compliance import LabelCompliance
    # LabelCompliance,
]

AGENTS = {}
for cls in _CLASSES:
    AGENTS[cls.key] = cls()


def get(key: str):
    return AGENTS.get(key) or AGENTS.get("packaging_designer")


def catalog() -> list:
    return [
        {
            "key": agent.key,
            "title": agent.title,
            "description": agent.description,
            "icon": agent.icon,
            "greeting": agent.greeting,
        }
        for agent in AGENTS.values()
    ]
