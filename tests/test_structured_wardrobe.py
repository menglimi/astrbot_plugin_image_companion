from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACKAGE = "image_companion_wardrobe_testpkg"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    package.__package__ = PACKAGE
    sys.modules[PACKAGE] = package

from image_companion_wardrobe_testpkg.photo_prompt_context import (  # noqa: E402
    PhotoPromptSection,
    _assemble,
    _budget_sections,
    compile_local_photo_negative_prompt,
    compile_local_photo_prompt,
)
from image_companion_wardrobe_testpkg.generation_policy import resolve_structured_outfit  # noqa: E402


class StructuredWardrobePromptTests(unittest.TestCase):
    def test_abstract_sleepwear_request_expands_to_concrete_structure(self):
        outfit = resolve_structured_outfit(
            category="sleepwear",
            thermal_level="hot",
            context_key="2026-08-16T23:45+08:00",
            request_text="准备睡了，看看睡衣",
        )
        tags = ", ".join(outfit.positive_tags()).lower()
        self.assertTrue("pajama" in tags or "sleep t-shirt" in tags)
        self.assertNotIn("看看睡衣", tags)
        self.assertIn("short sleeves", tags)

    def test_detailed_sleepwear_request_remains_explicit(self):
        outfit = resolve_structured_outfit(
            category="sleepwear",
            thermal_level="hot",
            context_key="2026-08-16T23:45+08:00",
            request_text="蓝色棉质短袖睡衣",
        )
        tags = ", ".join(outfit.positive_tags())
        self.assertIn("蓝色棉质短袖睡衣", tags)
        self.assertIn("materials exactly as requested", tags)

    def test_protected_wardrobe_survives_legacy_prompt_budget(self):
        positives = ", ".join(f"garment-detail-{index}" for index in range(80))
        negatives = ", ".join(f"canonical-detail-{index}" for index in range(50))
        sections = [
            PhotoPromptSection("request", "user_request", positive="来张居家自拍", protected=True),
            PhotoPromptSection(
                "structured", "wardrobe_structure", positive=positives, negative=negatives, protected=True,
            ),
            PhotoPromptSection("scene", "scene_context", positive="scene " * 1000),
        ]
        prompt = _assemble(_budget_sections(sections), "traditional")
        self.assertIn("garment-detail-79", prompt)
        self.assertIn("canonical-detail-49", prompt)
        self.assertIn("Negative prompt", prompt)
        local_positive = compile_local_photo_prompt(_budget_sections(sections), "traditional")
        local_negative = compile_local_photo_negative_prompt(_budget_sections(sections), "traditional")
        self.assertIn("garment-detail-79", local_positive)
        self.assertIn("canonical-detail-49", local_negative)


if __name__ == "__main__":
    unittest.main()
