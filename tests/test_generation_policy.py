from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generation_policy import (  # noqa: E402
    DEFAULT_CANONICAL_OUTFIT_NEGATIVES,
    character_identity_appearance_from_persona,
    apply_ambient_wardrobe_intent,
    extract_temperature_facts,
    infer_ambient_wardrobe_policy,
    outfit_context_fingerprint,
    hot_outfit_fields,
    infer_outfit_mode,
    resolve_structured_outfit,
)
from photo_wardrobe_decision import (  # noqa: E402
    PhotoWardrobeIntent,
    resolve_photo_wardrobe_decision,
)


class TemperatureParsingTests(unittest.TestCase):
    def test_character_identity_excludes_clothing_fields(self):
        appearance = character_identity_appearance_from_persona(
            "外貌：粉色长发\n瞳色：金色\n服装：黑色针织毛衣\n服饰风格：冬季叠穿",
            "星形发夹",
        )
        text = " | ".join(appearance)
        self.assertIn("粉色长发", text)
        self.assertIn("金色", text)
        self.assertIn("星形发夹", text)
        self.assertNotIn("毛衣", text)
        self.assertNotIn("冬季叠穿", text)
    def test_numeric_temperature_is_hot_without_weather_keyword(self):
        facts = extract_temperature_facts("山东淄博，晴，当前 33°C，体感温度 36℃")
        self.assertEqual(33, facts.temperature_c)
        self.assertEqual(36, facts.feels_like_c)
        self.assertEqual("hot", facts.thermal_level)

    def test_provider_mapping_prefers_feels_like(self):
        facts = extract_temperature_facts({"temp": "25", "feelsLike": "31"})
        self.assertEqual("hot", facts.thermal_level)

    def test_outfit_cache_changes_for_each_policy_dimension(self):
        base = {
            "daypart": "evening",
            "location_type": "home",
            "current_activity": "reading",
            "thermal_level": "hot",
            "route_key": "comfyui/anima/selfie/wf-a",
        }
        original = outfit_context_fingerprint(**base)
        for key, replacement in (
            ("daypart", "late_night"),
            ("location_type", "outdoor"),
            ("current_activity", "running"),
            ("thermal_level", "cold"),
            ("route_key", "comfyui/anima/selfie/wf-b"),
        ):
            self.assertNotEqual(original, outfit_context_fingerprint(**{**base, key: replacement}), key)

    def test_hot_outfit_profiles_contain_no_cold_weather_items(self):
        forbidden = ("sweater", "knit", "hoodie", "coat", "scarf", "wool", "毛衣", "针织")
        for scene in ("school", "commute", "sport", "home", "daily"):
            for index in range(6):
                text = " ".join(hot_outfit_fields(scene, index).values()).lower()
                self.assertFalse(any(term in text for term in forbidden), (scene, text))


class AmbientWardrobeTests(unittest.TestCase):
    def test_hot_homewear_expands_to_one_concrete_outfit(self):
        outfit = resolve_structured_outfit(
            category="homewear",
            thermal_level="hot",
            context_key="2026-08-16T09:54|home|33C",
            request_text="来张上午居家自拍",
        )
        text = ", ".join(outfit.positive_tags()).lower()
        self.assertEqual("free_outfit", outfit.mode)
        self.assertIn("t-shirt", text)
        self.assertIn("short sleeves", text)
        self.assertIn("lounge shorts", text)
        self.assertIn("slippers", text)
        self.assertNotRegex(text, r"\bor\b")
        self.assertIn("armored collar", outfit.forbidden_details)

    def test_canonical_and_real_reference_modes_are_isolated(self):
        self.assertEqual("canonical_outfit", infer_outfit_mode("换回官方作战服"))
        self.assertEqual("reference_outfit", infer_outfit_mode("照这个穿搭", has_outfit_reference=True))
        canonical = resolve_structured_outfit(
            category="daily_outfit", thermal_level="mild", context_key="canonical",
            request_text="换回官方作战服",
        )
        self.assertEqual((), canonical.forbidden_details)
        referenced = resolve_structured_outfit(
            category="daily_outfit", thermal_level="mild", context_key="reference",
            request_text="照这个穿搭", has_outfit_reference=True,
        )
        self.assertIn("submitted outfit reference image", " ".join(referenced.positive_tags()))

    def test_explicit_outfit_is_not_mixed_with_catalog_default(self):
        outfit = resolve_structured_outfit(
            category="custom_outfit", thermal_level="hot", context_key="explicit",
            request_text="oversized black pullover hoodie, blue denim shorts, bare legs, white low-top sneakers",
        )
        text = ", ".join(outfit.positive_tags()).lower()
        self.assertIn("black pullover hoodie", text)
        self.assertIn("denim shorts", text)
        self.assertNotIn("mint green", text)
        self.assertTrue(set(DEFAULT_CANONICAL_OUTFIT_NEGATIVES).issubset(outfit.forbidden_details))

    def test_late_night_home_context_selects_sleepwear(self):
        policy = infer_ambient_wardrobe_policy(
            workflow_kind="selfie",
            scene_context="时间：2026-08-15 00:08（深夜）；当前日程：准备睡觉；当前位置：卧室；天气背景：晴 29°C",
        )
        self.assertEqual("sleepwear", policy.category)
        self.assertEqual("hot", policy.thermal_level)
        self.assertIn("wool sweater", policy.forbidden)

    def test_home_context_selects_homewear(self):
        policy = infer_ambient_wardrobe_policy(
            workflow_kind="portrait",
            scene_context="时间：18:20；当前位置：家里；当前日程：整理房间",
        )
        self.assertEqual("homewear", policy.category)

    def test_user_request_and_exclusion_outrank_ambient(self):
        explicit = PhotoWardrobeIntent(target_category="cosplay", source="user_prompt")
        ambient = infer_ambient_wardrobe_policy(
            workflow_kind="selfie",
            scene_context="时间：00:08；当前位置：卧室；当前日程：准备睡觉",
        )
        self.assertIs(explicit, apply_ambient_wardrobe_intent(explicit, ambient))

        excluded = PhotoWardrobeIntent(excluded_categories=("sleepwear",))
        self.assertIs(excluded, apply_ambient_wardrobe_intent(excluded, ambient))

    def test_ambient_source_is_not_reported_as_user_prompt(self):
        intent = apply_ambient_wardrobe_intent(
            PhotoWardrobeIntent(),
            infer_ambient_wardrobe_policy(
                workflow_kind="selfie",
                scene_context="时间：00:08；当前位置：卧室；当前日程：准备睡觉",
            ),
        )
        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="来张自拍",
            intent=intent,
            reference={
                "id": "daily",
                "path": "/tmp/daily.png",
                "kind": "daily_outfit",
                "outfit_category": "daily_outfit",
                "outfit_lock_default": True,
                "reference_roles": ["identity", "outfit"],
            },
            scene_context="时间：00:08；当前位置：卧室；当天基础穿搭：针织毛衣",
            available_presets=("居家睡衣", "日常穿搭"),
        )
        self.assertEqual("ambient_context", decision.source)
        self.assertEqual("sleepwear", decision.category)
        self.assertNotIn("outfit", decision.effective_reference_roles)
        self.assertNotIn("当天基础穿搭", decision.scene_context)

    def test_no_reference_path_never_claims_selected_reference(self):
        decision = resolve_photo_wardrobe_decision(
            workflow_kind="selfie",
            prompt_text="comfortable homewear",
            intent=PhotoWardrobeIntent(target_category="homewear", target_text="comfortable homewear"),
            reference=None,
            available_presets=("居家服",),
        )
        self.assertNotIn("selected reference", decision.positive_instruction.lower())
        self.assertIn("without assuming", decision.positive_instruction.lower())


if __name__ == "__main__":
    unittest.main()
