"""Validator and guardrails for tool invocation.

This module provides:
- policy loading from `tool_policy.json`
- role resolution from RunnableConfig
- checks for allowed tools, simple intent alignment heuristics, duplicate-call prevention and rate limits.

The implementation is intentionally conservative and lightweight so it can
be integrated into the existing pipeline with minimal changes.
"""
import json
import os
import time
import threading
from typing import Any, Dict, Iterable, List

_policy = None
_policy_lock = threading.Lock()
_recent_calls: Dict[str, float] = {}


def _load_policy() -> Dict[str, Any]:
    global _policy
    if _policy is not None:
        return _policy
    with _policy_lock:
        if _policy is not None:
            return _policy
        path = os.path.join(os.path.dirname(__file__), "tool_policy.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _policy = json.load(fh)
        except Exception:
            # Fallback minimal policy
            _policy = {
                "roles": {},
                "default_role": "researcher",
                "duplicate_window_seconds": 300
            }
        return _policy


def get_role_from_config(config) -> str:
    # Try configurable.role, then metadata.role, else default
    try:
        cfg = config.get("configurable", {}) if config else {}
        role = cfg.get("role") or config.get("metadata", {}).get("role")
        if role:
            return role
    except Exception:
        pass
    return _load_policy().get("default_role", "researcher")


def is_tool_name(obj) -> str:
    if not obj:
        return ""
    if isinstance(obj, dict):
        return obj.get("name", "")
    # objects may have a `name` attribute
    return getattr(obj, "name", "") or ""


def is_tool_allowed_for_role(role: str, tool_name: str) -> bool:
    policy = _load_policy()
    roles = policy.get("roles", {})
    role_cfg = roles.get(role, {})
    allowed = role_cfg.get("allowed_tools", [])
    if not allowed:
        return False
    if "*" in allowed:
        return True
    return tool_name in allowed


def filter_tools_by_role(config, tools: Iterable) -> List:
    role = get_role_from_config(config)
    filtered = []
    for t in tools:
        name = is_tool_name(t)
        if not name:
            # keep unknown objects conservative
            continue
        if is_tool_allowed_for_role(role, name):
            filtered.append(t)
    return filtered


def _call_fingerprint(tool_call: Dict[str, Any]) -> str:
    # fingerprint based on tool name and args string
    name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    try:
        args_str = json.dumps(args, sort_keys=True)
    except Exception:
        args_str = str(args)
    return f"{name}:{args_str}"


def is_duplicate_call(tool_call: Dict[str, Any]) -> bool:
    fp = _call_fingerprint(tool_call)
    policy = _load_policy()
    window = policy.get("duplicate_window_seconds", 300)
    now = time.time()
    last = _recent_calls.get(fp)
    if last and (now - last) < window:
        return True
    _recent_calls[fp] = now
    # prune occasionally
    if len(_recent_calls) > 1000:
        cutoff = now - window
        keys = [k for k, v in _recent_calls.items() if v < cutoff]
        for k in keys:
            _recent_calls.pop(k, None)
    return False


def validate_tool_call(config, tool_call: Dict[str, Any], messages: List[Any] | None = None, phase: str = "research") -> (bool, str):
    """Validate a proposed tool_call before execution.

    Returns (allowed: bool, reason: str).
    """
    # Basic shape check
    if not isinstance(tool_call, dict) or "name" not in tool_call:
        return False, "Malformed tool_call"

    name = tool_call["name"]
    role = get_role_from_config(config)

    # Role-based allowance
    if not is_tool_allowed_for_role(role, name):
        return False, f"Tool '{name}' not allowed for role '{role}'"

    # Phase-based allowance: strict per-phase rules
    # clarification phase: no tools allowed at all
    if phase == "clarify":
        return False, "Tools prohibited during clarification phase"

    # research phase: only ConductResearch allowed (and read-only think_tool may be allowed inside sandboxed researcher)
    if phase == "research":
        if name not in ("ConductResearch", "ResearchComplete"):
            return False, f"Tool '{name}' not allowed during research orchestration"

    # Duplicate suppression
    try:
        if is_duplicate_call(tool_call):
            return False, "Duplicate tool call suppressed"
    except Exception:
        # On any failure in duplicate logic, be conservative and disallow
        return False, "Duplicate check failed"

    # Simple intent alignment heuristic: ensure the tool call references user topic
    # If messages provided, require a short overlap of keywords between user prompt and tool args
    if messages and isinstance(messages, list):
        try:
            user_text = " ".join([str(m.content) for m in messages if hasattr(m, "content")])
            args_text = json.dumps(tool_call.get("args", {}))
            # require at least one shared token of length>3
            user_tokens = {t.lower() for t in user_text.split() if len(t) > 3}
            arg_tokens = {t.lower() for t in args_text.split() if len(t) > 3}
            if user_tokens and arg_tokens and user_tokens.isdisjoint(arg_tokens):
                # no overlap -> suspicious
                return False, "Tool call appears unrelated to user intent"
        except Exception:
            # If alignment checking fails, we do not block, just warn via reason
            pass

    return True, "allowed"


def validate_structured_output_conflict(schemas: List[dict]) -> (bool, str):
    """Ensure no conflicting strict json_schema declarations are present.

    `schemas` is a list of schema descriptors, each descriptor should be a dict
    with keys like `type` and `strict` when applicable.
    """
    strict_json_count = 0
    for s in schemas:
        try:
            if s.get("type") == "json_schema" and s.get("strict"):
                strict_json_count += 1
        except Exception:
            continue

    if strict_json_count > 1:
        return False, "Multiple strict json_schema response formats detected"
    return True, "ok"
