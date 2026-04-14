"""Test that opencode-go appears in /model list when credentials are set."""

import json
from types import SimpleNamespace

import pytest
from hermes_cli.model_switch import list_authenticated_providers


@pytest.fixture()
def isolated_auth_homes(tmp_path, monkeypatch):
    """Run model-list tests against empty auth stores.

    The /model picker also checks Hermes and Codex auth state. These tests
    only care about the explicit OPENCODE_GO_API_KEY env-var path, so isolate
    them from any real machine credentials and shared provider metadata.
    """
    import agent.models_dev as models_dev
    from hermes_cli.auth import PROVIDER_REGISTRY, ProviderConfig

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()

    (hermes_home / "auth.json").write_text(json.dumps({"version": 2, "providers": {}}))
    (codex_home / "auth.json").write_text(json.dumps({}))

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    monkeypatch.setitem(models_dev.PROVIDER_TO_MODELS_DEV, "opencode-go", "opencode-go")
    monkeypatch.setattr(
        models_dev,
        "fetch_models_dev",
        lambda: {
            "openrouter": {"env": ["OPENROUTER_API_KEY"]},
            "opencode-go": {"env": ["OPENCODE_GO_API_KEY"]},
        },
    )
    monkeypatch.setattr(
        models_dev,
        "get_provider_info",
        lambda provider_id: (
            SimpleNamespace(name="OpenCode Go")
            if provider_id == "opencode-go"
            else SimpleNamespace(name="OpenRouter")
            if provider_id == "openrouter"
            else None
        ),
    )
    monkeypatch.setitem(
        PROVIDER_REGISTRY,
        "opencode-go",
        ProviderConfig(
            id="opencode-go",
            name="OpenCode Go",
            auth_type="api_key",
            inference_base_url="https://opencode.ai/zen/go/v1",
            api_key_env_vars=("OPENCODE_GO_API_KEY",),
            base_url_env_var="OPENCODE_GO_BASE_URL",
        ),
    )


def test_opencode_go_appears_when_api_key_set(monkeypatch, isolated_auth_homes):
    """opencode-go should appear in list_authenticated_providers when OPENCODE_GO_API_KEY is set."""
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "test-key")

    providers = list_authenticated_providers(current_provider="openrouter")

    # Find opencode-go in results
    opencode_go = next((p for p in providers if p["slug"] == "opencode-go"), None)

    assert opencode_go is not None, "opencode-go should appear when OPENCODE_GO_API_KEY is set"
    assert opencode_go["models"] == ["glm-5", "kimi-k2.5", "mimo-v2-pro", "mimo-v2-omni", "minimax-m2.7", "minimax-m2.5"]
    # opencode-go is in PROVIDER_TO_MODELS_DEV, so it appears as "built-in" (Part 1)
    assert opencode_go["source"] == "built-in"


def test_opencode_go_not_appears_when_no_creds(monkeypatch, isolated_auth_homes):
    """opencode-go should NOT appear when no credentials are set."""
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)

    providers = list_authenticated_providers(current_provider="openrouter")

    # opencode-go should not be in results
    opencode_go = next((p for p in providers if p["slug"] == "opencode-go"), None)
    assert opencode_go is None, "opencode-go should not appear without credentials"
