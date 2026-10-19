"""Validate native settings against the plugin's existing configuration schema."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from .comfyui_prompts import choose_dimensions
from .comfyui_workflows import WorkflowError


def settings_schema() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8"))["comfyui"]["items"]


def public_config(config: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: copy.deepcopy(config.get(key, item["default"])) for key, item in settings_schema().items()}
    result["has_api_key"] = bool(result.pop("api_key", ""))
    return result


def normalize_config(current: Mapping[str, Any], updates: Any) -> dict[str, Any]:
    schema = settings_schema()
    if not isinstance(updates, dict) or set(updates) - set(schema):
        raise WorkflowError("ComfyUI 配置包含未知字段")
    result = copy.deepcopy(dict(current))
    for key, item in schema.items():
        result.setdefault(key, copy.deepcopy(item["default"]))
    bounds = {"size_multiple": (1, 256), "max_pixels": (4096, 67108864),
              "timeout_seconds": (5, 1800), "model_timeout_seconds": (5, 600)}
    for key, value in updates.items():
        item = schema[key]
        kind = item["type"]
        if kind == "bool" and type(value) is not bool:
            raise WorkflowError(f"{key} 必须为布尔值")
        if kind == "int" and (type(value) is not int or not bounds[key][0] <= value <= bounds[key][1]):
            raise WorkflowError(f"{key} 超出范围")
        if kind in {"string", "text"}:
            if not isinstance(value, str) or len(value) > 16000:
                raise WorkflowError(f"{key} 必须为文本且不超过 16000 字符")
            value = value.strip()
        if item.get("options") and value not in item["options"]:
            raise WorkflowError(f"{key} 选项无效")
        if kind == "list" and (not isinstance(value, list) or len(value) > 200 or any(not isinstance(v, (str, dict)) for v in value)):
            raise WorkflowError("工作流列表必须包含文件路径或工作流对象")
        result[key] = copy.deepcopy(value)
    for key in ("portrait_size", "landscape_size", "square_size"):
        choose_dimensions(result, {"image_size": result[key]}, "")
    return result
