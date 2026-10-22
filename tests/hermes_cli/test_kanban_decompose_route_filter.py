"""Route-health filter for the Kanban auto-decomposer roster (regression for model-named profiles on exhausted routes)."""
from types import SimpleNamespace

from hermes_cli import kanban_decompose as kd

AUTH_TEXT = """anthropic (2 credentials):
  #1  anthropic-oauth-2    oauth   id=6c89e7 priority=0 hermes_pkce \u2190
  #2  claude_code          oauth   id=472023 priority=1 claude_code

openai-codex (1 credentials):
  #1  device_code          oauth   id=c27af8 priority=0 device_code rate-limited (429) (24d 5h left)

opencode-go (1 credentials):
  #1  OPENCODE_GO_API_KEY  api_key id=4188aa priority=0 env:OPENCODE_GO_API_KEY auth failed api_key_expired (401) \u2190
"""


def _profile(name, provider):
    return SimpleNamespace(name=name, provider=provider, description="")


def test_usable_providers_exclude_rate_limited_and_expired_routes():
    assert kd._usable_providers_from_auth_text(AUTH_TEXT) == {"anthropic"}


def test_exhausted_profiles_are_dropped_while_default_and_unresolved_are_kept():
    profiles = [
        _profile("default", "anthropic"),
        _profile("gpt54", "openai-codex"),
        _profile("glm52", "opencode-go"),
        _profile("mystery", None),
    ]
    kept = [p.name for p in kd._profiles_with_usable_routes(profiles, {"anthropic"})]
    assert kept == ["default", "mystery"]


def test_unreadable_auth_leaves_roster_unchanged():
    profiles = [_profile("gpt54", "openai-codex")]
    assert [p.name for p in kd._profiles_with_usable_routes(profiles, None)] == ["gpt54"]


def test_default_profile_is_never_dropped():
    profiles = [_profile("default", "openai-codex")]
    assert [p.name for p in kd._profiles_with_usable_routes(profiles, set())] == ["default"]
