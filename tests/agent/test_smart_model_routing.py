from agent.routing_policy import (
    ROUTING_POLICY_VERSION,
    get_allowed_route_models,
    load_routing_policy,
    resolve_primary_turn_route,
)


def test_default_matrix_resolves_current_routes():
    policy = load_routing_policy({})

    assert policy.valid
    assert policy.version == ROUTING_POLICY_VERSION
    assert policy.profile("3A", "high-risk").primary.model == "gpt-5.4"
    assert policy.profile("3B", "marathon").primary.model == "glm-5.1"
    assert policy.profile("3B", "marathon").primary.provider == "zai"
    assert policy.profile("3B", "marathon").fallbacks[0].model == "gpt-5.4-mini"
    assert policy.profile("3B", "long-context").primary.model == "xiaomi/mimo-v2-pro"
    assert policy.profile("3B", "long-context").primary.provider == "nous"
    assert policy.profile("3C", "quick-edit").primary.model == "MiniMax-M2.7"
    assert policy.profile("3C", "quick-edit").primary.provider == "minimax"


def test_config_override_validates_and_applies():
    cfg = {
        "routing": {
            "policy_version": "test-policy",
            "routes": {
                "3A": {"high-risk": {"primary": "codex_gpt54", "fallbacks": [], "default_timeout": 111}},
                "3B": {
                    "marathon": {"primary": "hermes_glm_zai", "fallbacks": ["codex_gpt54mini"]},
                    "long-context": {"primary": "hermes_nous_mimo_v2_pro", "fallbacks": ["codex_gpt54mini"]},
                },
                "3C": {"quick-edit": {"primary": "hermes_minimax_m27", "fallbacks": ["codex_gpt54mini"]}},
            },
        }
    }

    policy = load_routing_policy(cfg)

    assert policy.valid
    assert policy.version == "test-policy"
    assert policy.profile("3A", "high-risk").default_timeout == 111


def test_invalid_override_fails_closed_to_defaults():
    cfg = {
        "routing": {
            "routes": {
                "3A": {"high-risk": {"primary": "unknown", "fallbacks": []}},
                "3B": {"marathon": {"primary": "hermes_glm_zai", "fallbacks": []}},
                "3C": {"quick-edit": {"primary": "hermes_minimax_m27", "fallbacks": []}},
            }
        }
    }

    policy = load_routing_policy(cfg)

    assert not policy.valid
    assert policy.profile("3A", "high-risk").primary.model == "gpt-5.4"
    assert any("unknown routed executor" in error for error in policy.errors)


def test_allowed_route_models_come_from_policy():
    allowed = get_allowed_route_models({})

    assert allowed["3A"] == ("Codex CLI (gpt-5.4)",)
    assert "Hermes CLI (glm-5.1 via zai)" in allowed["3B"]
    assert "Codex CLI (gpt-5.4-mini)" in allowed["3C"]


def test_resolve_primary_turn_route_ignores_deprecated_smart_routing():
    primary = {
        "model": "anthropic/claude-sonnet-4",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions",
        "api_key": "sk-primary",
        "credential_pool": object(),
    }

    result = resolve_primary_turn_route(primary)

    assert result["model"] == "anthropic/claude-sonnet-4"
    assert result["runtime"]["provider"] == "openrouter"
    assert result["runtime"]["api_key"] == "sk-primary"
    assert result["runtime"]["credential_pool"] is primary["credential_pool"]
    assert result["label"] is None
