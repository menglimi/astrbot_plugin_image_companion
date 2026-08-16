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


class StructuredWardrobePromptTests(unittest.TestCase):
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
