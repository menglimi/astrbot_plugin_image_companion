"""Guided editing compiler for photo reference metadata.

The editor deals in plain-language answers; the selector consumes this module's
small, deterministic result objects.  Nothing in this module persists data.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from .photo_reference_catalog import PhotoReference

OUTFIT_BEHAVIORS = {
    "ignore": "ignore",
    "reference_without_lock": "reference_without_lock",
    "参考但不保持": "reference_without_lock",
    "可以参考，但不要求保持": "reference_without_lock",
    "通常保持，除非用户明确要求换装": "preserve_unless_explicit_change",
    "preserve": "preserve_unless_explicit_change",
    "preserve_unless_explicit_change": "preserve_unless_explicit_change",
}
_ROLE_LABELS = {
    "identity": "人物外貌",
    "outfit": "穿搭",
    "pose": "动作姿势",
    "scene": "场景背景",
    "style": "画面风格",
    "continuity": "连续性",
    "source": "原图",
}
_OUTFIT_CATEGORIES = {
    "cosplay",
    "school_uniform",
    "sleepwear",
    "swimwear",
    "sportswear",
    "formalwear",
    "homewear",
    "daily_outfit",
}
_SCENE_CATEGORIES = {
    "home",
    "bedroom",
    "school",
    "office",
    "outdoor",
    "formal_event",
    "sport",
    "beach",
}
_TIME_CATEGORIES = {"morning", "daytime", "afternoon", "evening", "night", "bedtime"}
_SELECTION_ELIGIBILITY = {"matching_only", "fallback_identity_only", "fallback_allowed", "disabled"}


@dataclass(frozen=True)
class MetadataField:
    field: str
    value: Any
    source: str
    label: str


@dataclass(frozen=True)
class CompileResult:
    metadata: dict[str, Any]
    behavior_summary: str
    fields: tuple[MetadataField, ...]
    differences: tuple[dict[str, Any], ...]
    missing: tuple[str, ...]
    conflicts: tuple[str, ...]
    recommended_trials: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata,
            "behavior_summary": self.behavior_summary,
            "fields": [asdict(field) for field in self.fields],
            "differences": list(self.differences),
            "missing": list(self.missing),
            "conflicts": list(self.conflicts),
            "recommended_trials": list(self.recommended_trials),
        }


@dataclass(frozen=True)
class ReferenceExplanation:
    reference_id: str
    behavior_summary: str
    fields: tuple[MetadataField, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "behavior_summary": self.behavior_summary,
            "fields": [asdict(field) for field in self.fields],
            "warnings": list(self.warnings),
        }


def _clean_questionnaire(questionnaire: Mapping[str, Any] | None) -> dict[str, Any]:
    source = questionnaire if isinstance(questionnaire, Mapping) else {}
    answers: list[dict[str, Any]] = []
    for raw_answer in list(source.get("answers") or ())[:8]:
        if not isinstance(raw_answer, Mapping):
            continue
        question_id = str(raw_answer.get("id") or "").strip()[:80]
        question = str(raw_answer.get("question") or "").strip()[:240]
        selections: list[dict[str, str]] = []
        for raw_selection in list(raw_answer.get("selections") or ())[:24]:
            if not isinstance(raw_selection, Mapping):
                continue
            field = str(raw_selection.get("field") or "").strip()[:80]
            value = str(raw_selection.get("value") or "").strip()[:120]
            label = str(raw_selection.get("label") or value).strip()[:160]
            if field and value:
                selections.append({"field": field, "value": value, "label": label})
        if question_id and selections:
            answers.append(
                {
                    "id": question_id,
                    "question": question,
                    "selections": selections,
                }
            )
    return {"version": 2, "answers": answers}


def merge_reference_questionnaire_evidence(
    questionnaire: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a deterministic suggestion from redundant plain-language answers."""
    normalized = _clean_questionnaire(questionnaire)
    role_scores = {role: 0 for role in _ROLE_LABELS}
    evidence: dict[str, list[dict[str, Any]]] = {role: [] for role in _ROLE_LABELS}
    prefer_scenes: list[str] = []
    prefer_times: list[str] = []
    avoid_scenes: list[str] = []
    avoid_times: list[str] = []
    outfit_behavior = "reference_without_lock"
    outfit_category = ""
    eligibility = "matching_only"
    preferred_preset = ""

    role_evidence: dict[tuple[str, str], tuple[tuple[str, int], ...]] = {
        ("core_anchor", "identity"): (("identity", 3),),
        ("core_anchor", "outfit"): (("outfit", 3),),
        ("core_anchor", "pose"): (("pose", 3),),
        ("core_anchor", "scene"): (("scene", 3),),
        ("core_anchor", "style"): (("style", 3),),
        ("core_anchor", "continuity"): (("continuity", 3),),
        ("wardrobe_change", "yes_identity"): (("identity", 2), ("outfit", -1)),
        ("wardrobe_change", "yes_pose"): (("pose", 2), ("outfit", -1)),
        ("wardrobe_change", "yes_scene"): (("scene", 2), ("outfit", -1)),
        ("wardrobe_change", "yes_style"): (("style", 2), ("outfit", -1)),
        ("wardrobe_change", "conditional"): (("outfit", 1),),
        ("wardrobe_change", "no_outfit_core"): (("outfit", 3),),
        ("location_change", "yes_identity"): (("identity", 2), ("scene", -1)),
        ("location_change", "yes_outfit"): (("outfit", 2), ("scene", -1)),
        ("location_change", "yes_pose"): (("pose", 2), ("scene", -1)),
        ("location_change", "yes_style"): (("style", 2), ("scene", -1)),
        ("location_change", "conditional"): (("scene", 1),),
        ("location_change", "no_scene_core"): (("scene", 3),),
        ("pose_change", "yes_identity"): (("identity", 2), ("pose", -1)),
        ("pose_change", "yes_outfit"): (("outfit", 2), ("pose", -1)),
        ("pose_change", "yes_scene"): (("scene", 2), ("pose", -1)),
        ("pose_change", "yes_style"): (("style", 2), ("pose", -1)),
        ("pose_change", "small_change"): (("pose", 1),),
        ("pose_change", "no_pose_core"): (("pose", 3),),
        ("outfit_behavior", "reference_without_lock"): (("outfit", 2),),
        ("outfit_behavior", "preserve_unless_explicit_change"): (("outfit", 3),),
        ("outfit_behavior", "ignore"): (("outfit", -3),),
    }
    fallback_values = {
        "strict": "matching_only",
        "fallback_identity": "fallback_identity_only",
        "fallback_any": "fallback_allowed",
        "disabled": "disabled",
        "unsure": "matching_only",
    }

    for answer in normalized["answers"]:
        question_id = answer["id"]
        for selection in answer["selections"]:
            field = selection["field"]
            value = selection["value"]
            for role, score in role_evidence.get((field, value), ()):
                role_scores[role] += score
                evidence[role].append(
                    {
                        "question_id": question_id,
                        "answer": selection["label"],
                        "stance": "support" if score > 0 else "oppose",
                        "weight": abs(score),
                    }
                )
            if field == "prefer_scenes" and value in _SCENE_CATEGORIES and value not in prefer_scenes:
                prefer_scenes.append(value)
            elif field == "prefer_times" and value in _TIME_CATEGORIES and value not in prefer_times:
                prefer_times.append(value)
            elif field == "avoid_scenes" and value in _SCENE_CATEGORIES and value not in avoid_scenes:
                avoid_scenes.append(value)
            elif field == "avoid_times" and value in _TIME_CATEGORIES and value not in avoid_times:
                avoid_times.append(value)
            elif field == "outfit_behavior" and value in OUTFIT_BEHAVIORS:
                outfit_behavior = _behavior(value)
            elif field == "outfit_category" and value in _OUTFIT_CATEGORIES:
                outfit_category = value
            elif field == "fallback_policy" and value in fallback_values:
                eligibility = fallback_values[value]
            elif field == "preferred_preset":
                preferred_preset = value

    roles = [role for role in _ROLE_LABELS if role_scores[role] >= 2]
    if outfit_behavior == "ignore" and "outfit" in roles:
        roles.remove("outfit")
    if not roles:
        roles = ["identity"]
    active_evidence = {role: items for role, items in evidence.items() if items}
    return {
        "preserve": roles,
        "outfit_behavior": outfit_behavior,
        "outfit_category": outfit_category,
        "prefer": {"scenes": prefer_scenes, "times": prefer_times},
        "avoid": {"scenes": avoid_scenes, "times": avoid_times},
        "fallback": eligibility,
        "selection_eligibility": eligibility,
        "preferred_preset": preferred_preset,
        "evidence": active_evidence,
        "questionnaire": normalized,
    }


def _filtered_review_values(value: Any, allowed: set[str]) -> list[str]:
    return [item for item in _values(value) if item in allowed]


def normalize_reviewed_reference_intent(
    reviewed: Mapping[str, Any] | None,
    fallback: Mapping[str, Any],
    *,
    available_presets: Iterable[str] = (),
) -> dict[str, Any]:
    """Constrain an LLM review to the selector's supported metadata vocabulary."""
    payload = reviewed if isinstance(reviewed, Mapping) else {}
    raw = payload.get("intent") if isinstance(payload.get("intent"), Mapping) else payload
    fallback_prefer = _as_mapping(fallback.get("prefer"))
    fallback_avoid = _as_mapping(fallback.get("avoid"))
    raw_prefer = _as_mapping(raw.get("prefer"))
    raw_avoid = _as_mapping(raw.get("avoid"))

    roles = _filtered_review_values(raw.get("preserve"), set(_ROLE_LABELS))
    if not roles:
        roles = _filtered_review_values(fallback.get("preserve"), set(_ROLE_LABELS)) or ["identity"]
    behavior_value = str(raw.get("outfit_behavior") or "").strip()
    behavior = _behavior(behavior_value) if behavior_value in OUTFIT_BEHAVIORS else _behavior(fallback.get("outfit_behavior"))
    category = str(raw.get("outfit_category") or "").strip()
    if category not in _OUTFIT_CATEGORIES:
        category = str(fallback.get("outfit_category") or "").strip()
    if category not in _OUTFIT_CATEGORIES:
        category = ""

    def reviewed_list(container: Mapping[str, Any], key: str, allowed: set[str], fallback_value: Any) -> list[str]:
        if key in container:
            valid = _filtered_review_values(container.get(key), allowed)
            if isinstance(container.get(key), (list, tuple, set)):
                return valid
            if valid:
                return valid
        return _filtered_review_values(fallback_value, allowed)

    prefer = {
        "scenes": reviewed_list(raw_prefer, "scenes", _SCENE_CATEGORIES, fallback_prefer.get("scenes")),
        "times": reviewed_list(raw_prefer, "times", _TIME_CATEGORIES, fallback_prefer.get("times")),
    }
    avoid = {
        "scenes": reviewed_list(raw_avoid, "scenes", _SCENE_CATEGORIES, fallback_avoid.get("scenes")),
        "times": reviewed_list(raw_avoid, "times", _TIME_CATEGORIES, fallback_avoid.get("times")),
    }
    eligibility = str(raw.get("selection_eligibility") or "").strip()
    if eligibility not in _SELECTION_ELIGIBILITY:
        eligibility = str(fallback.get("selection_eligibility") or "matching_only").strip()
    if eligibility not in _SELECTION_ELIGIBILITY:
        eligibility = "matching_only"
    presets = {str(item).strip() for item in available_presets if str(item).strip()}
    preset = str(raw.get("preferred_preset") or "").strip()
    if preset not in presets:
        preset = str(fallback.get("preferred_preset") or "").strip()
    if preset not in presets:
        preset = ""
    return {
        "preserve": roles,
        "outfit_behavior": behavior,
        "outfit_category": category,
        "prefer": prefer,
        "avoid": avoid,
        "fallback": eligibility,
        "selection_eligibility": eligibility,
        "preferred_preset": preset,
    }


def build_reference_metadata_review_prompt(
    questionnaire: Mapping[str, Any] | None,
    local_suggestion: Mapping[str, Any],
    *,
    available_presets: Iterable[str] = (),
) -> tuple[str, str]:
    normalized = _clean_questionnaire(questionnaire)
    allowed = {
        "roles": list(_ROLE_LABELS),
        "outfit_behaviors": sorted(set(OUTFIT_BEHAVIORS.values())),
        "outfit_categories": sorted(_OUTFIT_CATEGORIES),
        "scenes": sorted(_SCENE_CATEGORIES),
        "times": sorted(_TIME_CATEGORIES),
        "selection_eligibility": sorted(_SELECTION_ELIGIBILITY),
        "presets": [str(item).strip() for item in available_presets if str(item).strip()],
    }
    system_prompt = """
你是参考图用途元数据审批器。维护者会从多个角度重复回答同一职责相关问题。
请交叉审批全部答案：一致证据应合并，矛盾证据要明确裁决，不要把每题机械映射成一个字段。
只能使用给定白名单值，不要发明角色、场景、时间、服装类别或预设。
只输出 JSON 对象，不要 Markdown，不要解释 JSON 之外的内容。
""".strip()
    requested_schema = {
        "intent": {
            "preserve": ["identity"],
            "outfit_behavior": "reference_without_lock",
            "outfit_category": "",
            "prefer": {"scenes": [], "times": []},
            "avoid": {"scenes": [], "times": []},
            "selection_eligibility": "matching_only",
            "preferred_preset": "",
        },
        "responsibility_decisions": [
            {
                "responsibility": "identity",
                "verdict": "include",
                "evidence_question_ids": ["core_anchor"],
                "reason": "一句通俗中文原因",
            }
        ],
        "conflicts": [],
        "review_summary": "一句通俗中文审批结论",
    }
    user_prompt = "\n".join(
        (
            "可用值白名单：" + json.dumps(allowed, ensure_ascii=False),
            "维护者问答证据：" + json.dumps(normalized, ensure_ascii=False),
            "本地规则建议（只作参考，可基于交叉证据调整）：" + json.dumps(local_suggestion, ensure_ascii=False),
            "必须严格返回此结构：" + json.dumps(requested_schema, ensure_ascii=False),
        )
    )
    return system_prompt, user_prompt


def _values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _behavior(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in OUTFIT_BEHAVIORS:
        return OUTFIT_BEHAVIORS[text]
    if "完全不" in text or "不参考" in text or text in {"ignore", "none"}:
        return "ignore"
    if "通常保持" in text or "明确要求换装" in text:
        return "preserve_unless_explicit_change"
    return "reference_without_lock"


def _roles(intent: Mapping[str, Any]) -> list[str]:
    preserve = intent.get("preserve", intent.get("reference_roles", ()))
    result: list[str] = []
    for role in _values(preserve):
        role = role.lower()
        if role in _ROLE_LABELS and role not in result:
            result.append(role)
    return result


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def compile_reference_metadata(
    intent: Mapping[str, Any] | None,
    available_presets: Iterable[str] = (),
    *,
    saved: Mapping[str, Any] | PhotoReference | None = None,
) -> CompileResult:
    """Compile editor answers without writing to the catalog."""
    answers = dict(intent or {})
    manual_override = dict(answers.get("manual_override") or {}) if isinstance(answers.get("manual_override"), Mapping) else {}
    if manual_override:
        if "reference_roles" in manual_override:
            answers["preserve"] = manual_override["reference_roles"]
        if "outfit_category" in manual_override:
            category = str(manual_override.get("outfit_category") or "").strip().lower()
            answers["outfit_category"] = category if category in _OUTFIT_CATEGORIES else ""
        if "outfit_lock_default" in manual_override:
            answers["outfit_behavior"] = (
                "preserve_unless_explicit_change"
                if bool(manual_override.get("outfit_lock_default"))
                else "reference_without_lock"
            )
        prefer_override = dict(_as_mapping(answers.get("prefer")))
        avoid_override = dict(_as_mapping(answers.get("avoid")))
        if "scene_categories" in manual_override:
            prefer_override["scenes"] = [item for item in _values(manual_override["scene_categories"]) if item in _SCENE_CATEGORIES]
        if "time_categories" in manual_override:
            prefer_override["times"] = [item for item in _values(manual_override["time_categories"]) if item in _TIME_CATEGORIES]
        if "excluded_scene_categories" in manual_override:
            avoid_override["scenes"] = [item for item in _values(manual_override["excluded_scene_categories"]) if item in _SCENE_CATEGORIES]
        if "excluded_time_categories" in manual_override:
            avoid_override["times"] = [item for item in _values(manual_override["excluded_time_categories"]) if item in _TIME_CATEGORIES]
        answers["prefer"] = prefer_override
        answers["avoid"] = avoid_override
        if "selection_eligibility" in manual_override:
            answers["selection_eligibility"] = manual_override["selection_eligibility"]
        if "preferred_preset" in manual_override:
            answers["preferred_preset"] = manual_override["preferred_preset"]
    roles = _roles(answers)
    outfit_behavior = _behavior(answers.get("outfit_behavior")) if "outfit" in roles else "ignore"
    prefer = _as_mapping(answers.get("prefer"))
    avoid = _as_mapping(answers.get("avoid"))
    scenes = _values(prefer.get("scenes", answers.get("scene_categories")))
    times = _values(prefer.get("times", answers.get("time_categories")))
    excluded_scenes = _values(avoid.get("scenes", answers.get("excluded_scene_categories")))
    excluded_times = _values(avoid.get("times", answers.get("excluded_time_categories")))
    preset = str(answers.get("preferred_preset") or answers.get("preset") or "").strip()
    presets = {str(item).strip() for item in available_presets if str(item).strip()}
    conflicts: list[str] = []
    if preset and presets and preset not in presets:
        conflicts.append(f"首选预设不存在：{preset}")
        preset = ""
    overlap = set(scenes) & set(excluded_scenes)
    if overlap:
        conflicts.append("偏好和排除场景重复：" + ", ".join(sorted(overlap)))
        scenes = [item for item in scenes if item not in overlap]
    overlap_time = set(times) & set(excluded_times)
    if overlap_time:
        conflicts.append("偏好和排除时间重复：" + ", ".join(sorted(overlap_time)))
        times = [item for item in times if item not in overlap_time]
    if "outfit" in roles and not str(answers.get("outfit_category") or "").strip() and outfit_behavior != "ignore":
        conflicts.append("已选择穿搭职责，但没有填写服装类型")

    selection_eligibility = str(
        answers.get("selection_eligibility") or answers.get("fallback") or "matching_only"
    ).strip()
    if selection_eligibility not in _SELECTION_ELIGIBILITY:
        selection_eligibility = "matching_only"
    metadata: dict[str, Any] = {
        "editor_intent": {
            "version": 1,
            "preserve": roles,
            "outfit_behavior": outfit_behavior,
            "prefer": {"scenes": scenes, "times": times},
            "avoid": {"scenes": excluded_scenes, "times": excluded_times},
            "fallback": selection_eligibility,
            **({"manual_override": manual_override} if manual_override else {}),
        },
        "reference_roles": roles,
        "outfit_category": (
            str(answers.get("outfit_category") or "").strip()
            if "outfit" in roles and outfit_behavior != "ignore"
            else ""
        ),
        "outfit_lock_default": outfit_behavior == "preserve_unless_explicit_change",
        "scene_categories": scenes,
        "excluded_scene_categories": excluded_scenes,
        "time_categories": times,
        "excluded_time_categories": excluded_times,
        "selection_eligibility": selection_eligibility,
        "preferred_preset": preset,
        "metadata_source": "manual_override" if manual_override else "guided_editor",
    }
    normalized_questionnaire = answers.get("questionnaire") if isinstance(answers.get("questionnaire"), Mapping) else {}
    answer_sources: dict[str, list[str]] = {}
    for answer in list(normalized_questionnaire.get("answers") or ()):
        if not isinstance(answer, Mapping):
            continue
        question_id = str(answer.get("id") or "").strip()
        question_text = str(answer.get("question") or question_id).strip()
        for selection in list(answer.get("selections") or ()):
            if not isinstance(selection, Mapping):
                continue
            field_name = str(selection.get("field") or "").strip()
            label = str(selection.get("label") or selection.get("value") or "").strip()
            if field_name and label:
                source = f"{question_text}：{label}" if question_text else label
                answer_sources.setdefault(field_name, []).append(source)

    source_fields = {
        "reference_roles": ("core_anchor", "wardrobe_change", "location_change", "pose_change", "outfit_behavior"),
        "outfit_category": ("outfit_category",),
        "outfit_lock_default": ("outfit_behavior",),
        "scene_categories": ("prefer_scenes",),
        "excluded_scene_categories": ("avoid_scenes",),
        "time_categories": ("prefer_times",),
        "excluded_time_categories": ("avoid_times",),
        "selection_eligibility": ("fallback_policy",),
        "preferred_preset": ("preferred_preset",),
    }

    def field_source(field: str) -> str:
        if field in manual_override:
            return "manual_override"
        sources = list(
            dict.fromkeys(
                source
                for source_field in source_fields.get(field, (field,))
                for source in answer_sources.get(source_field, ())
            )
        )
        return "；".join(sources) or "规则合并"

    fields = tuple(
        MetadataField(field, value, field_source(field), _ROLE_LABELS.get(field, field))
        for field, value in (
            ("reference_roles", roles),
            ("outfit_category", metadata["outfit_category"]),
            ("outfit_lock_default", metadata["outfit_lock_default"]),
            ("scene_categories", scenes),
            ("excluded_scene_categories", excluded_scenes),
            ("time_categories", times),
            ("excluded_time_categories", excluded_times),
            ("selection_eligibility", metadata["selection_eligibility"]),
            ("preferred_preset", preset),
        )
    )
    old = _mapping(saved)
    differences = tuple(
        {"field": key, "saved": old.get(key), "generated": value}
        for key, value in metadata.items()
        if key != "editor_intent" and key in old and old.get(key) != value
    )
    missing = tuple(
        field for field in ("reference_roles", "selection_eligibility") if not metadata.get(field)
    )
    summary = _summary(metadata)
    trials = (
        {"label": "通用自拍", "message": "现在给我拍一张自然的自拍吧"},
        {"label": "冲突换装", "message": "晚上了，在卧室穿着睡衣给我拍一张吧"},
    )
    return CompileResult(metadata, summary, fields, differences, missing, tuple(conflicts), trials)


def _mapping(saved: Mapping[str, Any] | PhotoReference | None) -> Mapping[str, Any]:
    if isinstance(saved, PhotoReference):
        return {
            "reference_roles": list(saved.reference_roles),
            "outfit_category": saved.outfit_category,
            "outfit_lock_default": saved.outfit_lock_default,
            "scene_categories": list(saved.scene_categories),
            "excluded_scene_categories": list(saved.excluded_scene_categories),
            "time_categories": list(saved.time_categories),
            "excluded_time_categories": list(saved.excluded_time_categories),
            "selection_eligibility": saved.selection_eligibility,
            "preferred_preset": saved.preferred_preset,
        }
    return saved if isinstance(saved, Mapping) else {}


def _summary(metadata: Mapping[str, Any]) -> str:
    roles = [str(item) for item in metadata.get("reference_roles", ())]
    labels = "、".join(_ROLE_LABELS.get(role, role) for role in roles) or "不承担特定职责"
    outfit = metadata.get("outfit_lock_default")
    suffix = "；用户明确换装时才改变穿搭" if outfit else "；穿搭仅作参考，不强制保持"
    return f"这张图用于参考：{labels}{suffix}。"


def explain_reference_metadata(reference: PhotoReference | Mapping[str, Any]) -> ReferenceExplanation:
    data = _mapping(reference if isinstance(reference, PhotoReference) else reference)
    result = compile_reference_metadata(data.get("editor_intent") or data, ())
    warnings: list[str] = []
    if not data.get("editor_intent"):
        warnings.append("该条目没有保存维护者原始回答，显示的是兼容字段推导结果")
    return ReferenceExplanation(str(data.get("id") or ""), result.behavior_summary, result.fields, tuple(warnings))


__all__ = [
    "CompileResult",
    "MetadataField",
    "ReferenceExplanation",
    "build_reference_metadata_review_prompt",
    "compile_reference_metadata",
    "explain_reference_metadata",
    "merge_reference_questionnaire_evidence",
    "normalize_reviewed_reference_intent",
]
