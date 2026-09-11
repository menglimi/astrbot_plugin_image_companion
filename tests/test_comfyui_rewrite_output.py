import json

import pytest

from astrbot_plugin_image_companion.comfyui_prompts import _normalize_rewrite_output, rewrite_prompts
from astrbot_plugin_image_companion.comfyui_workflows import WorkflowError, fill_workflow


FIELDS = {
    "clothing_prompt": {"node_id": "1", "input_name": "clothing_tags", "kind": "prompt", "mode": "replace"},
    "pose_prompt": {"node_id": "1", "input_name": "pose_tags", "kind": "prompt", "mode": "replace"},
    "background_prompt": {"node_id": "1", "input_name": "background_tags", "kind": "prompt", "mode": "replace"},
    "extra_prompt": {"node_id": "1", "input_name": "extra_prompt", "kind": "prompt", "mode": "replace"},
    "negative_prompt": {"node_id": "2", "input_name": "text", "kind": "prompt", "mode": "append"},
}
POSITIVE = {"clothing_prompt": "blue shirt", "pose_prompt": "sitting", "background_prompt": "window", "extra_prompt": "soft light"}


@pytest.mark.parametrize("negative", ["missing", None, "", " \n\t "])
def test_empty_negative_retains_companion_constraints_and_workflow_defaults(negative):
    raw = dict(POSITIVE)
    if negative != "missing":
        raw["negative_prompt"] = negative
    slots, _ = _normalize_rewrite_output({"slots": raw}, FIELDS, {"negative_prompt": "thick coat"})
    assert slots["negative_prompt"] == "thick coat"
    workflow = {"1": {"inputs": {v["input_name"]: "" for v in FIELDS.values() if v["node_id"] == "1"}}, "2": {"inputs": {"text": "bad anatomy"}}}
    assert fill_workflow(workflow, {"fields": FIELDS}, slots)["2"]["inputs"]["text"] == "bad anatomy, thick coat"


def test_flat_json_and_unique_node_input_aliases_are_normalized():
    raw = {FIELDS[k]["input_name"]: v for k, v in POSITIVE.items()}
    slots, orientation = _normalize_rewrite_output({**raw, "orientation": "landscape"}, FIELDS, {})
    assert slots == {**POSITIVE, "negative_prompt": ""}
    assert orientation == "landscape"


def test_orientation_inside_slots_is_metadata_not_a_workflow_input():
    slots, orientation = _normalize_rewrite_output({"slots": {**POSITIVE, "orientation": "portrait"}}, FIELDS, {})
    assert "orientation" not in slots and orientation == "portrait"
    with pytest.raises(WorkflowError, match="冲突"):
        _normalize_rewrite_output({"slots": {**POSITIVE, "orientation": "portrait"}, "orientation": "landscape"}, FIELDS, {})


def test_missing_required_slot_only_uses_authoritative_supplied_content():
    raw = {k: v for k, v in POSITIVE.items() if k != "clothing_prompt"}
    with pytest.raises(WorkflowError, match="缺少=clothing_prompt"):
        _normalize_rewrite_output({"slots": raw}, FIELDS, {})
    slots, _ = _normalize_rewrite_output({"slots": raw}, FIELDS, {"semantic_prompt_slots": {"clothing_prompt": "confirmed pajamas"}})
    assert slots["clothing_prompt"] == "confirmed pajamas"


def test_unknown_fields_duplicates_and_wrong_types_still_fail():
    with pytest.raises(WorkflowError, match="未知=seed"):
        _normalize_rewrite_output({"slots": {**POSITIVE, "seed": 123}}, FIELDS, {})
    with pytest.raises(WorkflowError, match="重复"):
        _normalize_rewrite_output({"slots": {**POSITIVE, "clothing_tags": "red shirt"}}, FIELDS, {})
    with pytest.raises(WorkflowError, match="类型错误"):
        _normalize_rewrite_output({"slots": {**POSITIVE, "pose_prompt": ["sitting"]}}, FIELDS, {})
    with pytest.raises(WorkflowError, match="slots 必须为对象"):
        _normalize_rewrite_output({"slots": "not an object"}, FIELDS, {})


def test_ambiguous_text_alias_does_not_choose_an_arbitrary_encoder():
    fields = {k: {"input_name": "text"} for k in ("positive_prompt", "negative_prompt")}
    with pytest.raises(WorkflowError, match="未知=text"):
        _normalize_rewrite_output({"slots": {"text": "portrait"}}, fields, {})


@pytest.mark.asyncio
async def test_prompt_contains_exact_output_template_and_uses_one_call():
    workflow = {"1": {"inputs": {v["input_name"]: "" for v in FIELDS.values() if v["node_id"] == "1"}}, "2": {"inputs": {"text": "fixed negative"}}}
    calls = []
    async def model(prompt):
        calls.append(prompt)
        expected = json.dumps({"slots": {name: "" for name in FIELDS}, "orientation": "portrait"}, ensure_ascii=False)
        assert expected in prompt
        return json.dumps({"slots": POSITIVE, "orientation": "portrait"})
    slots, orientation = await rewrite_prompts(workflow, {"fields": FIELDS}, {"prompt_text": "selfie"}, model)
    assert len(calls) == 1
    assert slots == {**POSITIVE, "negative_prompt": ""} and orientation == "portrait"
