from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generation_config import (  # noqa: E402
    active_engine_claims_profile,
    build_route_registry,
    legacy_migration_preview,
    parse_rollout_config,
    route_diagnostics,
)


class GenerationConfigTests(unittest.TestCase):
    def test_instant_rollback_overrides_active_mode(self):
        rollout = parse_rollout_config({"engine": {"mode": "active", "instant_rollback": True}})
        self.assertFalse(rollout.engine_enabled)
        self.assertFalse(rollout.shadow_mode)

    def test_route_validation_and_paid_fallback_warning(self):
        config = {
            "engine": {
                "routes": [
                    {
                        "name": "anima",
                        "backend": "comfyui",
                        "model_profile": "anima",
                        "operation": "selfie",
                        "workflow": "anima.json",
                        "fallback_routes": ["online"],
                        "allow_paid_fallback": False,
                    },
                    {
                        "name": "online",
                        "backend": "external",
                        "model_profile": "generic_natural",
                        "operation": "selfie",
                        "workflow": "endpoint-1",
                        "paid": True,
                    },
                ]
            }
        }
        registry, result = build_route_registry(config)
        self.assertTrue(result.ok)
        self.assertEqual(2, len(registry.list()))
        self.assertIn("will be skipped", result.warnings[0])

    def test_conflicting_or_incomplete_routes_fail_closed(self):
        _, result = build_route_registry({
            "engine": {"routes": [
                {"name": "bad", "backend": "comfyui", "model_profile": "unknown", "operation": "selfie"},
                {"name": "bad", "backend": "external", "model_profile": "nai", "workflow": "x"},
            ]}
        })
        self.assertFalse(result.ok)
        self.assertGreaterEqual(len(result.errors), 2)

    def test_workflow_mapping_is_bound_to_route_and_changes_cache_identity(self):
        registry, result = build_route_registry({
            "engine": {
                "routes": [{
                    "name": "anima", "backend": "comfyui", "model_profile": "anima",
                    "operation": "selfie", "workflow": "custom.json",
                }],
                "workflow_mappings": [{
                    "route": "anima", "fingerprint": "sha256:abc", "mapping_version": 3,
                    "slots": {"positive_prompt": {"node_id": "41", "input_name": "text"}},
                }],
            }
        })
        self.assertTrue(result.ok)
        route = registry.get("anima")
        self.assertEqual("41", route.settings["mapping"]["positive_prompt"]["node_id"])
        self.assertEqual("sha256:abc", route.settings["workflow_fingerprint"])
        self.assertEqual(3, route.settings["mapping_version"])

    def test_fallback_cycle_is_rejected_before_runtime(self):
        _, result = build_route_registry({"engine": {"routes": [
            {"name": "a", "backend": "comfyui", "model_profile": "anima", "operation": "selfie", "workflow": "a", "fallback_routes": ["b"]},
            {"name": "b", "backend": "comfyui", "model_profile": "anima", "operation": "selfie", "workflow": "b", "fallback_routes": ["a"]},
        ]}})
        self.assertFalse(result.ok)
        self.assertTrue(any("fallback cycle" in error for error in result.errors))

    def test_legacy_preview_does_not_apply_or_leak_credentials(self):
        preview = legacy_migration_preview({
            "image": {
                "photo_generation_backend": "auto",
                "comfyui_selfie_workflow_name": "Anima",
                "external_image_api_endpoints": [{
                    "name": "paid", "platform": "openai", "model": "gpt-image-2", "api_key": "secret",
                }],
            }
        })
        self.assertFalse(preview["changes_applied"])
        self.assertNotIn("secret", str(preview))
        self.assertEqual(2, len(preview["routes"]))

    def test_diagnostics_include_dual_track_state(self):
        diagnostics = route_diagnostics({
            "engine": {"mode": "shadow", "routes": []},
            "image": {"external_image_api_endpoints": [{
                "name": "nai-proxy", "platform": "novelai", "model": "nai-v4", "api_key": "secret",
                "capabilities": {"negative_prompt": True, "max_reference_images": 0},
            }]},
        })
        self.assertTrue(diagnostics["effective_engine_enabled"])
        self.assertTrue(diagnostics["effective_shadow_mode"])
        self.assertIn("migration_preview", diagnostics)
        self.assertEqual("nai", diagnostics["online_endpoints"][0]["model_profile"])
        self.assertNotIn("secret", str(diagnostics))
        self.assertIn("anima", diagnostics["available_model_profiles"])

    def test_active_route_ownership_is_explicit_and_shadow_safe(self):
        route = {
            "name": "nai-online",
            "backend": "external",
            "model_profile": "nai",
            "operation": "selfie",
            "workflow": "nai-endpoint",
        }
        self.assertTrue(active_engine_claims_profile({
            "engine": {"mode": "active", "routes": [route]},
        }, "nai"))
        self.assertFalse(active_engine_claims_profile({
            "engine": {"mode": "shadow", "routes": [route]},
        }, "nai"))
        self.assertFalse(active_engine_claims_profile({
            "engine": {"mode": "active", "instant_rollback": True, "routes": [route]},
        }, "nai"))


if __name__ == "__main__":
    unittest.main()
