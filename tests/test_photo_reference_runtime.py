from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types
import unittest


class _Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return self

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False

    def __mro_entries__(self, bases):
        return ()


def _load_runtime_module():
    names = (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.provider",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.agent",
        "astrbot.core.agent.message",
        "astrbot.core.astr_main_agent",
        "astrbot.core.db",
        "astrbot.core.db.po",
        "astrbot.core.db.sqlite_db",
        "astrbot.core.db.star_repo",
        "astrbot.core.message",
        "astrbot.core.message.components",
        "astrbot.core.platform",
        "astrbot.core.platform.astrbot_message",
        "astrbot.core.platform.message_session",
        "astrbot.core.platform.message_type",
        "astrbot.core.platform.platform",
        "astrbot.core.platform.platform_metadata",
        "astrbot.core.provider",
        "astrbot.core.provider.entities",
        "astrbot.core.star",
        "astrbot.core.star.star",
        "astrbot.core.star.star_handler",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
    )
    packages = {
        "astrbot",
        "astrbot.api",
        "astrbot.core",
        "astrbot.core.agent",
        "astrbot.core.db",
        "astrbot.core.message",
        "astrbot.core.platform",
        "astrbot.core.provider",
        "astrbot.core.star",
        "astrbot.core.utils",
    }
    previous = {name: sys.modules.get(name) for name in names}
    created = set()
    for name in names:
        module = types.ModuleType(name)
        if name in packages:
            module.__path__ = []
        module.__getattr__ = lambda name: _Dummy()
        sys.modules[name] = module
        created.add(name)
    event = sys.modules["astrbot.api.event"]
    event.AstrMessageEvent = _Dummy
    event.MessageChain = _Dummy
    event.filter = _Dummy()
    sys.modules["astrbot.api.provider"].ProviderRequest = _Dummy
    star = sys.modules["astrbot.api.star"]
    star.Context = _Dummy
    star.Star = _Dummy
    star.StarTools = _Dummy
    star.register = lambda *args, **kwargs: lambda cls: cls
    sys.modules["astrbot.core.astr_main_agent"].MainAgentBuildConfig = _Dummy
    sys.modules["astrbot.core.astr_main_agent"].build_main_agent = _Dummy
    sys.modules["astrbot.core.agent.message"].AssistantMessageSegment = _Dummy
    sys.modules["astrbot.core.agent.message"].UserMessageSegment = _Dummy
    sys.modules["astrbot.core.db.po"].Conversation = _Dummy
    sys.modules["astrbot.core.db.sqlite_db"].DB = _Dummy
    sys.modules["astrbot.core.db.star_repo"].StarMeta = _Dummy
    components = sys.modules["astrbot.core.message.components"]
    components.At = _Dummy
    components.Image = _Dummy
    components.Plain = _Dummy
    sys.modules["astrbot.core.platform.astrbot_message"].AstrBotMessage = _Dummy
    sys.modules["astrbot.core.platform.astrbot_message"].MessageMember = _Dummy
    sys.modules["astrbot.core.platform.message_session"].MessageSession = _Dummy
    sys.modules["astrbot.core.platform.message_type"].MessageType = _Dummy
    sys.modules["astrbot.core.platform.platform"].PlatformStatus = _Dummy
    sys.modules["astrbot.core.platform.platform_metadata"].PlatformMetadata = _Dummy
    sys.modules["astrbot.core.provider.entities"].LLMResponse = _Dummy
    sys.modules["astrbot.core.star.star"].star_map = {}
    sys.modules["astrbot.core.star.star_handler"].EventType = _Dummy
    sys.modules["astrbot.core.star.star_handler"].star_handlers_registry = []
    sys.modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: (
        pathlib.Path(".")
    )
    package_name = "photo_runtime_test_package"
    package = types.ModuleType(package_name)
    package.__path__ = [str(pathlib.Path(__file__).resolve().parents[1])]
    sys.modules[package_name] = package
    try:
        return importlib.import_module(f"{package_name}.image_runtime")
    finally:
        for name in created:
            if previous[name] is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous[name]


RUNTIME = _load_runtime_module()


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Harness(RUNTIME.ProactiveMessageMixin):
    def __init__(self):
        self.enable_photo_reference_image = True
        self.enable_photo_text_action = True
        self.photo_generation_backend = "auto"
        self._data_lock = _Lock()
        self.calls = []
        self.data = {}

    def _photo_reference_knowledge_asset_candidates(self, **kwargs):
        return []

    def _photo_reference_role_asset_candidates(self, **kwargs):
        return []

    def _photo_generation_scene_presets(self):
        return {}

    def _task_provider(self, *args):
        return ""

    def _append_photo_generation_trace_event(self, *args, **kwargs):
        return None


class PhotoReferenceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_portrait_zero_or_empty_keeps_identity(self):
        runtime = _Harness()
        for reply in ("0", ""):

            async def choose(*args, expected=reply, **kwargs):
                return expected

            runtime._llm_call = choose
            result = await runtime._select_photo_reference_candidate_async(
                "selfie",
                request_text="来张自拍",
                candidate_overrides=(
                    {
                        "id": "daily",
                        "kind": "daily_outfit",
                        "path": "daily.png",
                        "reference_roles": ["identity", "outfit"],
                    },
                    {
                        "id": "persona",
                        "kind": "persona",
                        "path": "persona.png",
                        "reference_roles": ["identity"],
                    },
                ),
                return_selection_result=True,
            )
            self.assertEqual("persona.png", result.selected["path"])
            self.assertIn("identity", result.selected["reference_roles"])

    async def test_portrait_without_identity_returns_unavailable(self):
        runtime = _Harness()
        result = await runtime._select_photo_reference_candidate_async(
            "selfie",
            request_text="来张自拍",
            candidate_overrides=(
                {
                    "id": "scene",
                    "kind": "library",
                    "path": "scene.png",
                    "reference_roles": ["scene"],
                },
            ),
            return_selection_result=True,
        )
        self.assertIsNone(result.selected)
        self.assertEqual("identity_reference_unavailable", result.selection_reason)

    async def test_identity_scene_without_reference_stops_before_bridge(self):
        runtime = _Harness()

        async def build_scene(user, name, reason):
            return {
                "kind": "text2img",
                "prompt": "a casual character photo",
                "caption": "人物生活照",
                "use_persona_reference": True,
                "subject_owner": "bot",
                "scene_context": "",
                "prompt_format": "traditional",
            }

        runtime._build_photo_scene_prompt = build_scene
        runtime._photo_text_load_defer_note = lambda *args, **kwargs: ""
        runtime._photo_text_available = lambda *args, **kwargs: True
        runtime._compose_photo_continuity_key = lambda *args, **kwargs: "continuity"
        runtime._photo_persona_reference_image_for_kind_async = lambda *args, **kwargs: (
            asyncio.sleep(0, result="")
        )

        async def bridge(**kwargs):
            runtime.calls.append(kwargs)
            return "mock", "result.png", "ok"

        runtime._generate_photo_image = bridge
        result = await runtime._run_photo_text_action(
            {"user_id": "u1", "umo": "private:u1"}, "user", "share"
        )
        self.assertIn("身份参考图", result)
        self.assertEqual([], runtime.calls)

    async def test_portrait_kind_requires_identity_even_without_flag(self):
        runtime = _Harness()

        async def build_scene(user, name, reason):
            return {
                "kind": "selfie",
                "prompt": "a casual character photo",
                "caption": "一张人物自拍",
                "use_persona_reference": False,
                "subject_owner": "scene",
                "scene_context": "",
                "prompt_format": "traditional",
            }

        runtime._build_photo_scene_prompt = build_scene
        runtime._photo_text_load_defer_note = lambda *args, **kwargs: ""
        runtime._photo_text_available = lambda *args, **kwargs: True
        runtime._compose_photo_continuity_key = lambda *args, **kwargs: "continuity"
        runtime._photo_persona_reference_image_for_kind_async = lambda *args, **kwargs: (
            asyncio.sleep(0, result="")
        )

        async def bridge(**kwargs):
            runtime.calls.append(kwargs)
            return "mock", "result.png", "ok"

        runtime._generate_photo_image = bridge
        result = await runtime._run_photo_text_action(
            {"user_id": "u1", "umo": "private:u1"}, "user", "share"
        )

        self.assertIn("身份参考图", result)
        self.assertEqual([], runtime.calls)

    async def test_pure_scene_has_no_person_constraint(self):
        runtime = _Harness()

        async def build_scene(user, name, reason):
            return {
                "kind": "text2img",
                "prompt": "a bowl of ramen on a wooden table",
                "caption": "桌上的拉面",
                "use_persona_reference": False,
                "subject_owner": "scene",
                "scene_context": "",
                "prompt_format": "traditional",
            }

        runtime._build_photo_scene_prompt = build_scene
        runtime._photo_text_load_defer_note = lambda *args, **kwargs: ""
        runtime._photo_text_available = lambda *args, **kwargs: True
        runtime._compose_photo_continuity_key = lambda *args, **kwargs: "continuity"
        runtime._note_photo_generation_attempt = lambda *args, **kwargs: None
        runtime._save_data_sync = lambda: None

        async def bridge(**kwargs):
            runtime.calls.append(kwargs)
            return "mock", "result.png", "ok"

        runtime._generate_photo_image = bridge
        await runtime._run_photo_text_action(
            {"user_id": "u1", "umo": "private:u1"}, "user", "share"
        )
        self.assertEqual(1, len(runtime.calls))
        self.assertIn("people", runtime.calls[0]["prompt_text"])
        self.assertIn("human figures", runtime.calls[0]["prompt_text"])


if __name__ == "__main__":
    unittest.main()
