"""
prompt_store.py — runtime prompt template persistence.

Stores user-edited prompt templates in data/prompts.json while keeping code
defaults as the fallback source of truth.
"""

import json
import logging
import os
from copy import deepcopy
from typing import Any, Dict, Iterable, Optional

import config

logger = logging.getLogger(__name__)

PROMPTS_PATH = os.path.join(config.DATA_DIR, "prompts.json")


def _load_from_disk() -> Dict[str, str]:
    if not os.path.exists(PROMPTS_PATH):
        return {}
    try:
        with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("Failed to load prompts.json: %s", e)
        return {}

    raw_prompts = data.get("prompts", data) if isinstance(data, dict) else {}
    if not isinstance(raw_prompts, dict):
        return {}
    return {
        str(key): value
        for key, value in raw_prompts.items()
        if isinstance(value, str)
    }


def _save_to_disk(prompts: Dict[str, str]):
    os.makedirs(os.path.dirname(PROMPTS_PATH), exist_ok=True)
    with open(PROMPTS_PATH, "w", encoding="utf-8") as f:
        json.dump({"prompts": prompts}, f, indent=2, ensure_ascii=False)


def get_text(key: str, default: str) -> str:
    value = _load_from_disk().get(key)
    if isinstance(value, str) and value.strip():
        return value
    return default


def get_all(definitions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    saved = _load_from_disk()
    groups: Dict[str, str] = {}
    prompts = []

    for key, meta in definitions.items():
        default_value = str(meta.get("default", ""))
        value = saved.get(key, default_value)
        group_key = str(meta.get("group", "other"))
        groups[group_key] = str(meta.get("group_label", group_key))
        prompts.append({
            "key": key,
            "label": meta.get("label", key),
            "group": group_key,
            "description": meta.get("description", ""),
            "value": value,
            "default": default_value,
            "is_custom": key in saved,
            "rows": meta.get("rows", 12),
        })

    return {"groups": groups, "prompts": prompts}


def update(definitions: Dict[str, Dict[str, Any]], changes: Dict[str, Any]) -> Dict[str, str]:
    if not isinstance(changes, dict):
        return {}

    saved = _load_from_disk()
    changed: Dict[str, str] = {}
    valid_keys = set(definitions.keys())

    for key, value in changes.items():
        if key not in valid_keys or not isinstance(value, str):
            continue
        default_value = str(definitions[key].get("default", ""))
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        if normalized.strip() and normalized != default_value:
            if saved.get(key) != normalized:
                saved[key] = normalized
                changed[key] = normalized
        elif key in saved:
            del saved[key]
            changed[key] = default_value

    if changed:
        _save_to_disk(saved)
        logger.info("Prompt templates updated: %s", list(changed.keys()))

    return changed


def reset(definitions: Dict[str, Dict[str, Any]], keys: Optional[Iterable[str]] = None) -> Dict[str, str]:
    saved = _load_from_disk()
    valid_keys = set(definitions.keys())
    target_keys = valid_keys if keys is None else {key for key in keys if key in valid_keys}
    reset_values: Dict[str, str] = {}

    for key in target_keys:
        if key in saved:
            del saved[key]
            reset_values[key] = str(definitions[key].get("default", ""))

    if reset_values:
        _save_to_disk(saved)
        logger.info("Prompt templates reset: %s", list(reset_values.keys()))

    return reset_values

