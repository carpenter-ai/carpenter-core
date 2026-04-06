"""Tests for dry-run executor (verify/dry_run.py)."""

import pytest

from carpenter.verify.dry_run import (
    run_dry_run, _enumerate_inputs, _cross_product,
    _extract_tracked_values, _validate_tool_calls, ToolCallRecord,
    clear_tool_policy_cache, _get_tool_policy_map,
)
from carpenter.verify.tracked import Tracked, TrackedList, TrackedDict, TrackedStr, wrap_value, _T, _C


class TestSimpleCode:
    """Code with no constrained inputs."""

    def test_all_trusted_passes(self):
        code = """
from carpenter_tools.act import arc
arc.create(name="test", goal="do thing")
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_simple_assignment(self):
        result = run_dry_run("x = 1\ny = x + 2", [])
        assert result.passed

    def test_syntax_error(self):
        result = run_dry_run("def ", [])
        assert not result.passed
        assert "syntax" in result.reason.lower()


class TestConstrainedBranching:
    """Code that branches on constrained data."""

    def test_bare_constrained_if_fails(self):
        """Branching on bare C value raises ConstrainedControlFlowError."""
        code = """
x = state.get('flag', arc_id=10)
if x:
    y = 1
"""
        inputs = [{"key": "flag", "arc_id": 10, "integrity_level": "constrained"}]
        result = run_dry_run(code, inputs)
        assert not result.passed
        assert "CONSTRAINED" in result.reason or "constrained" in result.reason


class TestThreshold:
    def test_threshold_exceeded_rejects(self):
        inputs = [
            {"key": f"k{i}", "arc_id": i, "integrity_level": "constrained"}
            for i in range(20)
        ]
        # 2^20 > 1024
        result = run_dry_run("x = 1", inputs, threshold=1024)
        assert not result.passed
        assert "threshold" in result.reason.lower()


class TestToolCallRecording:
    def test_mock_tool_calls_recorded(self):
        code = """
from carpenter_tools.act import arc, messaging
arc.create(name="test", goal="do thing")
messaging.send(message="hello")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert len(result.tool_calls) == 2
        modules = {tc.module for tc in result.tool_calls}
        assert "arc" in modules
        assert "messaging" in modules


class TestCrossProduct:
    def test_empty(self):
        assert _cross_product([]) == [()]

    def test_single(self):
        result = _cross_product([[1, 2]])
        assert result == [(1,), (2,)]

    def test_two(self):
        result = _cross_product([[1, 2], [3, 4]])
        assert len(result) == 4
        assert (1, 3) in result
        assert (2, 4) in result


class TestEnumerateInputs:
    def test_basic_enumeration(self):
        inputs = [{"key": "flag", "arc_id": 10, "integrity_level": "constrained"}]
        result = _enumerate_inputs(inputs)
        assert len(result) == 1
        key = "flag:10"
        assert key in result
        assert len(result[key]) == 2  # True, False

    def test_no_inputs(self):
        result = _enumerate_inputs([])
        assert result == {}


class TestImportHandling:
    def test_policy_import_available(self):
        # Email validation will use in-memory policies (dry-run patches the validator).
        # Without the email in the allowlist, PolicyValidationError is raised.
        # Add the email to the allowlist first.
        from carpenter.security.policies import get_policies
        get_policies().add("email", "test@example.com")

        code = """
from carpenter_tools.policy import EmailPolicy
e = EmailPolicy("test@example.com")
x = 1
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_state_module_available(self):
        code = """
x = state.get('key')
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_web_module_available(self):
        code = """
from carpenter_tools.act import web
result = web.get("https://example.com")
"""
        from carpenter.security.policies import get_policies
        get_policies().add("url", "https://example.com")
        result = run_dry_run(code, [])
        assert result.passed

    def test_files_module_available(self):
        code = """
from carpenter_tools.act import files
result = files.read("/tmp/test.txt")
"""
        from carpenter.security.policies import get_policies
        get_policies().add("filepath", "/tmp/test.txt")
        result = run_dry_run(code, [])
        assert result.passed

    def test_scheduling_module_available(self):
        code = """
from carpenter_tools.act import scheduling
scheduling.add_cron("test", "0 * * * *", "test.event")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "scheduling" for tc in result.tool_calls)

    def test_git_module_available(self):
        code = """
from carpenter_tools.act import git
git.create_branch("/workspace", "feature-branch")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "git" for tc in result.tool_calls)

    def test_lm_module_available(self):
        code = """
from carpenter_tools.act import lm
result = lm.call("Hello")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "lm" for tc in result.tool_calls)

    def test_plugin_module_available(self):
        code = """
from carpenter_tools.act import plugin
result = plugin.submit_task("claude-code", "Fix the bug")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "plugin" for tc in result.tool_calls)

    def test_review_module_available(self):
        code = """
from carpenter_tools.act import review
review.submit_verdict(42, "APPROVE", reason="Looks good")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "review" for tc in result.tool_calls)

    def test_read_platform_time_available(self):
        code = """
from carpenter_tools.read import platform_time
t = platform_time.current_time()
"""
        result = run_dry_run(code, [])
        assert result.passed


class TestToolCallPolicyValidation:
    """Item 3: Tool calls validated against security policies."""

    def test_web_get_allowed_url_passes(self):
        """URL in allowlist -> passes."""
        from carpenter.security.policies import get_policies
        get_policies().add("url", "https://api.example.com")

        code = """
from carpenter_tools.act import web
result = web.get("https://api.example.com/data")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].module == "web"

    def test_web_get_denied_url_fails(self):
        """URL not in allowlist -> fails with policy violation."""
        from carpenter.security.policies import get_policies
        get_policies().clear("url")

        code = """
from carpenter_tools.act import web
url_val = state.get('target_url', arc_id=10)
web.get(url_val)
"""
        inputs = [{"key": "target_url", "arc_id": 10, "integrity_level": "constrained"}]
        result = run_dry_run(code, inputs)
        assert not result.passed
        assert "policy violation" in result.reason.lower()

    def test_trusted_args_not_validated(self):
        """Literal string args (trusted) skip policy validation."""
        from carpenter.security.policies import get_policies
        get_policies().clear("url")
        # Even with empty URL allowlist, trusted literal strings are not checked
        code = """
from carpenter_tools.act import web
web.get("https://not-in-allowlist.com")
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_filepath_policy_checked(self):
        """files.write with constrained path is validated."""
        from carpenter.security.policies import get_policies
        get_policies().clear("filepath")
        get_policies().add("filepath", "/safe/dir/")

        code = """
from carpenter_tools.act import files
path = state.get('target_path', arc_id=10)
files.write(path, "content")
"""
        inputs = [{"key": "target_path", "arc_id": 10, "integrity_level": "constrained"}]
        result = run_dry_run(code, inputs)
        # True and False are not valid filepath values -> violation
        assert not result.passed
        assert "policy violation" in result.reason.lower()

    def test_filepath_policy_allowed(self):
        """files.write with constrained path in allowlist passes."""
        from carpenter.security.policies import get_policies
        get_policies().clear("filepath")
        get_policies().add("filepath", "True")
        get_policies().add("filepath", "False")

        code = """
from carpenter_tools.act import files
path = state.get('target_path', arc_id=10)
files.write(path, "content")
"""
        inputs = [{"key": "target_path", "arc_id": 10, "integrity_level": "constrained"}]
        result = run_dry_run(code, inputs)
        assert result.passed

    def test_non_policy_params_ignored(self):
        """messaging.send(message=constrained) has no policy mapping -> fine."""
        code = """
msg = state.get('msg_text', arc_id=10)
messaging.send(message=msg)
"""
        inputs = [{"key": "msg_text", "arc_id": 10, "integrity_level": "constrained"}]
        result = run_dry_run(code, inputs)
        assert result.passed


class TestExtractTrackedValues:
    """Unit tests for _extract_tracked_values."""

    def test_tracked_non_trusted(self):
        val = Tracked("hello", _C)
        result = _extract_tracked_values(val)
        assert len(result) == 1
        assert result[0] == ("hello", _C)

    def test_tracked_trusted_skipped(self):
        val = Tracked("hello", _T)
        result = _extract_tracked_values(val)
        assert result == []

    def test_tracked_list(self):
        items = TrackedList([Tracked("a", _C), Tracked("b", _T)], _C)
        result = _extract_tracked_values(items)
        assert len(result) == 1
        assert result[0][0] == "a"

    def test_plain_value_empty(self):
        result = _extract_tracked_values("just a string")
        assert result == []


class TestSchemaEnumeration:
    """Item 1: Schema-driven input enumeration uses allowlist values."""

    def test_email_type_uses_allowlist(self):
        from carpenter.security.policies import get_policies
        get_policies().clear("email")
        get_policies().add("email", "alice@example.com")
        get_policies().add("email", "bob@example.com")

        inputs = [{"key": "email", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "email"}]
        result = _enumerate_inputs(inputs)
        key = "email:10"
        assert key in result
        # Should have 2 values from allowlist (not True/False)
        assert len(result[key]) == 2
        raw_values = sorted(v.value for v in result[key])
        assert raw_values == ["alice@example.com", "bob@example.com"]

    def test_enum_type_uses_allowlist(self):
        from carpenter.security.policies import get_policies
        get_policies().clear("enum")
        get_policies().add("enum", "red")
        get_policies().add("enum", "green")
        get_policies().add("enum", "blue")

        inputs = [{"key": "color", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "enum"}]
        result = _enumerate_inputs(inputs)
        key = "color:10"
        assert len(result[key]) == 3

    def test_int_range_enumerates_integers(self):
        from carpenter.security.policies import get_policies
        get_policies().clear("int_range")
        get_policies().add("int_range", "1:5")

        inputs = [{"key": "count", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "int_range"}]
        result = _enumerate_inputs(inputs)
        key = "count:10"
        assert len(result[key]) == 5  # 1, 2, 3, 4, 5
        raw_values = [v.value for v in result[key]]
        assert raw_values == [1, 2, 3, 4, 5]

    def test_none_type_falls_back_to_bool(self):
        inputs = [{"key": "flag", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": None}]
        result = _enumerate_inputs(inputs)
        key = "flag:10"
        assert len(result[key]) == 2

    def test_pattern_type_falls_back_to_bool(self):
        inputs = [{"key": "pat", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "pattern"}]
        result = _enumerate_inputs(inputs)
        key = "pat:10"
        assert len(result[key]) == 2

    def test_empty_allowlist_generates_dummy(self):
        from carpenter.security.policies import get_policies
        get_policies().clear("email")

        inputs = [{"key": "email", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "email"}]
        result = _enumerate_inputs(inputs)
        key = "email:10"
        assert len(result[key]) == 1
        assert result[key][0].value == "__no_allowlist_value__"


class TestIteratedEnumeration:
    """Item 2: Iterated inputs generate single-element TrackedLists."""

    def test_iterated_generates_single_element_lists(self):
        from carpenter.security.policies import get_policies
        get_policies().clear("email")
        get_policies().add("email", "alice@example.com")
        get_policies().add("email", "bob@example.com")

        inputs = [{"key": "emails", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "email", "is_iterated": True}]
        result = _enumerate_inputs(inputs)
        key = "emails:10"
        assert len(result[key]) == 2  # 2 emails -> 2 single-element lists
        for val in result[key]:
            assert isinstance(val, TrackedList)
            assert len(val) == 1

    def test_linear_combination_count(self):
        """5 emails iterated -> 5 runs, not 32 (2^5)."""
        from carpenter.security.policies import get_policies
        get_policies().clear("email")
        for i in range(5):
            get_policies().add("email", f"user{i}@example.com")

        inputs = [{"key": "emails", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "email", "is_iterated": True}]
        result = _enumerate_inputs(inputs)
        key = "emails:10"
        assert len(result[key]) == 5  # linear, not exponential

    def test_non_iterated_generates_scalars(self):
        from carpenter.security.policies import get_policies
        get_policies().clear("email")
        get_policies().add("email", "alice@example.com")

        inputs = [{"key": "email", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "email", "is_iterated": False}]
        result = _enumerate_inputs(inputs)
        key = "email:10"
        assert len(result[key]) == 1
        # Should be TrackedStr, not TrackedList
        assert not isinstance(result[key][0], TrackedList)

    def test_end_to_end_iterated_dry_run(self):
        """End-to-end: iterated constrained input processes each item."""
        from carpenter.security.policies import get_policies
        get_policies().clear("email")
        get_policies().add("email", "alice@example.com")
        get_policies().add("email", "bob@example.com")

        code = """
emails = state.get('emails', arc_id=10)
for email in emails:
    messaging.send(message=email)
"""
        inputs = [{"key": "emails", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "email", "is_iterated": True}]
        result = run_dry_run(code, inputs)
        assert result.passed
        # Should record messaging.send calls
        assert any(tc.module == "messaging" for tc in result.tool_calls)


class TestClearToolPolicyCache:
    """Item 2 follow-up: cache invalidation."""

    def test_clear_cache_forces_rebuild(self):
        """Clearing cache should cause _get_tool_policy_map() to rebuild."""
        # Build cache
        m1 = _get_tool_policy_map()
        assert ("web", "get", "url") in m1
        # Clear
        clear_tool_policy_cache()
        # Rebuild — should still work
        m2 = _get_tool_policy_map()
        assert ("web", "get", "url") in m2

    def test_auto_built_map_matches_hardcoded_keys(self):
        """The auto-built map should contain at least the old hardcoded keys."""
        clear_tool_policy_cache()
        m = _get_tool_policy_map()
        for key, expected in [
            (("web", "get", 0), "url"),
            (("web", "get", "url"), "url"),
            (("web", "post", 0), "url"),
            (("web", "post", "url"), "url"),
            (("files", "write", 0), "filepath"),
            (("files", "write", "path"), "filepath"),
            (("files", "read", 0), "filepath"),
            (("files", "read", "path"), "filepath"),
        ]:
            assert m.get(key) == expected, f"Missing or wrong: {key}"


class TestAccumulatorEnumeration:
    """Item 1: Accumulator inputs generate a single multi-element list."""

    def test_accumulator_generates_single_multi_element_list(self):
        from carpenter.security.policies import get_policies
        get_policies().clear("email")
        get_policies().add("email", "a@example.com")
        get_policies().add("email", "b@example.com")
        get_policies().add("email", "c@example.com")

        inputs = [{"key": "emails", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "email", "is_iterated": True,
                    "has_accumulator": True}]
        result = _enumerate_inputs(inputs)
        key = "emails:10"
        # Single multi-element list (not 3 single-element lists)
        assert len(result[key]) == 1
        val = result[key][0]
        assert isinstance(val, TrackedList)
        assert len(val) == 3

    def test_non_accumulator_still_generates_singles(self):
        from carpenter.security.policies import get_policies
        get_policies().clear("email")
        get_policies().add("email", "a@example.com")
        get_policies().add("email", "b@example.com")

        inputs = [{"key": "emails", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "email", "is_iterated": True,
                    "has_accumulator": False}]
        result = _enumerate_inputs(inputs)
        key = "emails:10"
        assert len(result[key]) == 2
        for val in result[key]:
            assert isinstance(val, TrackedList)
            assert len(val) == 1

    def test_end_to_end_accumulator_dry_run(self):
        """Full dry-run with accumulator pattern passes."""
        from carpenter.security.policies import get_policies
        get_policies().clear("email")
        get_policies().add("email", "a@example.com")
        get_policies().add("email", "b@example.com")

        code = """
emails = state.get('emails', arc_id=10)
result = ""
for email in emails:
    result += email
state.set('all_emails', result)
"""
        inputs = [{"key": "emails", "arc_id": 10, "integrity_level": "constrained",
                    "detected_type": "email", "is_iterated": True,
                    "has_accumulator": True}]
        result = run_dry_run(code, inputs)
        assert result.passed
        assert any(tc.module == "state" and tc.function == "set"
                   for tc in result.tool_calls)


class TestDeclarationsImport:
    """Import of carpenter_tools.declarations works in dry-run."""

    def test_declarations_import_available(self):
        code = """
from carpenter_tools.declarations import Label
x = Label("status")
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_declarations_all_types_available(self):
        code = """
from carpenter_tools.declarations import Label, Email, URL, WorkspacePath, SQL, JSON, UnstructuredText
x = Label("key")
e = Email("a@b.com")
u = URL("https://example.com")
w = WorkspacePath("file.txt")
s = SQL("SELECT 1")
j = JSON("{}")
t = UnstructuredText("hello")
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_declarations_via_parent_import(self):
        code = """
from carpenter_tools import declarations
x = declarations.Label("key")
"""
        result = run_dry_run(code, [])
        assert result.passed


class TestMissingActModules:
    """Newly added act submodules must be importable during dry-run."""

    def test_act_config_set_value(self):
        code = """
from carpenter_tools.act import config
config.set_value("memory_recent_hints", 5)
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "config" and tc.function == "set_value"
                   for tc in result.tool_calls)

    def test_act_config_reload(self):
        code = """
from carpenter_tools.act import config
config.reload()
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "config" and tc.function == "reload"
                   for tc in result.tool_calls)

    def test_act_conversation_rename(self):
        code = """
from carpenter_tools.act import conversation
conversation.rename(1, "New Title")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "conversation" and tc.function == "rename"
                   for tc in result.tool_calls)

    def test_act_conversation_archive(self):
        code = """
from carpenter_tools.act import conversation
conversation.archive(1)
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "conversation" and tc.function == "archive"
                   for tc in result.tool_calls)

    def test_act_credentials_request(self):
        code = """
from carpenter_tools.act import credentials
credentials.request("FORGEJO_TOKEN", label="Forge Token")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "credentials" and tc.function == "request"
                   for tc in result.tool_calls)

    def test_act_kb_edit(self):
        code = """
from carpenter_tools.act import kb
kb.edit("scheduling/tools", "Updated content")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "kb" and tc.function == "edit"
                   for tc in result.tool_calls)

    def test_act_kb_add(self):
        code = """
from carpenter_tools.act import kb
kb.add("new/entry", "Content", "Description")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "kb" and tc.function == "add"
                   for tc in result.tool_calls)

    def test_act_platform_request_restart(self):
        code = """
from carpenter_tools.act import platform
platform.request_restart(mode="opportunistic", reason="deploy")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "platform" and tc.function == "request_restart"
                   for tc in result.tool_calls)

    def test_act_webhook_subscribe(self):
        code = """
from carpenter_tools.act import webhook
webhook.subscribe("forgejo", ["pull_request"], "create_arc", {}, "ben-harack", "test")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "webhook" and tc.function == "subscribe"
                   for tc in result.tool_calls)

    def test_act_webhook_delete(self):
        code = """
from carpenter_tools.act import webhook
webhook.delete("hook-123")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "webhook" and tc.function == "delete"
                   for tc in result.tool_calls)


class TestMissingReadModules:
    """Newly added read submodules must be importable during dry-run."""

    def test_read_config_get_value(self):
        code = """
from carpenter_tools.read import config
config.get_value("memory_recent_hints")
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_config_models(self):
        code = """
from carpenter_tools.read import config
config.models()
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_git_get_pr(self):
        code = """
from carpenter_tools.read import git
git.get_pr("ben-harack", "test", 1)
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "git" and tc.function == "get_pr"
                   for tc in result.tool_calls)

    def test_read_git_get_pr_diff(self):
        code = """
from carpenter_tools.read import git
git.get_pr_diff("ben-harack", "test", 1)
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "git" and tc.function == "get_pr_diff"
                   for tc in result.tool_calls)

    def test_read_webhook_list_subscriptions(self):
        code = """
from carpenter_tools.read import webhook
webhook.list_subscriptions("forgejo")
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_messaging_ask(self):
        code = """
from carpenter_tools.read import messaging
messaging.ask("What do you think?")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "messaging" and tc.function == "ask"
                   for tc in result.tool_calls)

    def test_read_plugin_list_plugins(self):
        code = """
from carpenter_tools.read import plugin
plugin.list_plugins()
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_plugin_check_health(self):
        code = """
from carpenter_tools.read import plugin
plugin.check_health("claude-code")
"""
        result = run_dry_run(code, [])
        assert result.passed


class TestMissingMethodsOnExistingMocks:
    """Methods added to existing mock classes."""

    def test_arc_cancel(self):
        code = """
from carpenter_tools.act import arc
arc.cancel(42)
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "arc" and tc.function == "cancel"
                   for tc in result.tool_calls)

    def test_arc_update_status(self):
        code = """
from carpenter_tools.act import arc
arc.update_status(42, "completed")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "arc" and tc.function == "update_status"
                   for tc in result.tool_calls)

    def test_arc_invoke_coding_change(self):
        code = """
from carpenter_tools.act import arc
arc.invoke_coding_change("/src", "Fix the bug")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "arc" and tc.function == "invoke_coding_change"
                   for tc in result.tool_calls)

    def test_arc_request_ai_review(self):
        code = """
from carpenter_tools.act import arc
arc.request_ai_review(42, "sonnet")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "arc" and tc.function == "request_ai_review"
                   for tc in result.tool_calls)

    def test_arc_grant_read_access(self):
        code = """
from carpenter_tools.act import arc
arc.grant_read_access(1, 2, depth="subtree")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "arc" and tc.function == "grant_read_access"
                   for tc in result.tool_calls)

    def test_git_post_pr_review(self):
        code = """
from carpenter_tools.act import git
git.post_pr_review("owner", "repo", 1, "LGTM", event="APPROVED")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "git" and tc.function == "post_pr_review"
                   for tc in result.tool_calls)

    def test_git_create_repo_webhook(self):
        code = """
from carpenter_tools.act import git
git.create_repo_webhook("owner", "repo", "https://hook.example.com")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "git" and tc.function == "create_repo_webhook"
                   for tc in result.tool_calls)

    def test_git_delete_repo_webhook(self):
        code = """
from carpenter_tools.act import git
git.delete_repo_webhook("owner", "repo", 1)
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "git" and tc.function == "delete_repo_webhook"
                   for tc in result.tool_calls)

    def test_plugin_submit_task_async(self):
        code = """
from carpenter_tools.act import plugin
plugin.submit_task_async("claude-code", "Fix the bug")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "plugin" and tc.function == "submit_task_async"
                   for tc in result.tool_calls)

    def test_state_delete(self):
        code = """
from carpenter_tools.act import state
state.delete("old_key")
"""
        result = run_dry_run(code, [])
        assert result.passed
        assert any(tc.module == "state" and tc.function == "delete"
                   for tc in result.tool_calls)

    def test_state_list_keys(self):
        code = """
from carpenter_tools.read import state
keys = state.list_keys()
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_arc_get(self):
        code = """
from carpenter_tools.read import arc
arc.get(42)
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_arc_get_children(self):
        code = """
from carpenter_tools.read import arc
arc.get_children(42)
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_arc_get_history(self):
        code = """
from carpenter_tools.read import arc
arc.get_history(42)
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_arc_get_plan(self):
        code = """
from carpenter_tools.read import arc
arc.get_plan(42)
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_arc_get_children_plan(self):
        code = """
from carpenter_tools.read import arc
arc.get_children_plan(42)
"""
        result = run_dry_run(code, [])
        assert result.passed


class TestSafeOsModule:
    """_SafeOsModule blocks dangerous operations."""

    def test_os_path_join_allowed(self):
        code = """
import os
result = os.path.join("/tmp", "test.txt")
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_os_path_exists_allowed(self):
        code = """
import os
result = os.path.exists("/tmp")
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_os_getcwd_allowed(self):
        code = """
import os
result = os.getcwd()
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_os_sep_available(self):
        code = """
import os
sep = os.sep
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_os_system_blocked(self):
        code = """
import os
os.system("echo hello")
"""
        result = run_dry_run(code, [])
        assert not result.passed
        assert "PermissionError" in result.reason

    def test_os_remove_blocked(self):
        code = """
import os
os.remove("/tmp/test.txt")
"""
        result = run_dry_run(code, [])
        assert not result.passed
        assert "PermissionError" in result.reason

    def test_os_mkdir_blocked(self):
        code = """
import os
os.mkdir("/tmp/new_dir")
"""
        result = run_dry_run(code, [])
        assert not result.passed
        assert "PermissionError" in result.reason

    def test_os_chdir_blocked(self):
        code = """
import os
os.chdir("/tmp")
"""
        result = run_dry_run(code, [])
        assert not result.passed
        assert "PermissionError" in result.reason

    def test_os_rename_blocked(self):
        code = """
import os
os.rename("/tmp/a", "/tmp/b")
"""
        result = run_dry_run(code, [])
        assert not result.passed
        assert "PermissionError" in result.reason

    def test_os_popen_blocked(self):
        code = """
import os
os.popen("echo hello")
"""
        result = run_dry_run(code, [])
        assert not result.passed
        assert "PermissionError" in result.reason


class TestSafeTimeModule:
    """_SafeTimeModule replaces sleep with no-op."""

    def test_time_sleep_noop(self):
        """time.sleep should not block during dry-run."""
        code = """
import time
time.sleep(999)
x = 1
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_time_time_available(self):
        code = """
import time
t = time.time()
"""
        result = run_dry_run(code, [])
        assert result.passed


class TestActReadSplit:
    """Act and read arc mocks expose different method sets."""

    def test_act_arc_has_create_not_get(self):
        """act.arc should have create but not get (that is read-only)."""
        code = """
from carpenter_tools.act import arc
arc.create(name="test", goal="test")
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_arc_has_get_not_create(self):
        """read.arc should have get but not create (that is act-only)."""
        code = """
from carpenter_tools.read import arc
result = arc.get(42)
"""
        result = run_dry_run(code, [])
        assert result.passed

    def test_read_arc_has_no_create(self):
        """read.arc.create should not exist."""
        code = """
from carpenter_tools.read import arc
arc.create(name="test")
"""
        result = run_dry_run(code, [])
        assert not result.passed
        assert "AttributeError" in result.reason
