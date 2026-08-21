from __future__ import annotations

import importlib
import pathlib
import sys
import types


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

    def __await__(self):
        async def resolve():
            return self

        return resolve().__await__()


def load_runtime_module():
    external_modules = (
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
    previous = {name: sys.modules.get(name) for name in external_modules}
    created = set()

    def make_module(name: str, *, package: bool = False):
        module = types.ModuleType(name)
        if package:
            module.__path__ = []
        module.__getattr__ = lambda name: _Dummy()
        sys.modules[name] = module
        created.add(name)
        return module

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
    for name in external_modules:
        make_module(name, package=name in packages)

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
    sys.modules["astrbot.core.message"].components = _Dummy()
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
    sys.modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: pathlib.Path(".")

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


RUNTIME = load_runtime_module()
