"""The tool error boundary."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from es_search_mcp.errors import GENERIC_ERROR_MESSAGE, to_tool_error, tool_error_boundary
from es_search_mcp.exceptions import BackendServerError, InvalidInputError


async def test_domain_errors_keep_their_code_and_message():
    with pytest.raises(ToolError) as excinfo:
        async with tool_error_boundary():
            raise InvalidInputError("`query` must not be empty.")
    assert str(excinfo.value) == "[INVALID_INPUT] `query` must not be empty."


async def test_retryable_errors_say_so():
    with pytest.raises(ToolError) as excinfo:
        async with tool_error_boundary():
            raise BackendServerError("The backend API responded with HTTP 500.")
    assert "retried" in str(excinfo.value)


async def test_unexpected_errors_are_masked_by_default():
    with pytest.raises(ToolError) as excinfo:
        async with tool_error_boundary():
            raise RuntimeError("/srv/app/internal.py exploded")
    assert str(excinfo.value) == GENERIC_ERROR_MESSAGE


async def test_masking_can_be_turned_off_for_development():
    with pytest.raises(ToolError) as excinfo:
        async with tool_error_boundary(mask_unexpected=False):
            raise RuntimeError("boom")
    assert str(excinfo.value) == "[INTERNAL_ERROR] boom"


async def test_existing_tool_errors_pass_through_untouched():
    with pytest.raises(ToolError) as excinfo:
        async with tool_error_boundary():
            raise ToolError("already shaped")
    assert str(excinfo.value) == "already shaped"


async def test_successful_bodies_are_not_disturbed():
    async with tool_error_boundary():
        value = 42
    assert value == 42


def test_to_tool_error_is_idempotent():
    original = ToolError("already shaped")
    assert to_tool_error(original) is original
