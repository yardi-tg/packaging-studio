"""Agent scaffolding: a tool loop against the Anthropic Messages API.

An agent is three things:
  * a system prompt  -- who it is and how it works. Never changes mid-conversation.
  * a state block    -- what the project looks like right now. Sent fresh with
                        every new message from the user.
  * tools            -- Python functions with a JSON schema.

Why the split matters. Current Claude models think before they answer, and
each thinking block is signed against everything that came before it: system
prompt, tool list, earlier messages. Change any of that afterwards and the
model can no longer read its own earlier reasoning (some models refuse the
request outright). So the conversation here is append-only: the system prompt
and tools stay fixed, everything that changes rides in the newest message, and
nothing already sent is ever edited. When something fixed does change -- you
switch model, update the app, use another API key -- the conversation is
folded into a short summary and a fresh one starts from the current project
state. The design itself never depends on chat history; it lives in the
project file.
"""

from __future__ import annotations
import copy
import hashlib
import json
import traceback

from . import config, llm, store

# Fold the conversation once it carries roughly this much text (~150k tokens).
COMPACT_AT_CHARS = 600_000


class Tool:
    def __init__(self, name, description, input_schema, handler, label=None):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler
        self.label = label or name

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


class Agent:
    key = "agent"
    title = "Agent"
    description = ""
    icon = "*"
    greeting = ""
    starters: list = []

    def system_prompt(self) -> str:
        """Fixed for the life of a conversation. No project data in here."""
        return ""

    def state_block(self, project: dict) -> str:
        """The current project, attached to every new user message."""
        return ""

    def tools(self) -> list:
        return []

    # -- conversation bookkeeping ------------------------------------------

    def _signature(self, model: str, system: str, schemas: list) -> str:
        key = config.get("ANTHROPIC_API_KEY") or ""
        blob = json.dumps({"m": model, "s": system, "t": schemas,
                           "k": hashlib.sha256(key.encode()).hexdigest()[:12]},
                          sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:20]

    @staticmethod
    def _active(project: dict) -> list:
        return [m for m in project.get("messages", [])
                if m.get("api") and not m.get("archived")]

    @staticmethod
    def _size(entries: list) -> int:
        return sum(len(json.dumps(e["api"], ensure_ascii=False)) for e in entries)

    def compact(self, project: dict, reason: str):
        """Retire the live conversation and leave a short summary for the next one.

        The visible chat stays on screen; only what is sent to the model resets.
        """
        active = self._active(project)
        if not active:
            return
        lines = []
        for entry in project.get("messages", []):
            if entry.get("hidden") or not entry.get("display"):
                continue
            who = "User" if entry.get("role") == "user" else "You"
            text = " ".join(entry["display"].split())
            lines.append(f"{who}: {text[:500]}")
        for entry in active:
            entry["archived"] = True
        recent = "\n".join(lines[-14:])
        project["carryover"] = (
            "Earlier conversation, summarised because " + reason + ". The project "
            "state below is authoritative; this is only for context on what the "
            "user has asked for so far.\n" + recent
        )
        store.log(project, "compact", reason)

    # -- main loop ----------------------------------------------------------

    def run(self, project: dict, user_blocks: list, emit) -> dict:
        tools = {t.name: t for t in self.tools()}
        schemas = [t.schema() for t in tools.values()]
        system = self.system_prompt()
        model = config.get("ANTHROPIC_MODEL")
        effort = config.get("ANTHROPIC_EFFORT") or "high"
        max_steps = config.get_int("MAX_AGENT_STEPS")

        signature = self._signature(model, system, schemas)
        if self._active(project):
            if project.get("conversation_signature") != signature:
                self.compact(project, "the model, the app or the API key changed")
            elif self._size(self._active(project)) > COMPACT_AT_CHARS:
                self.compact(project, "it had grown long")
        project["conversation_signature"] = signature

        self._open_turn(project, user_blocks)
        run_cost = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                    "cost_usd": 0.0, "steps": 0}
        final_text = ""
        recovered = False
        ceiling = None

        step = 0
        while step < max_steps:
            emit({"type": "status", "text": "Thinking"})
            messages = [e["api"] for e in self._active(project)]
            try:
                response = llm.stream(
                    messages=_with_cache_breakpoint(messages),
                    system=system, tools=schemas, model=model, effort=effort,
                    max_tokens=ceiling, on_event=_relay(emit, tools),
                )
            except llm.ThinkingMismatch:
                if recovered:
                    return self._fail(project, run_cost, emit,
                                      "The conversation could not be resumed. "
                                      "Clear the history and try again.")
                recovered = True
                self.compact(project, "earlier reasoning could no longer be verified")
                self._open_turn(project, user_blocks)
                continue
            except llm.Truncated as exc:
                if (exc.max_tokens or 0) >= llm.MAX_OUTPUT_CEILING:
                    return self._fail(project, run_cost, emit,
                                      "The design was too large for one answer. Ask "
                                      "for one panel at a time.")
                ceiling = min(llm.MAX_OUTPUT_CEILING, (exc.max_tokens or 32000) * 2)
                emit({"type": "status", "text": "Answer was long, giving it more room"})
                continue
            except llm.LLMError as exc:
                return self._fail(project, run_cost, emit, str(exc))

            step += 1
            ceiling = None
            cost = llm.cost_of(response)
            for key in ("input_tokens", "output_tokens", "cached_tokens"):
                run_cost[key] += cost[key]
            run_cost["cost_usd"] = round(run_cost["cost_usd"] + cost["cost_usd"], 6)
            run_cost["steps"] += 1
            run_cost["model_label"] = cost["model_label"]
            emit({"type": "cost", "run": dict(run_cost)})

            text = llm.text_of(response)
            calls = llm.tool_calls_of(response)
            if text:
                emit({"type": "assistant", "text": text})
                final_text = text

            # Stored exactly as received, thinking blocks and all.
            assistant_api = {"role": "assistant", "content": response.get("content", [])}
            project["messages"].append({
                "role": "assistant", "api": assistant_api, "display": text,
                "tools": [c.get("name") for c in calls],
            })

            if not calls:
                if response.get("stop_reason") == "refusal" and not text:
                    final_text = "The model declined this request."
                    emit({"type": "assistant", "text": final_text})
                _record(project, run_cost)
                store.save(project)
                emit({"type": "done", "text": final_text, "run": dict(run_cost),
                      "total": project.get("usage", {})})
                return {"ok": True, "text": final_text, "cost": run_cost}

            results = [self._run_tool(project, tools, call, emit) for call in calls]
            project["messages"].append({
                "role": "user", "api": {"role": "user", "content": results},
                "display": "", "hidden": True,
            })
            store.save(project)

        _record(project, run_cost)
        store.save(project)
        emit({"type": "done", "text": final_text, "run": dict(run_cost),
              "total": project.get("usage", {}), "note": "Step limit reached."})
        return {"ok": True, "text": final_text, "cost": run_cost, "truncated": True}

    # -- pieces -------------------------------------------------------------

    def _open_turn(self, project: dict, user_blocks: list):
        """Append the user's message, with the current project state attached."""
        blocks = []
        carry = project.pop("carryover", None)
        if carry:
            blocks.append({"type": "text", "text": carry})
        state = self.state_block(project)
        if state:
            blocks.append({"type": "text",
                           "text": "<project_state>\n" + state + "\n</project_state>"})
        blocks.extend(user_blocks)
        project.setdefault("messages", []).append({
            "role": "user", "api": {"role": "user", "content": blocks},
            "display": _display_text(user_blocks),
        })

    def _run_tool(self, project, tools, call, emit) -> dict:
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
                payload = {"error": f"{type(exc).__name__}: {exc}",
                           "trace": traceback.format_exc()[-800:]}
                emit({"type": "error", "text": f"Tool {call.get('name')} failed: {exc}"})
        payload = payload if isinstance(payload, dict) else {"result": payload}
        emit({"type": "tool_end", "id": call.get("id"), "name": call.get("name"),
              "label": label, "ok": "error" not in payload})

        # A tool may hand back a picture (look_at_design does). It goes to the
        # model as an image block and stays in the history as sent: editing it
        # out later would break the conversation for the reasons above.
        image = payload.pop("_image", None)
        text = json.dumps(payload, ensure_ascii=False)[:24000]
        content = text
        if image:
            content = [{"type": "image", "source": {
                            "type": "base64", "media_type": image["media_type"],
                            "data": image["data"]}},
                       {"type": "text", "text": text}]
        result = {"type": "tool_result", "tool_use_id": call.get("id"),
                  "content": content}
        if "error" in payload:
            result["is_error"] = True
        return result

    def _fail(self, project, run_cost, emit, message):
        # The open user turn has no answer. Retire the conversation so the next
        # message starts clean instead of tripping over the dangling turn.
        self.compact(project, "the previous request failed")
        _record(project, run_cost)
        store.save(project)
        emit({"type": "error", "text": message})
        emit({"type": "done", "text": "", "run": dict(run_cost),
              "total": project.get("usage", {})})
        return {"ok": False, "error": message, "cost": run_cost}


def _with_cache_breakpoint(messages: list) -> list:
    """Mark the end of the conversation as cacheable, on a copy.

    Within one run the model is called again after every tool step with the
    same history plus a little more; caching that prefix makes each step cost a
    fraction of the first. The marker is not stored, and it is not part of what
    thinking blocks are signed against.
    """
    if not messages:
        return messages
    out = list(messages)
    last = copy.deepcopy(out[-1])
    content = last.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if content:
        target = content[-1]
        if target.get("type") not in ("thinking", "redacted_thinking"):
            target["cache_control"] = {"type": "ephemeral"}
        last["content"] = content
    out[-1] = last
    return out


def _relay(emit, tools):
    """Translate stream progress into interface events."""
    labels = {name: tool.label for name, tool in tools.items()}

    def on_event(event):
        kind = event.get("kind")
        if kind == "text":
            emit({"type": "assistant_delta", "text": event["text"]})
        elif kind == "thinking":
            emit({"type": "status", "text": "Thinking"})
        elif kind == "tool":
            label = labels.get(event.get("name"), event.get("name"))
            chars = event.get("chars", 0)
            emit({"type": "status",
                  "text": f"{label}" + (f" · {chars // 1000}k" if chars >= 1000 else "")})
        elif kind == "restart":
            emit({"type": "assistant_reset"})
    return on_event


def _record(project: dict, run_cost: dict):
    """Add this run to the project's running total."""
    total = project.setdefault("usage", {"input_tokens": 0, "output_tokens": 0,
                                         "cached_tokens": 0, "cost_usd": 0.0, "runs": 0})
    for key in ("input_tokens", "output_tokens", "cached_tokens"):
        total[key] = total.get(key, 0) + run_cost.get(key, 0)
    total["cost_usd"] = round(total.get("cost_usd", 0) + run_cost["cost_usd"], 6)
    total["runs"] = total.get("runs", 0) + 1
    if run_cost["cost_usd"]:
        for entry in reversed(project.get("messages", [])):
            if entry.get("role") == "assistant" and not entry.get("hidden"):
                entry["cost"] = dict(run_cost)
                break


def _display_text(blocks) -> str:
    if isinstance(blocks, str):
        return blocks
    parts = []
    for block in blocks or []:
        if isinstance(block, dict):
            if block.get("type") == "text" and not str(block.get("text", "")).startswith(
                    "[uploaded image"):
                parts.append(block.get("text", ""))
            elif block.get("type") == "image":
                parts.append("[image]")
    return "\n".join(parts).strip()
