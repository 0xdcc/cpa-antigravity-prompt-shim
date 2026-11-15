"""Conservative request-only protocol adapters. Never mutate caller objects."""
from __future__ import annotations

import copy
import re
from urllib.parse import unquote, urlsplit


NATIVE_PATH = re.compile(r"/v1beta/models/([^/]+):(generateContent|streamGenerateContent)\Z")


def route(target: str) -> tuple[str, str | None]:
    path = urlsplit(target).path
    protocols = {"/v1/chat/completions": "chat", "/v1/messages": "messages", "/v1/responses": "responses"}
    if path in protocols:
        return protocols[path], None
    match = NATIVE_PATH.fullmatch(path)
    if match:
        token = match[1]
        if re.search(r"%(?![0-9a-fA-F]{2})", token):
            return "unsupported", None
        try:
            model = unquote(token, errors="strict")
        except UnicodeError:
            return "unsupported", None
        # Encoded slashes, action delimiters, double encoding and controls cannot
        # change model/path interpretation. The raw target is still forwarded.
        if re.fullmatch(r"[A-Za-z0-9_.~-]+", model):
            return "gemini", model
    return "unsupported", None


def _valid_cache_control(cc) -> bool:
    return (
        isinstance(cc, dict)
        and cc.get("type") == "ephemeral"
        and set(cc) <= {"type", "ttl"}
        and ("ttl" not in cc or isinstance(cc["ttl"], str))
    )


def text_blocks(value, kind: str, allow_cache: bool = False) -> tuple[list[tuple[str, dict | None]] | None, bool]:
    if isinstance(value, str):
        return [(value, None)], False
    if not isinstance(value, list) or not value:
        return None, False
    segs, has_cc = [], False
    allowed = {"type", "text", "cache_control"} if allow_cache else {"type", "text"}
    for x in value:
        if not (isinstance(x, dict) and set(x) <= allowed and x.get("type") == kind and isinstance(x.get("text"), str)):
            return None, False
        cc = None
        if "cache_control" in x:
            if not _valid_cache_control(x["cache_control"]):
                return None, False
            cc = copy.deepcopy(x["cache_control"])
            has_cc = True
        segs.append((x["text"], cc))
    if not has_cc:
        return [("\n".join(t for t, _ in segs), None)], False
    return segs, True


def leading(items, kind: str, allow_type: bool = False, allow_cache: bool = False):
    segs, count, has_cc = [], 0, False
    for item in items:
        if not isinstance(item, dict) or item.get("role") not in ("system", "developer"):
            break
        allowed = {"role", "content", "type"} if allow_type else {"role", "content"}
        if set(item) - allowed or ("type" in item and item["type"] != "message"):
            return None, 0, False
        item_segs, item_cc = text_blocks(item.get("content"), kind, allow_cache=allow_cache)
        if item_segs is None:
            return None, 0, False
        segs.extend(item_segs)
        has_cc = has_cc or item_cc
        count += 1
    return segs, count, has_cc


def _apply_rules_and_wrap(segs: list[tuple[str, dict | None]], rules, has_cc: bool):
    cleaned = []
    for text, cc in segs:
        for pattern, replacement in rules:
            text = pattern.sub(replacement, text)
        cleaned.append((text, cc))
    if not has_cc:
        joined = "\n\n".join(t for t, _ in cleaned)
        return "<developer_instructions>\n" + joined + "\n</developer_instructions>", None
    blocks = []
    last = len(cleaned) - 1
    for i, (text, cc) in enumerate(cleaned):
        if i == 0:
            text = "<developer_instructions>\n" + text
        if i == last:
            text = text + "\n</developer_instructions>"
        b = {"type": "text", "text": text}
        if cc is not None:
            b["cache_control"] = cc
        blocks.append(b)
    return None, blocks


def adapt(data: dict, protocol: str, anchor: str, rules) -> tuple[dict | None, str]:
    segs, consumed, has_cc = [], 0, False
    if protocol == "chat":
        history = data.get("messages")
        if not isinstance(history, list):
            return None, "no-messages"
        segs, consumed, has_cc = leading(history, "text", allow_cache=True)
        if segs is None:
            return None, "complex-system"
    elif protocol == "messages":
        history = data.get("messages")
        if not isinstance(history, list):
            return None, "no-messages"
        if "system" not in data:
            return None, "no-leading-system"
        segs, has_cc = text_blocks(data["system"], "text", allow_cache=True)
        if segs is None:
            return None, "complex-system"
    elif protocol == "responses":
        # A synthetic first turn cannot be inserted safely into server history.
        if any(key in data for key in ("previous_response_id", "conversation")):
            return None, "server-continuation"
        history = data.get("input")
        if isinstance(history, str):
            history = [{"role": "user", "content": [{"type": "input_text", "text": history}]}]
        if not isinstance(history, list):
            return None, "no-input"
        segs, consumed, _ = leading(history, "input_text", allow_type=True)
        if segs is None:
            return None, "complex-system"
        if "instructions" in data:
            if not isinstance(data["instructions"], str):
                return None, "complex-system"
            segs.insert(0, (data["instructions"], None))
    elif protocol == "gemini":
        if any(key in data for key in ("cachedContent", "cached_content")):
            return None, "server-continuation"
        history = data.get("contents")
        if not isinstance(history, list):
            return None, "no-contents"
        keys = [key for key in ("systemInstruction", "system_instruction") if key in data]
        if not keys:
            return None, "no-leading-system"
        if len(keys) != 1:
            return None, "complex-system"
        system = data[keys[0]]
        if not isinstance(system, dict) or set(system) - {"role", "parts"} or (
            "role" in system and system["role"] != "system"
        ):
            return None, "complex-system"
        parts = system.get("parts")
        if not isinstance(parts, list) or not parts or not all(
            isinstance(x, dict) and set(x) == {"text"} and isinstance(x["text"], str) for x in parts
        ):
            return None, "complex-system"
        segs = [("\n".join(x["text"] for x in parts), None)]
    else:
        return None, "unsupported-path"
    if not segs:
        return None, "no-leading-system"
    block, cc_blocks = _apply_rules_and_wrap(segs, rules, has_cc)
    result = copy.copy(data)
    if protocol == "chat":
        user_content = cc_blocks if cc_blocks is not None else block
        result["messages"] = [{"role": "system", "content": anchor}, {"role": "user", "content": user_content}] + history[consumed:]
    elif protocol == "messages":
        user_blocks = cc_blocks if cc_blocks is not None else [{"type": "text", "text": block}]
        result["system"] = anchor
        result["messages"] = [{"role": "user", "content": user_blocks}] + history
    elif protocol == "responses":
        result["instructions"] = anchor
        result["input"] = [{"role": "user", "content": [{"type": "input_text", "text": block}]}] + history[consumed:]
    else:
        result[keys[0]] = {"parts": [{"text": anchor}]}
        result["contents"] = [{"role": "user", "parts": [{"text": block}]}] + history
    return result, "wrapped"
