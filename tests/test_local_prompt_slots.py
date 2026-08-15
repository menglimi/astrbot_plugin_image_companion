from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "image_companion_testpkg"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    package.__package__ = PACKAGE
    sys.modules[PACKAGE] = package

from image_companion_testpkg.photo_prompt_context import (  # noqa: E402
    PhotoPromptSection,
    compile_local_photo_negative_prompt,
    compile_local_photo_prompt,
)


class LocalPromptSlotTests(unittest.TestCase):
    def test_positive_and_negative_slots_are_distinct(self):
        sections = (
            PhotoPromptSection("request", "user_request", positive="summer pajamas", negative="wool sweater"),
            PhotoPromptSection("scene", "scene_context", positive="bedroom at night"),
            PhotoPromptSection("composition", "composition", positive="single coherent portrait", negative="multiple people"),
        )
        positive = compile_local_photo_prompt(sections, "traditional")
        negative = compile_local_photo_negative_prompt(sections, "traditional")
        self.assertIn("summer pajamas", positive)
        self.assertNotIn("wool sweater", positive)
        self.assertIn("wool sweater", negative)
        self.assertIn("multiple people", negative)
        self.assertNotEqual(positive, negative)

    def test_negative_slot_deduplicates_constraints(self):
        sections = (
            PhotoPromptSection("a", "user_request", negative="text, watermark"),
            PhotoPromptSection("b", "fixed_prompt", negative="text, watermark"),
        )
        self.assertEqual("text, watermark", compile_local_photo_negative_prompt(sections, "traditional"))


if __name__ == "__main__":
    unittest.main()
