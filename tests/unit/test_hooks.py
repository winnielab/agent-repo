import asyncio

from repopilot.core.hooks import HookEvent, HookManager


def test_hooks_run_in_registration_order() -> None:
    calls = []
    hooks = HookManager()
    hooks.register(HookEvent.PRE_TOOL_USE, lambda payload: calls.append(("a", payload["name"])))

    async def second(payload):
        calls.append(("b", payload["name"]))

    hooks.register(HookEvent.PRE_TOOL_USE, second)
    asyncio.run(hooks.run(HookEvent.PRE_TOOL_USE, name="read_file"))
    assert calls == [("a", "read_file"), ("b", "read_file")]
