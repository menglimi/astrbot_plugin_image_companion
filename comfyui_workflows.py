"""API workflows and persisted, constrained input mappings for native ComfyUI."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


class WorkflowError(ValueError):
    def __init__(self, message: str, *, submitted: bool = False, task_id: str = "", failure_stage: str = "preflight"):
        super().__init__(message)
        self.submitted = submitted
        self.task_id = task_id
        self.failure_stage = failure_stage


def fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def api_workflow(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value.lstrip("\ufeff"))
    if isinstance(value, dict) and isinstance(value.get("prompt"), dict):
        value = value["prompt"]
    if not isinstance(value, dict) or not value or "nodes" in value:
        raise WorkflowError("请导入 ComfyUI 的 API 格式 JSON（普通画布请先导出 API 格式）")
    if len(value) > 2000 or len(json.dumps(value)) > 4 * 1024 * 1024:
        raise WorkflowError("工作流过大")
    for key, node in value.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict) or not node.get("class_type"):
            raise WorkflowError(f"节点 {key} 缺少 class_type 或 inputs")
        for field, item in node["inputs"].items():
            if isinstance(item, list) and len(item) == 2:
                if str(item[0]) not in value or type(item[1]) is not int or item[1] < 0:
                    raise WorkflowError(f"节点 {key}.{field} 的连线无效")
    return copy.deepcopy(value)


def ancestors(workflow: Mapping[str, Any], roots: list[str]) -> set[str]:
    found: set[str] = set()
    pending = list(roots)
    while pending:
        node_id = pending.pop()
        if node_id in found or node_id not in workflow:
            continue
        found.add(node_id)
        pending.extend(str(v[0]) for v in workflow[node_id]["inputs"].values() if isinstance(v, list) and len(v) == 2)
    return found


def infer_mapping(workflow: Mapping[str, Any]) -> dict[str, Any]:
    outputs = [n for n, v in workflow.items() if v["class_type"] == "SaveImage"]
    if not outputs:
        outputs = [n for n, v in workflow.items() if v["class_type"] == "PreviewImage"]
    active = ancestors(workflow, outputs)
    fields: dict[str, Any] = {}
    warnings: list[str] = []

    def add(name: str, node_id: str, field: str, kind: str = "prompt", **extra: Any) -> None:
        base, index = name, 2
        while name in fields:
            name = f"{base}_{index}"
            index += 1
        fields[name] = {"node_id": node_id, "input_name": field, "kind": kind,
                        "mode": "append" if name.startswith("negative_prompt") else "replace", **extra}

    prompt_roles: dict[str, str] = {}
    for node_id in active:
        node = workflow[node_id]
        if "sampler" in node["class_type"].lower():
            for role in ("positive", "negative"):
                link = node["inputs"].get(role)
                if isinstance(link, list):
                    for source in ancestors(workflow, [str(link[0])]):
                        # A shared CLIP loader is not a text entry. Later we only
                        # select literal STRING inputs from known prompt nodes.
                        prompt_roles.setdefault(source, f"{role}_prompt")
    for node_id, node in workflow.items():
        if node_id not in active:
            continue
        cls, inputs = node["class_type"], node["inputs"]
        if cls == "AnimaPromptPlusClipEncode":
            for name, field in (("clothing_prompt", "clothing_tags"), ("pose_prompt", "pose_tags"),
                                ("background_prompt", "background_tags"), ("extra_prompt", "extra_prompt")):
                if field in inputs:
                    add(name, node_id, field, description=name, format="tags")
        if cls in {"Simple String", "CLIPTextEncode", "CLIPTextEncodeSDXL", "PrimitiveString", "StringConstant"}:
            for field in ("text", "string", "text_g", "text_l"):
                if isinstance(inputs.get(field), str):
                    role = prompt_roles.get(node_id, "positive_prompt")
                    mode = "append" if inputs[field].strip() and cls.startswith("CLIPTextEncode") else "replace"
                    add(role, node_id, field, mode=mode if role != "negative_prompt" else "append",
                        description=str(node.get("_meta", {}).get("title", role)))
        if cls in {"LoadImage", "ETN_LoadImageBase64"} and isinstance(inputs.get("image"), str):
            add(f"reference_image_{1 + sum(v['kind'] == 'image' for v in fields.values())}", node_id, "image", "image", role="generic")
        if cls in {"KSampler", "KSamplerAdvanced", "RandomNoise"}:
            for field in ("seed", "noise_seed"):
                if type(inputs.get(field)) is int:
                    add("seed", node_id, field, "number")
        if cls in {"ResolutionMaster", "EmptyLatentImage", "EmptySD3LatentImage", "EmptyFlux2LatentImage"}:
            if not inputs.get("auto_detect", False):
                for field in ("width", "height"):
                    if type(inputs.get(field)) is int:
                        add(field, node_id, field, "number")
    if len(outputs) != 1:
        warnings.append("存在多个图片输出或没有标准图片输出，请选择最终输出节点")
    if not any(v["kind"] == "prompt" for v in fields.values()):
        warnings.append("未确定提示词入口，请使用模型分析或手动映射")
    for name in ("positive_prompt", "width", "height"):
        if f"{name}_2" in fields:
            warnings.append(f"存在多个 {name} 输入，请检查对应阶段")
    return {"fields": fields, "output_node": outputs[0] if len(outputs) == 1 else "", "warnings": warnings}


def validate_mapping(workflow: Mapping[str, Any], mapping: Mapping[str, Any], definitions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    fields = mapping.get("fields", {})
    output = str(mapping.get("output_node") or "")
    if not isinstance(fields, dict) or not fields or len(fields) > 100:
        raise WorkflowError("填写规则必须包含有效的 fields")
    if output not in workflow:
        raise WorkflowError("请选择有效的最终图片输出节点")
    active = ancestors(workflow, [output])
    result: dict[str, Any] = {}
    targets: set[tuple[str, str]] = set()
    for name, raw in fields.items():
        if not isinstance(name, str) or not name.replace("_", "").isalnum() or not isinstance(raw, dict):
            raise WorkflowError("填写规则名称或内容无效")
        node_id, field = str(raw.get("node_id", "")), str(raw.get("input_name", ""))
        if node_id not in active or field not in workflow[node_id]["inputs"]:
            raise WorkflowError(f"{name} 指向不存在或不影响输出的输入")
        if (node_id, field) in targets:
            raise WorkflowError(f"同一个输入不能被重复映射：{node_id}.{field}")
        targets.add((node_id, field))
        kind = raw.get("kind", "prompt")
        mode = raw.get("mode", "replace")
        if kind not in {"prompt", "number", "image"} or mode not in {"replace", "append", "preserve"}:
            raise WorkflowError(f"{name} 的填写类型或方式无效")
        if kind != "prompt" and mode == "append":
            raise WorkflowError("只有文本提示词允许追加")
        value = workflow[node_id]["inputs"][field]
        definition = (definitions or {}).get(workflow[node_id]["class_type"], {})
        schema = {**definition.get("input", {}).get("required", {}), **definition.get("input", {}).get("optional", {})}
        field_type = schema.get(field, [None])[0]
        if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise WorkflowError(f"{name} 不是可直接填写的数字输入")
        if kind == "number" and field not in {"width", "height", "seed", "noise_seed"}:
            raise WorkflowError("自动填写的数字仅支持尺寸与种子，其他参数保留工作流原值")
        if kind in {"prompt", "image"} and not isinstance(value, (str, list)):
            raise WorkflowError(f"{name} 不是文本输入")
        if kind == "prompt" and definitions and field_type != "STRING":
            raise WorkflowError(f"{name} 不是节点声明的 STRING 输入")
        if kind == "prompt" and any(part in field.lower() for part in ("filename", "directory", "path", "url", "api_key", "password", "token")):
            raise WorkflowError(f"{name} 指向文件或连接设置，不能作为提示词填写")
        if kind == "image" and workflow[node_id]["class_type"] not in {"LoadImage", "ETN_LoadImageBase64"}:
            raise WorkflowError("图片必须映射到 LoadImage 或 ETN_LoadImageBase64 的 image 输入")
        if kind == "image" and (field != "image" or not isinstance(value, str)):
            raise WorkflowError("图片必须映射到实际图片加载入口")
        if mode == "append" and not isinstance(value, str):
            raise WorkflowError("连线输入不能直接追加文本，请选择文本源输入或替换方式")
        result[name] = {"node_id": node_id, "input_name": field, "kind": kind, "mode": mode,
                        "description": str(raw.get("description", name))[:300],
                        "format": str(raw.get("format", "auto"))[:80],
                        "role": str(raw.get("role", "generic"))[:60]}
    if definitions and not definitions.get(workflow[output]["class_type"], {}).get("output_node"):
        raise WorkflowError("所选节点不是输出节点")
    return {"fields": result, "output_node": output}


class WorkflowStore:
    def __init__(self, root: Path, entries: list[Any] | None = None):
        self.root = Path(root)
        self.entries = []
        for entry in entries or []:
            if isinstance(entry, str):
                entry = json.loads(entry) if entry.lstrip().startswith("{") else {"name": Path(entry).name, "path": entry}
            if not isinstance(entry, dict) or not entry.get("name"):
                raise WorkflowError("每个工作流应填写 API JSON 文件路径或带 name 的对象")
            self.entries.append(entry)

    def _entry(self, name: str) -> dict[str, Any] | None:
        matches = [entry for entry in self.entries if entry.get("name") == name]
        if not matches:
            matches = [entry for entry in self.entries if Path(str(entry["name"])).stem.split("+")[0] == name]
        if len(matches) > 1:
            raise WorkflowError("工作流名称不唯一，请填写完整名称")
        return matches[0] if matches else None

    def load(self, name: str) -> dict[str, Any]:
        entry = self._entry(name)
        if entry is not None:
            value = entry.get("workflow")
            if not value and entry.get("path"):
                path = Path(str(entry["path"]))
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise WorkflowError("工作流过大")
                value = path.read_text(encoding="utf-8-sig")
            return api_workflow(value)
        if Path(name).name != name or "/" in name or "\\" in name:
            raise WorkflowError("工作流名称不能包含路径")
        matches = [p for p in self.root.glob("*.json") if p.name == name or p.stem == name or p.stem.split("+")[0] == name]
        if len(matches) != 1:
            raise WorkflowError("未找到工作流或名称不唯一，请填写完整名称")
        path = matches[0]
        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            raise WorkflowError("工作流文件无效或过大")
        return api_workflow(path.read_text(encoding="utf-8-sig"))

    def names(self) -> list[str]:
        return sorted(set([str(e["name"]) for e in self.entries if isinstance(e, dict) and e.get("name")] + [p.name for p in self.root.glob("*.json")]))

    def import_workflow(self, name: str, value: Any) -> str:
        if not name or Path(name).name != name or any(c in name for c in '/\\:'):
            raise WorkflowError("工作流名称无效")
        name = name if name.endswith(".json") else name + ".json"
        workflow = api_workflow(value)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        if path.exists():
            raise WorkflowError("同名工作流已存在，请使用新名称")
        path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        return name

    def _mapping_path(self, name: str) -> Path:
        return self.root.parent / "comfyui_mappings" / (hashlib.sha256(name.encode()).hexdigest() + ".json")

    def mapping(self, name: str, workflow: dict[str, Any]) -> dict[str, Any]:
        manual = (self._entry(name) or {}).get("mapping")
        if manual:
            return validate_mapping(workflow, manual)
        path = self._mapping_path(name)
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("fingerprint") != fingerprint(workflow):
                raise WorkflowError("工作流已变化，请重新识别并保存填写规则")
            return validate_mapping(workflow, record["mapping"])
        return validate_mapping(workflow, infer_mapping(workflow))

    def save_mapping(self, name: str, mapping: Mapping[str, Any], expected_fingerprint: str, definitions: Mapping[str, Any] | None = None) -> dict[str, Any]:
        workflow = self.load(name)
        if fingerprint(workflow) != expected_fingerprint:
            raise WorkflowError("分析期间工作流发生变化，请重新分析")
        result = validate_mapping(workflow, mapping, definitions)
        path = self._mapping_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"fingerprint": expected_fingerprint, "mapping": result}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return result


def fill_workflow(workflow: Mapping[str, Any], mapping: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(workflow)
    for name, value in values.items():
        target = mapping["fields"].get(name)
        if target is None:
            raise WorkflowError(f"工作流没有填写项：{name}")
        if target["mode"] == "preserve":
            continue
        inputs = result[target["node_id"]]["inputs"]
        field = target["input_name"]
        if target["kind"] == "number":
            if type(value) not in (int, float) or not math.isfinite(value):
                raise WorkflowError(f"{name} 必须为有限数字")
            if type(inputs[field]) is int and type(value) is not int:
                raise WorkflowError(f"{name} 必须为整数")
        elif not isinstance(value, str) or len(value) > (24 * 1024 * 1024 if target["kind"] == "image" else 16000):
            raise WorkflowError(f"{name} 内容类型错误或过长")
        if target["mode"] == "append":
            original = str(inputs[field]).strip()
            value = ", ".join(dict.fromkeys(v for v in (original, value.strip()) if v))
        inputs[field] = value
    return result
