"""
Tests for eva_tui.py
"""

import pytest
from eva_tui import (
    EVATUI,
    is_json,
    rich_markdown,
    rich_json,
    create_agent,
    EVA_MODEL_NAME,
    TOKEN_CAP,
)


class TestHelpers:
    """辅助函数测试"""

    def test_is_json_object(self):
        assert is_json('{"key": "value"}') is True

    def test_is_json_array(self):
        assert is_json('[1, 2, 3]') is True

    def test_is_json_false(self):
        assert is_json("hello world") is False
        assert is_json("no json here") is False

    def test_render_json_formats(self):
        result = rich_json('{"key": "value"}')
        assert '"key"' in result

    def test_render_md_passthrough(self):
        result = rich_markdown("hello world")
        assert result == "hello world"


class TestEVATUIMethods:
    """EVATUI 方法存在性测试"""

    def test_evatui_has_required_methods(self):
        required = [
            "_on_thinking",
            "_on_content",
            "_show_thinking",
            "_finalize_thinking",
            "_process_result",
            "_run_single_step",
            "_process_and_maybe_resume",
            "_resume_next",
            "on_input_submitted",
            "on_mount",
        ]
        for name in required:
            assert hasattr(EVATUI, name), f"missing: {name}"

    def test_evatui_has_bindings(self):
        assert len(EVATUI.BINDINGS) >= 2

    def test_evatui_css_defined(self):
        assert hasattr(EVATUI, "CSS")
        assert len(EVATUI.CSS) > 0


class TestConstants:
    """常量测试"""

    def test_model_name_defined(self):
        assert EVA_MODEL_NAME
        assert isinstance(EVA_MODEL_NAME, str)

    def test_token_cap_positive(self):
        assert TOKEN_CAP > 0
