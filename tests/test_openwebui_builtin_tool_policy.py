import importlib.util
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPEN_WEBUI_BRANDING = PROJECT_ROOT / "branding" / "open-webui"
TOOL_POLICY_PATH = OPEN_WEBUI_BRANDING / "turtle_chat" / "tool_policy.py"
spec = importlib.util.spec_from_file_location("turtle_tool_policy", TOOL_POLICY_PATH)
assert spec and spec.loader
tool_policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool_policy)

should_enable_builtin_tools = tool_policy.should_enable_builtin_tools


def test_plain_conversation_uses_fast_path() -> None:
    assert not should_enable_builtin_tools(prompt="你好")
    assert not should_enable_builtin_tools(prompt="Explain dynamic programming.")
    assert not should_enable_builtin_tools(
        prompt="分析一下这段代码的时间复杂度",
        features={
            "web_search": False,
            "image_generation": False,
            "code_interpreter": False,
            "memory": False,
        },
        files=[],
        skill_ids=[],
        tool_ids=[],
        tool_servers=[],
        model_knowledge=[],
    )


def test_explicit_feature_or_context_keeps_builtin_tools() -> None:
    cases = (
        {"features": {"web_search": True}},
        {"files": [{"id": "file-1"}]},
        {"skill_ids": ["skill-1"]},
        {"tool_ids": ["tool-1"]},
        {"terminal_id": "terminal-1"},
        {"tool_servers": [{"url": "https://tools.invalid"}]},
        {"model_knowledge": [{"id": "kb-1", "type": "collection"}]},
        {"note_chat": True},
    )

    for context in cases:
        assert should_enable_builtin_tools(prompt="你好", **context)


def test_workspace_intent_without_toggle_keeps_builtin_tools() -> None:
    prompts = (
        "现在几点？",
        "搜索一下我之前的聊天记录",
        "帮我创建一个笔记",
        "提醒我明天下午交报告",
        "What time is it?",
        "Find my previous chats",
        "Create a calendar event",
    )

    for prompt in prompts:
        assert should_enable_builtin_tools(prompt=prompt)


def test_patcher_wires_policy_before_builtin_resolution() -> None:
    patcher = (OPEN_WEBUI_BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    assert "from open_webui.turtle_chat.tool_policy import should_enable_builtin_tools" in patcher
    assert "should_enable_builtin_tools(" in patcher
    assert "model_knowledge=get_attached_knowledge(model, metadata)" in patcher
