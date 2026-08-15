# -*- coding: utf-8 -*-
"""Model-specific prompt compilers isolated from execution backends."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Protocol

try:
    from .generation_contracts import GenerationSpecV1, PromptPackageV1
except ImportError:  # pragma: no cover - standalone test/import fallback
    from generation_contracts import GenerationSpecV1, PromptPackageV1


def _join(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(dict.fromkeys(value.strip() for value in values if value and value.strip()))


FRONT_FACING_CAMERA_PORTRAIT = "front-facing camera perspective, arm's-length portrait"
SELFIE_UI_NEGATIVE_TERMS = (
    "social media interface",
    "mobile app UI",
    "camera UI",
    "livestream overlay",
    "profile avatar",
    "username",
    "buttons",
    "interface icons",
    "timestamp",
    "subtitle",
    "caption",
    "text",
    "watermark",
    "logo",
    "thumbnail strip",
    "picture-in-picture",
    "screenshot",
    "screen border",
    "decorative frame",
    "HUD",
    "navigation bar",
)


def append_selfie_ui_negative(value: str) -> str:
    parts = [part.strip() for part in str(value or "").split(",") if part.strip()]
    return _join([*parts, *SELFIE_UI_NEGATIVE_TERMS])


def _scene_phrases(spec: GenerationSpecV1) -> list[str]:
    scene = spec.scene
    values = [
        spec.composition.location or scene.location_text,
        scene.time_phase.replace("_", " "),
        scene.current_activity,
        spec.composition.lighting,
        spec.composition.shot,
        *spec.composition.instructions,
    ]
    return [value for value in values if value]


class PromptCompiler(Protocol):
    profile: str
    version: str

    def compile(self, spec: GenerationSpecV1) -> PromptPackageV1: ...


class ModelProfileRegistry:
    def __init__(self) -> None:
        self._compilers: dict[str, PromptCompiler] = {}

    def register(self, compiler: PromptCompiler, *aliases: str) -> None:
        names = (compiler.profile, *aliases)
        for name in names:
            key = str(name or "").strip().lower().replace("-", "_")
            if not key:
                raise ValueError("model profile name is required")
            if key in self._compilers and self._compilers[key] is not compiler:
                raise ValueError(f"duplicate model profile: {key}")
            self._compilers[key] = compiler

    def get(self, profile: str) -> PromptCompiler:
        key = str(profile or "legacy").strip().lower().replace("-", "_")
        if key not in self._compilers:
            raise KeyError(f"unknown model profile: {profile}")
        return self._compilers[key]

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._compilers))


class LegacyPromptCompiler:
    profile = "legacy"
    version = "1"

    def compile(self, spec: GenerationSpecV1) -> PromptPackageV1:
        positive = spec.legacy_prompt.strip() or spec.user_request.strip()
        return PromptPackageV1(
            model_profile=self.profile,
            positive_prompt=positive,
            required_concepts=spec.required_concepts,
            forbidden_concepts=spec.forbidden_concepts,
            compiler_version=self.version,
        )


class AnimaPromptCompiler:
    profile = "anima"
    version = "1"
    quality = ("masterpiece", "best quality", "newest", "safe", "highres")

    @staticmethod
    def _remove_nai_syntax(value: str) -> str:
        value = re.sub(r"-?\d+(?:\.\d+)?::(.*?)::", r"\1", value)
        value = value.replace("{", "").replace("}", "").replace("[", "").replace("]", "")
        value = re.sub(r"\b(?:source|target)#\S+", "", value, flags=re.I)
        return " ".join(value.split()).strip(" ,")

    def compile(self, spec: GenerationSpecV1) -> PromptPackageV1:
        appearance = list(spec.character.appearance)
        if spec.character.fixed_accessories:
            appearance.extend(spec.character.fixed_accessories)
        wardrobe = list(spec.wardrobe.required)
        required = list(spec.required_concepts)
        positive_tags = [
            *self.quality,
            "1girl" if spec.composition.subject_count == 1 else f"{spec.composition.subject_count}people",
            spec.character.default_style or "anime illustration",
            *appearance,
            *wardrobe,
            *required,
        ]
        tag_block = _join([self._remove_nai_syntax(value) for value in positive_tags])
        prose_values = [self._remove_nai_syntax(value) for value in _scene_phrases(spec)]
        prose = ". ".join(value for value in prose_values if value)
        positive = f"{tag_block}. {prose}".strip(" .")
        negative_values = [
            *spec.character.forbidden_features,
            *spec.wardrobe.forbidden,
            *spec.forbidden_concepts,
        ]
        negative = _join([self._remove_nai_syntax(value) for value in negative_values])
        if spec.operation in {"selfie", "portrait"}:
            negative = append_selfie_ui_negative(negative)
        return PromptPackageV1(
            model_profile=self.profile,
            positive_prompt=positive,
            negative_prompt=negative,
            required_concepts=tuple(dict.fromkeys((*spec.required_concepts, *spec.wardrobe.required))),
            forbidden_concepts=tuple(dict.fromkeys((*spec.forbidden_concepts, *spec.wardrobe.forbidden))),
            compiler_version=self.version,
        )


class NaiPromptCompiler:
    profile = "nai"
    version = "1"

    def compile(self, spec: GenerationSpecV1) -> PromptPackageV1:
        positive = _join(
            [
                "masterpiece", "best quality", "1girl",
                *spec.character.appearance,
                *spec.character.fixed_accessories,
                *spec.wardrobe.required,
                *_scene_phrases(spec),
                *spec.required_concepts,
            ]
        )
        negative = _join(
            [*spec.character.forbidden_features, *spec.wardrobe.forbidden, *spec.forbidden_concepts]
        )
        return PromptPackageV1(
            model_profile=self.profile,
            positive_prompt=positive,
            negative_prompt=negative,
            required_concepts=spec.required_concepts,
            forbidden_concepts=spec.forbidden_concepts,
            compiler_version=self.version,
        )


class GenericNaturalPromptCompiler:
    profile = "generic_natural"
    version = "1"

    def compile(self, spec: GenerationSpecV1) -> PromptPackageV1:
        parts = [
            spec.user_request,
            f"Character: {_join([*spec.character.appearance, *spec.character.fixed_accessories])}.",
            f"Wear exactly {_join(list(spec.wardrobe.required))}." if spec.wardrobe.required else "",
            f"Scene: {'. '.join(_scene_phrases(spec))}." if _scene_phrases(spec) else "",
        ]
        negative = _join([*spec.wardrobe.forbidden, *spec.forbidden_concepts])
        return PromptPackageV1(
            model_profile=self.profile,
            positive_prompt=" ".join(part for part in parts if part).strip(),
            negative_prompt=negative,
            required_concepts=spec.required_concepts,
            forbidden_concepts=spec.forbidden_concepts,
            compiler_version=self.version,
        )


class GenericTagPromptCompiler(NaiPromptCompiler):
    profile = "generic_tags"


def default_model_profile_registry() -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    registry.register(LegacyPromptCompiler(), "legacy_traditional", "legacy_natural")
    registry.register(AnimaPromptCompiler())
    registry.register(NaiPromptCompiler(), "novelai")
    registry.register(GenericNaturalPromptCompiler(), "natural_language")
    registry.register(GenericTagPromptCompiler(), "traditional")
    return registry


__all__ = [
    "PromptCompiler", "ModelProfileRegistry", "LegacyPromptCompiler",
    "AnimaPromptCompiler", "NaiPromptCompiler", "GenericNaturalPromptCompiler",
    "GenericTagPromptCompiler", "default_model_profile_registry",
    "FRONT_FACING_CAMERA_PORTRAIT", "SELFIE_UI_NEGATIVE_TERMS", "append_selfie_ui_negative",
]
