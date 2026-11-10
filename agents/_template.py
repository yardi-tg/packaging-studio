"""TEMPLATE for another agent.

This is how you add a second, third, fourth agent to the studio. The one below
works as-is; it just isn't switched on yet.

TO SWITCH IT ON
---------------
1. Copy this file and rename it, e.g.  agents/brand_strategist.py
2. Rename and adapt the class at the bottom
3. Register it in core/registry.py:

       from agents.brand_strategist import BrandStrategist

       _CLASSES = [
           PackagingDesigner,
           BrandStrategist,        # <- new
       ]

4. Restart the server. The agent shows up in the interface automatically.

WHAT AN AGENT NEEDS
-------------------
  system_prompt()         Who it is and how it works. This is where the real
                          quality lives. Be specific, give it rules, describe
                          the workflow you want. Keep it FIXED: no project data
                          in here (see core/agent.py for why).
  state_block(project)    What the project looks like right now. Attached to
                          every new message, so it is always current.
  tools()                 What it can do. Each tool is a Python function with a
                          JSON schema. The function receives the project, the
                          parameters and `emit` (for live updates to the
                          interface) and returns a dict that Claude reads back
                          as the tool result.

Anything a tool writes into `project` is saved automatically and is visible to
every agent working on the same project. So agents can build on each other:
one researches, the next designs.
"""

from __future__ import annotations
from core import store
from core.agent import Agent, Tool


# --- tools ----------------------------------------------------------------

def _t_remember(project, params, emit):
    """Example tool: stores something on the project."""
    notes = project.setdefault("notes", {})
    notes[params["topic"]] = params["content"]
    store.log(project, "note", params["topic"])
    emit({"type": "project_update", "field": "notes", "value": notes})
    return {"ok": True, "saved": params["topic"]}


def _t_read(project, params, emit):
    """Example tool: reads what other agents have left behind."""
    return {
        "brief": project.get("brief") or {},
        "copy": project.get("copy") or {},
        "notes": project.get("notes") or {},
        "structure": (project.get("dieline") or {}).get("box_name"),
    }


# --- the agent ------------------------------------------------------------

class Template(Agent):
    key = "template"                    # unique, lowercase, no spaces
    title = "Template"                  # shown in the interface
    icon = "star"
    description = "Example. Copy it, rebuild it, register it in registry.py."
    greeting = "What should I start with?"

    def system_prompt(self) -> str:
        return """You are a specialist working inside a packaging studio.

Reply in whatever language the user writes in. Be brief. Make decisions rather
than asking a lot of questions.

Each user message carries a <project_state> block with the current brief and
structure. read_project shows what the other agents have worked out in detail.
remember stores your results so they are not lost.
"""

    def state_block(self, project: dict) -> str:
        brief = project.get("brief") or {}
        return (f"Product: {brief.get('product_name') or 'not known yet'}\n"
                f"Brand: {brief.get('brand') or 'not known yet'}\n"
                f"Structure: {(project.get('dieline') or {}).get('box_name') or 'none yet'}")

    def tools(self) -> list:
        return [
            Tool(
                "read_project",
                "Show the project's brief, copy and notes.",
                {"type": "object", "properties": {}},
                _t_read, "Reading project",
            ),
            Tool(
                "remember",
                "Store a result permanently on the project.",
                {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string",
                                  "description": "short key, e.g. 'name ideas'"},
                        "content": {"type": "string"},
                    },
                    "required": ["topic", "content"],
                },
                _t_remember, "Saving note",
            ),
        ]
