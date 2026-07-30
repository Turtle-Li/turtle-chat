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
builtin_tool_reasons = tool_policy.builtin_tool_reasons
explicit_image_generation_intent = tool_policy.explicit_image_generation_intent


def test_plain_conversation_uses_fast_path() -> None:
    assert not should_enable_builtin_tools(prompt="你好")
    assert not should_enable_builtin_tools(prompt="Explain dynamic programming.")
    assert not should_enable_builtin_tools(
        prompt="分析一下这段代码的时间复杂度",
        features={
            "web_search": False,
            "image_generation": False,
            "code_interpreter": False,
            "memory": True,
            "voice": False,
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


def test_memory_tools_require_explicit_memory_intent() -> None:
    assert not should_enable_builtin_tools(
        prompt="你好",
        features={"memory": True},
    )
    assert should_enable_builtin_tools(
        prompt="请记住我喜欢喝黑咖啡",
        features={"memory": True},
    )
    assert not should_enable_builtin_tools(
        prompt="请记住我喜欢喝黑咖啡",
        features={"memory": False},
    )


def test_direct_image_creation_intent_is_narrow_and_multilingual() -> None:
    assert explicit_image_generation_intent(
        "掐丝竹茶盘，尺寸在图中标注了，帮我生成5张淘宝封面方图，2k图"
    )
    assert explicit_image_generation_intent("请画一张海边日落的插画")
    assert explicit_image_generation_intent("Create three product cover images")

    assert not explicit_image_generation_intent("怎么生成淘宝封面图？")
    assert not explicit_image_generation_intent("请解释图片生成模型的原理")
    assert not explicit_image_generation_intent("不要生成图片，只分析构图")
    assert not explicit_image_generation_intent("分析这张图片里的尺寸")


def test_patcher_wires_policy_before_builtin_resolution() -> None:
    patcher = (OPEN_WEBUI_BRANDING / "patch_open_webui.py").read_text(encoding="utf-8")
    assert "explicit_image_generation_intent," in patcher
    assert "turtle_image_generation_auto_enabled=1" in patcher
    assert "form_data['_turtle_direct_image_generation'] = True" in patcher
    assert "form_data.pop('_turtle_direct_image_generation', False)" in patcher
    assert "turtle_image_generation_direct_completion=1" in patcher
    assert "detail=error_message or '图片生成失败，请稍后重试'" in patcher
    assert "builtin_tool_reasons_for_turn = builtin_tool_reasons(" in patcher
    assert "model_knowledge=get_attached_knowledge(model, metadata)" in patcher
    assert "turtle_builtin_tool_policy enabled=%s reasons=%s" in patcher


def test_reason_labels_do_not_include_user_values() -> None:
    assert builtin_tool_reasons(
        prompt="搜索我的聊天",
        features={"web_search": True},
        files=[{"name": "private-name.txt"}],
        tool_ids=["private-tool-id"],
    ) == ("feature:web_search", "files", "selected_tools", "workspace_intent")
