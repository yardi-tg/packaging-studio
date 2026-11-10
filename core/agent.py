"""Agent scaffolding: a tool loop against the Anthropic Messages API.

An agent is two things:
  * a system prompt (who it is, how it works)
  * a list of tools (Python functions with a JSON schema)

The loop runs until Claude stops calling tools or the step limit is hit. Every
step is reported through `emit` so the interface can show progress live, and
every API response is priced so you can see what the job cost.
"""

from __future__ import annotations
import json
import traceback

from . import config, llm, store

MAX_HISTORY_MESSAGES = 40


class Tool:
    def __init__(self, name, description, input_schema, handler,
                 label=None, writes=False):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler
        self.label = label or name
        self.writes = writes

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class Agent:
    key = "agent"
    title = "Agent"
    description = ""
    icon = "*"
    greeting = ""

    def system_prompt(self, project: dict) -> str:
        return ""

    def tools(self) -> list:
        return []

    # -- internals ----------------------------------------------------------

    def _tool_map(self):
        return {tool.name: tool for tool in self.tools()}

    def _history(self, project: dict) -> list:
        messages = []
        for entry in project.get("messages", [])[-MAX_HISTORY_MESSAGES:]:
            if entry.get("role") in ("user", "assistant") and entry.get("api"):
                messages.append(entry["api"])
        return messages

    # -- main loop ----------------------------------------------------------

    def run(self, project: dict, user_blocks: list, emit) -> dict:
        tools = self._tool_map()
        tool_schemas = [t.schema() for t in tools.values()]
        max_steps = config.get_int("MAX_AGENT_STEPS")

        project.setdefault("messages", []).append({
            "role": "user",
            "api": {"role": "user", "content": user_blocks},
            "display": _display_text(user_blocks),
        })

        messages = self._history(project)
        system = self.system_prompt(project)
        final_text = ""
        run_cost = {"input_tokens": 0, "output_tokens": 0,
                    "cached_tokens": 0, "cost_usd": 0.0, "steps": 0}

        for _step in range(max_steps):
            emit({"type": "status", "text": "Thinking"})
            try:
                response = llm.call(
                    messages=messages,
                    system=system,
                    tools=tool_schemas,
                    # Artwork specs are long. Give the model room to design a
                    # whole panel set in one call instead of dribbling it out.
                    max_tokens=16000,
                )
            except llm.LLMError as exc:
                emit({"type": "error", "text": str(exc)})
                _record(project, run_cost)
                store.save(project)
                return {"ok": False, "error": str(exc), "cost": run_cost}

            cost = llm.cost_of(response)
            run_cost["input_tokens"] += cost["input_tokens"]
            run_cost["output_tokens"] += cost["output_tokens"]
            run_cost["cached_tokens"] += cost["cached_tokens"]
            run_cost["cost_usd"] = round(run_cost["cost_usd"] + cost["cost_usd"], 6)
            run_cost["steps"] += 1
            run_cost["model_label"] = cost["model_label"]
            emit({"type": "cost", "run": dict(run_cost)})

            text = llm.text_of(response)
            calls = llm.tool_calls_of(response)

            if text:
                emit({"type": "assistant", "text": text})
                final_text = text

            assistant_api = {"role": "assistant", "content": response.get("content", [])}
            messages.append(assistant_api)
            project["messages"].append({
                "role": "assistant",
                "api": assistant_api,
                "display": text,
                "tools": [c.get("name") for c in calls],
            })

            if not calls:
                _record(project, run_cost)
                store.save(project)
                emit({"type": "done", "text": final_text, "run": dict(run_cost),
                      "total": project.get("usage", {})})
                return {"ok": True, "text": final_text, "cost": run_cost}

            results = []
            for call in calls:
                tool = tools.get(call.get("name"))
                label = tool.label if tool else call.get("name")
                emit({"type": "tool_start", "id": call.get("id"),
                      "name": call.get("name"), "label": label})
                if not tool:
                    payload = {"error": f"Unknown tool {call.get('name')}"}
                else:
                    try:
                        payload = tool.handler(project, call.get("input") or {}, emit)
                    except Exception as exc:
                        payload = {
                            "error": f"{type(exc).__name__}: {exc}",
                            "trace": traceback.format_exc()[-800:],
                        }
                        emit({"type": "error",
                              "text": f"Tool {call.get('name')} failed: {exc}"})
                emit({"type": "tool_end", "id": call.get("id"),
                      "name": call.get("name"), "label": label,
                      "ok": "error" not in (payload or {})})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.get("id"),
                    "content": json.dumps(payload, ensure_ascii=False)[:24000],
                    **({"is_error": True} if "error" in (payload or {}) else {}),
                })

            tool_api = {"role": "user", "content": results}
            messages.append(tool_api)
            project["messages"].append({
                "role": "user", "api": tool_api, "display": "", "hidden": True,
            })
            store.save(project)

        _record(project, run_cost)
        store.save(project)
        emit({"type": "done", "text": final_text, "run": dict(run_cost),
              "total": project.get("usage", {}),
              "note": "Step limit reached."})
        return {"ok": True, "text": final_text, "cost": run_cost, "truncated": True}


def _record(project: dict, run_cost: dict):
    """Add this run to the project's running total."""
    total = project.setdefault("usage", {
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        "cost_usd": 0.0, "runs": 0,
    })
    total["input_tokens"] += run_cost["input_tokens"]
    total["output_tokens"] += run_cost["output_tokens"]
    total["cached_tokens"] += run_cost.get("cached_tokens", 0)
    total["cost_usd"] = round(total["cost_usd"] + run_cost["cost_usd"], 6)
    total["runs"] += 1
    if run_cost["cost_usd"]:
        project["messages"][-1]["cost"] = dict(run_cost)


def _display_text(blocks) -> str:
    if isinstance(blocks, str):
        return blocks
    parts = []
    for block in blocks or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image":
                parts.append("[image]")
    return "\n".join(parts).strip()
