from __future__ import annotations

import asyncio
from dataclasses import replace
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generation_contracts import (  # noqa: E402
    BackendCapabilitiesV1,
    CharacterIdentitySpecV1,
    CompositionSpecV1,
    GenerationResultV1,
    GenerationSpecV1,
    ReferenceBindingV1,
    SceneContextV1,
    WardrobeSpecV1,
    WeatherFactsV1,
    thermal_level_for_temperature,
)
from generation_engine import (  # noqa: E402
    GenerationEngine,
    RouteDefinition,
    RouteKey,
    RouteRegistry,
    ReferencePlanner,
    route_cache_key,
)
from generation_adapters import LegacyCallbackAdapter  # noqa: E402
from generation_debug import GenerationDebugConfig, GenerationDebugRecorder  # noqa: E402
from generation_profiles import (  # noqa: E402
    AnimaPromptCompiler,
    FRONT_FACING_CAMERA_PORTRAIT,
    SELFIE_UI_NEGATIVE_TERMS,
    append_selfie_ui_negative,
    default_model_profile_registry,
)


def _spec(*, request_id: str = "r1") -> GenerationSpecV1:
    return GenerationSpecV1(
        schema_version=1,
        request_id=request_id,
        operation="selfie",
        user_request="来张自拍",
        scene=SceneContextV1(
            captured_at="2026-08-15T00:08:00+08:00",
            local_time="00:08",
            time_phase="late_night",
            location_text="bedroom",
            location_type="bedroom",
            indoor=True,
            current_activity="preparing for sleep",
            sleep_phase="preparing_for_sleep",
            weather=WeatherFactsV1(temperature_c=30, feels_like_c=32),
        ),
        character=CharacterIdentitySpecV1(
            name="小爱",
            appearance=("long pink hair", "golden eyes"),
            default_style="anime illustration",
        ),
        wardrobe=WardrobeSpecV1(
            category="sleepwear",
            required=("lightweight summer pajamas",),
            forbidden=("wool sweater", "thick jacket"),
            thermal_level="hot",
        ),
        references=(
            ReferenceBindingV1("identity", "/tmp/identity.png", roles=("identity",), priority=10),
        ),
        composition=CompositionSpecV1(
            shot="upper body selfie", location="bedroom", lighting="warm bedside light"
        ),
    )


class _Adapter:
    def __init__(self, backend: str, *, fail: bool = False, max_refs: int = 1) -> None:
        self.backend = backend
        self.fail = fail
        self.max_refs = max_refs
        self.prompts = []

    async def capabilities(self, route):
        return BackendCapabilitiesV1(
            negative_prompt=True,
            max_reference_images=self.max_refs,
            reference_roles=("identity", "outfit"),
        )

    async def generate(self, route, spec, prompt, references, trace):
        self.prompts.append(prompt)
        if self.fail:
            return GenerationResultV1(
                request_id=spec.request_id,
                backend=self.backend,
                model_profile=prompt.model_profile,
                error_code="submission_failed",
            )
        return GenerationResultV1(
            request_id=spec.request_id,
            task_id="task",
            backend=self.backend,
            model_profile=prompt.model_profile,
            workflow=route.key.workflow,
            image_path="/tmp/result.png",
            submitted_reference_ids=tuple(item.reference_id for item in references.submitted),
            generation_completed=True,
            trace=tuple(trace),
        )


class _TimeoutAdapter(_Adapter):
    async def generate(self, route, spec, prompt, references, trace):
        raise TimeoutError("injected timeout")


class ContractAndCompilerTests(unittest.TestCase):
    def test_reference_planner_preserves_subjects_and_reports_capacity_degradation(self):
        references = (
            ReferenceBindingV1("bot", "/tmp/bot.png", roles=("identity",), subject="bot", priority=10),
            ReferenceBindingV1("friend", "/tmp/friend.png", roles=("relationship_role",), subject="friend", priority=9),
        )
        plan = ReferencePlanner().plan(
            references,
            BackendCapabilitiesV1(max_reference_images=1, reference_roles=("identity", "relationship_role")),
        )
        self.assertEqual(("bot",), tuple(item.subject for item in plan.submitted))
        self.assertEqual(("friend",), tuple(item.subject for item in plan.omitted))
        self.assertIn("references:1/2", plan.degraded_capabilities)
    def test_temperature_thresholds_use_feels_like(self):
        self.assertEqual("hot", thermal_level_for_temperature(28))
        self.assertEqual("warm", thermal_level_for_temperature(27.9))
        self.assertEqual("mild", thermal_level_for_temperature(13))
        self.assertEqual("cool", thermal_level_for_temperature(5))
        self.assertEqual("cold", thermal_level_for_temperature(4.9))
        self.assertEqual("hot", WeatherFactsV1(temperature_c=20, feels_like_c=31).thermal_level)

    def test_anima_compiler_separates_negative_and_strips_nai_syntax(self):
        compiled = AnimaPromptCompiler().compile(_spec())
        self.assertIn("lightweight summer pajamas", compiled.positive_prompt)
        self.assertIn("wool sweater", compiled.negative_prompt)
        self.assertNotIn("wool sweater", compiled.positive_prompt)
        self.assertNotRegex(compiled.positive_prompt, r"\d+(?:\.\d+)?::|[{}\[\]]")

    def test_anima_compiler_receives_structured_outfit_terms(self):
        from generation_policy import resolve_structured_outfit
        outfit = resolve_structured_outfit(
            category="homewear", thermal_level="hot", context_key="09:54", request_text="居家服",
        )
        required = outfit.positive_tags()
        spec = replace(_spec(), wardrobe=replace(
            _spec().wardrobe,
            category="homewear",
            required=required,
            forbidden=outfit.forbidden_details,
            outfit=outfit,
        ))
        compiled = AnimaPromptCompiler().compile(spec)
        self.assertIn("lounge shorts", compiled.positive_prompt)
        self.assertIn("armored collar", compiled.negative_prompt)
        self.assertNotIn("armored collar", compiled.positive_prompt)

    def test_selfie_ui_contract_is_added_to_anima_negative_prompt(self):
        compiled = AnimaPromptCompiler().compile(_spec())
        for term in SELFIE_UI_NEGATIVE_TERMS:
            self.assertIn(term, compiled.negative_prompt)
        self.assertNotIn("mobile app UI", compiled.positive_prompt)

    def test_selfie_ui_negative_helper_deduplicates_existing_terms(self):
        compiled = append_selfie_ui_negative("bad hands, text, watermark")
        self.assertEqual(1, compiled.split(", ").count("text"))
        self.assertEqual(1, compiled.split(", ").count("watermark"))
        self.assertIn("camera UI", compiled)
        self.assertEqual("front-facing camera perspective, arm's-length portrait", FRONT_FACING_CAMERA_PORTRAIT)

    def test_legacy_selfie_prompts_do_not_reintroduce_phone_ui_cues(self):
        source = (ROOT / "image_runtime.py").read_text(encoding="utf-8")
        for stale in (
            "handheld selfie",
            "natural phone snapshot",
            "phone snapshot feeling",
            "selfie-inspired outfit portrait composition",
        ):
            self.assertNotIn(stale, source)
        for required in ("front-facing camera perspective", "SELFIE_UI_NEGATIVE_TERMS", "append_selfie_ui_negative"):
            self.assertIn(required, source)

    def test_route_cache_is_isolated_by_profile_and_workflow(self):
        spec = _spec()
        first = RouteDefinition("a", RouteKey("comfyui", "anima", "selfie", "wf-a"))
        second = RouteDefinition("b", RouteKey("comfyui", "nai", "selfie", "wf-a"))
        third = RouteDefinition("c", RouteKey("comfyui", "anima", "selfie", "wf-b"))
        self.assertNotEqual(route_cache_key(spec, first), route_cache_key(spec, second))
        self.assertNotEqual(route_cache_key(spec, first), route_cache_key(spec, third))
        different_request = replace(spec, user_request="换成礼服")
        self.assertNotEqual(route_cache_key(spec, first), route_cache_key(different_request, first))

    def test_route_cache_tracks_prompt_semantics_but_not_request_id(self):
        spec = _spec()
        route = RouteDefinition("anima", RouteKey("comfyui", "anima", "selfie", "wf"))
        same_semantics = replace(spec, request_id="another-id")
        changed_wardrobe = replace(
            spec,
            wardrobe=replace(spec.wardrobe, required=("light summer dress",)),
        )
        changed_scene = replace(
            spec,
            composition=replace(spec.composition, location="sunlit kitchen"),
        )
        self.assertEqual(route_cache_key(spec, route), route_cache_key(same_semantics, route))
        self.assertNotEqual(route_cache_key(spec, route), route_cache_key(changed_wardrobe, route))
        self.assertNotEqual(route_cache_key(spec, route), route_cache_key(changed_scene, route))

    def test_companion_snapshot_maps_to_versioned_scene(self):
        scene = SceneContextV1.from_companion_snapshot({
            "version": 3,
            "captured_at": "2026-08-15T00:08:00+08:00",
            "time": "00:08",
            "daypart": "深夜",
            "schedule": {"activity": "准备睡觉"},
            "location": {"text": "卧室", "category": "home"},
            "weather": {"temperature_c": 33, "feels_like_c": 36, "text": "晴", "source": "qweather"},
            "sleep": {"phase": "falling_asleep"},
        })
        self.assertEqual("hot", scene.weather.thermal_level)
        self.assertEqual("falling_asleep", scene.sleep_phase)
        self.assertTrue(scene.indoor)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_debug_events_reuse_request_trace_id_when_legacy_state_exists(self):
        routes = RouteRegistry()
        routes.register(RouteDefinition("route", RouteKey("comfyui", "anima", "selfie", "wf")))
        adapter = _Adapter("comfyui")
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = GenerationDebugRecorder(
                temp_dir,
                GenerationDebugConfig(enabled=True, capture_mode="full"),
            )
            recorder.start_trace("engine-trace", request_id="engine-trace")
            recorder.emit("engine-trace", "legacy_context")
            result = await GenerationEngine(
                default_model_profile_registry(),
                routes,
                {"comfyui": adapter},
                debug_recorder=recorder,
            ).generate(_spec(request_id="engine-trace"), "route")

            self.assertTrue(result.ok)
            events = recorder.read_events(trace_id="engine-trace")
            self.assertTrue(events)
            self.assertTrue(all(event["trace_id"] == "engine-trace" for event in events))
            self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
            self.assertTrue(any(event["stage"] == "engine_completed" for event in events))
            self.assertFalse(any(event["stage"] == "completed" for event in events))

    async def test_invalid_context_is_returned_as_classified_failure(self):
        routes = RouteRegistry()
        routes.register(RouteDefinition("route", RouteKey("comfyui", "anima", "selfie", "wf")))
        invalid = replace(_spec(), operation="unsupported")
        result = await GenerationEngine(default_model_profile_registry(), routes, {}).generate(invalid, "route")
        self.assertEqual("context_invalid", result.error_code)
        self.assertEqual("context", result.failure_stage)

    async def test_capability_probe_failure_uses_fallback(self):
        class BrokenAdapter(_Adapter):
            async def capabilities(self, route):
                raise RuntimeError("probe failed")

        routes = RouteRegistry()
        routes.register(RouteDefinition(
            "broken",
            RouteKey("comfyui", "anima", "selfie", "wf"),
            fallback_routes=("legacy",),
        ))
        routes.register(RouteDefinition("legacy", RouteKey("legacy", "legacy", "selfie")))
        adapter = BrokenAdapter("comfyui")
        legacy = _Adapter("legacy")
        result = await GenerationEngine(
            default_model_profile_registry(), routes, {"comfyui": adapter, "legacy": legacy}
        ).generate(_spec(), "broken")
        self.assertTrue(result.ok)
        self.assertEqual("legacy", result.backend)

    async def test_route_concurrency_limit_is_shared_between_engines(self):
        class SlowAdapter(_Adapter):
            active = 0
            peak = 0

            async def generate(self, route, spec, prompt, references, trace):
                type(self).active += 1
                type(self).peak = max(type(self).peak, type(self).active)
                await asyncio.sleep(0.01)
                type(self).active -= 1
                return await super().generate(route, spec, prompt, references, trace)

        routes = RouteRegistry()
        routes.register(RouteDefinition(
            "limited",
            RouteKey("comfyui", "anima", "selfie", "wf"),
            concurrency_limit=1,
        ))
        shared_semaphores = {}
        first = SlowAdapter("comfyui")
        second = SlowAdapter("comfyui")
        engines = [
            GenerationEngine(
                default_model_profile_registry(), routes, {"comfyui": adapter},
                route_semaphores=shared_semaphores,
            )
            for adapter in (first, second)
        ]
        results = await asyncio.gather(
            engines[0].generate(_spec(request_id="one"), "limited"),
            engines[1].generate(_spec(request_id="two"), "limited"),
        )
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(1, SlowAdapter.peak)

    async def test_prompt_cache_can_be_shared_across_request_scoped_engines(self):
        routes = RouteRegistry()
        routes.register(RouteDefinition("route", RouteKey("comfyui", "anima", "selfie", "wf")))
        shared_cache = {}
        first_adapter = _Adapter("comfyui")
        await GenerationEngine(
            default_model_profile_registry(), routes, {"comfyui": first_adapter}, prompt_cache=shared_cache
        ).generate(_spec(request_id="first"), "route")
        self.assertEqual(1, len(shared_cache))
        second_adapter = _Adapter("comfyui")
        result = await GenerationEngine(
            default_model_profile_registry(), routes, {"comfyui": second_adapter}, prompt_cache=shared_cache
        ).generate(_spec(request_id="second"), "route")
        self.assertTrue(result.ok)
        self.assertEqual(1, len(shared_cache))
    async def test_legacy_callback_adapter_keeps_unified_contract(self):
        captured = {}

        async def callback(**kwargs):
            captured.update(kwargs)
            return {"image_path": "/tmp/legacy.png", "note": "legacy-ok"}

        routes = RouteRegistry()
        routes.register(RouteDefinition("legacy", RouteKey("legacy", "legacy", "selfie")))
        engine = GenerationEngine(
            default_model_profile_registry(), routes, {"legacy": LegacyCallbackAdapter(callback)}
        )
        spec = replace(_spec(), legacy_prompt="legacy positive prompt")
        result = await engine.generate(spec, "legacy")
        self.assertTrue(result.ok)
        self.assertEqual("legacy positive prompt", captured["prompt"].positive_prompt)

    async def test_fallback_replans_references_for_target_capacity(self):
        routes = RouteRegistry()
        routes.register(RouteDefinition(
            "small", RouteKey("comfyui", "anima", "selfie"),
            fallback_routes=("large",), allow_paid_fallback=True,
        ))
        routes.register(RouteDefinition("large", RouteKey("external", "generic_natural", "selfie"), paid=True))
        small = _Adapter("comfyui", fail=True, max_refs=1)
        large = _Adapter("external", max_refs=2)
        second = ReferenceBindingV1("friend", "/tmp/friend.png", roles=("outfit",), subject="friend", priority=5)
        spec = replace(_spec(), references=(*_spec().references, second))
        result = await GenerationEngine(
            default_model_profile_registry(), routes, {"comfyui": small, "external": large}
        ).generate(spec, "small")
        self.assertTrue(result.ok)
        self.assertEqual(("identity", "friend"), result.submitted_reference_ids)
    async def test_fallback_recompiles_for_target_profile(self):
        routes = RouteRegistry()
        routes.register(
            RouteDefinition(
                "anima-local",
                RouteKey("comfyui", "anima", "selfie", "anima-selfie"),
                fallback_routes=("natural-online",),
                allow_paid_fallback=True,
            )
        )
        routes.register(
            RouteDefinition(
                "natural-online",
                RouteKey("external", "generic_natural", "selfie"),
                paid=True,
            )
        )
        local = _Adapter("comfyui", fail=True)
        online = _Adapter("external")
        engine = GenerationEngine(
            default_model_profile_registry(), routes, {"comfyui": local, "external": online}
        )
        result = await engine.generate(_spec(), "anima-local")
        self.assertTrue(result.ok)
        self.assertEqual("anima", local.prompts[0].model_profile)
        self.assertEqual("generic_natural", online.prompts[0].model_profile)
        self.assertNotEqual(local.prompts[0].positive_prompt, online.prompts[0].positive_prompt)

    async def test_paid_fallback_is_not_silent(self):
        routes = RouteRegistry()
        routes.register(
            RouteDefinition(
                "local", RouteKey("comfyui", "anima", "selfie"),
                fallback_routes=("paid",), allow_paid_fallback=False,
            )
        )
        routes.register(RouteDefinition("paid", RouteKey("external", "generic_natural", "selfie"), paid=True))
        local = _Adapter("comfyui", fail=True)
        paid = _Adapter("external")
        engine = GenerationEngine(
            default_model_profile_registry(), routes, {"comfyui": local, "external": paid}
        )
        result = await engine.generate(_spec(), "local")
        self.assertFalse(result.ok)
        self.assertEqual([], paid.prompts)
        self.assertTrue(any(event["stage"] == "fallback_skipped" for event in result.trace))

    async def test_shadow_mode_never_calls_backend(self):
        routes = RouteRegistry()
        routes.register(RouteDefinition("route", RouteKey("comfyui", "anima", "selfie")))
        adapter = _Adapter("comfyui")
        engine = GenerationEngine(
            default_model_profile_registry(), routes, {"comfyui": adapter}, shadow_mode=True
        )
        result = await engine.generate(_spec(), "route")
        self.assertFalse(result.ok)
        self.assertIn("shadow_mode", result.note)
        self.assertEqual([], adapter.prompts)

    async def test_output_validation_failure_is_classified(self):
        routes = RouteRegistry()
        routes.register(RouteDefinition("route", RouteKey("comfyui", "anima", "selfie")))
        adapter = _Adapter("comfyui")
        engine = GenerationEngine(
            default_model_profile_registry(),
            routes,
            {"comfyui": adapter},
            output_validator=lambda path: (False, "bad image"),
        )
        result = await engine.generate(_spec(), "route")
        self.assertFalse(result.ok)
        self.assertEqual("output_validation_failed", result.error_code)
        self.assertEqual("output_validation", result.failure_stage)

    async def test_timeout_fault_injection_and_circuit_isolation(self):
        routes = RouteRegistry()
        routes.register(RouteDefinition("bad", RouteKey("comfyui", "anima", "selfie", "bad")))
        routes.register(RouteDefinition("good", RouteKey("comfyui", "anima", "selfie", "good")))
        adapter = _TimeoutAdapter("comfyui")
        engine = GenerationEngine(default_model_profile_registry(), routes, {"comfyui": adapter})
        for _ in range(3):
            result = await engine.generate(_spec(), "bad")
            self.assertEqual("backend_timeout", result.error_code)
        opened = await engine.generate(_spec(), "bad")
        self.assertEqual("route_unavailable", opened.error_code)
        # The breaker key is the route name, so another route remains callable.
        isolated = await engine.generate(_spec(), "good")
        self.assertEqual("backend_timeout", isolated.error_code)


if __name__ == "__main__":
    unittest.main()
