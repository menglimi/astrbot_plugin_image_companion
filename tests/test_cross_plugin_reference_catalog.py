from dataclasses import dataclass
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_image_companion.photo_reference_catalog import (  # noqa: E402
    PhotoReference,
    load_catalog,
    validate_and_serialize,
)
from astrbot_plugin_image_companion.photo_prompt_context import (  # noqa: E402
    PhotoPromptSection,
    resolve_photo_prompt_context,
)


@dataclass(frozen=True)
class ForeignPhotoReference:
    id: str
    kind: str
    source: str
    note: str
    reference_roles: tuple[str, ...]
    outfit_category: str
    outfit_lock_default: bool
    scene_categories: tuple[str, ...]
    preferred_preset: str
    metadata_source: str
    time_categories: tuple[str, ...] = ()
    editor_intent: dict | None = None
    excluded_scene_categories: tuple[str, ...] = ()
    excluded_time_categories: tuple[str, ...] = ()
    selection_eligibility: str = "matching_only"


@dataclass(frozen=True)
class ForeignPhotoPromptSection:
    name: str
    source: str
    positive: str
    negative: str = ""
    protected: bool = False
    sanitize_conflicts: bool | None = None


def test_foreign_reference_dataclasses_are_normalized_at_plugin_boundary() -> None:
    foreign_catalog = (
        ForeignPhotoReference(
            id="persona",
            kind="persona",
            source="linxiaoye.png",
            note="identity only",
            reference_roles=("identity",),
            outfit_category="",
            outfit_lock_default=False,
            scene_categories=(),
            preferred_preset="",
            metadata_source="configured",
        ),
        ForeignPhotoReference(
            id="sleepwear",
            kind="library",
            source="shuiyi.png",
            note="sleepwear",
            reference_roles=("identity", "outfit"),
            outfit_category="sleepwear",
            outfit_lock_default=True,
            scene_categories=("bedroom",),
            preferred_preset="居家睡衣",
            metadata_source="configured",
        ),
    )

    loaded = load_catalog(
        foreign_catalog,
        catalog_version=2,
        preset_names=("居家睡衣",),
    )

    assert [item.id for item in loaded.references] == ["persona", "sleepwear"]
    assert all(isinstance(item, PhotoReference) for item in loaded.references)
    assert loaded.references[1].reference_roles == ("identity", "outfit")
    assert loaded.references[1].outfit_lock_default is True

    serialized = validate_and_serialize(
        foreign_catalog,
        preset_names=("居家睡衣",),
    )
    assert [item["id"] for item in serialized] == ["persona", "sleepwear"]


def test_foreign_prompt_sections_are_normalized_before_strict_validation() -> None:
    resolved = resolve_photo_prompt_context(
        wardrobe={},
        sections=(
            ForeignPhotoPromptSection(
                name="user_request",
                source="user_request",
                positive="pink sleepwear in a bedroom",
                protected=True,
            ),
        ),
        prompt_format="traditional",
        workflow_kind="selfie",
    )

    assert len(resolved.prompt_sections) == 1
    assert isinstance(resolved.prompt_sections[0], PhotoPromptSection)
    assert resolved.prompt_sections[0].positive == "pink sleepwear in a bedroom"
