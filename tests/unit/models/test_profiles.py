from __future__ import annotations

import pytest

from repopilot.models.profiles import (
    ModelProfileError,
    ModelProfileRegistry,
    ModelProvider,
)


def secret_value(prefix: str) -> str:
    return prefix + "-test-value-1234567890"


def test_fake_profile_requires_no_external_configuration() -> None:
    registry = ModelProfileRegistry.from_env({})

    profile = registry.resolve("fake")

    assert profile.provider is ModelProvider.FAKE
    assert profile.model == "scripted"
    assert profile.base_url is None
    assert profile.api_key is None
    assert registry.allowed_names == ("fake", "deepseek", "qwen", "custom")


def test_deepseek_reads_only_fixed_key_and_default_endpoint() -> None:
    key = secret_value("deepseek")
    profile = ModelProfileRegistry.from_env(
        {"DEEPSEEK_MODEL": "deepseek-chat", "DEEPSEEK_API_KEY": key}
    ).resolve("deepseek")

    assert profile.provider is ModelProvider.DEEPSEEK
    assert profile.model == "deepseek-chat"
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.api_key is not None
    assert profile.api_key.get_secret_value() == key


def test_deepseek_does_not_fall_back_to_another_provider_key() -> None:
    registry = ModelProfileRegistry.from_env(
        {
            "DEEPSEEK_MODEL": "deepseek-chat",
            "DASHSCOPE_API_KEY": secret_value("qwen"),
            "MODEL_API_KEY": secret_value("custom"),
        }
    )

    with pytest.raises(ModelProfileError) as caught:
        registry.resolve("deepseek")

    assert caught.value.error_code == "model_key_missing"


def test_qwen_requires_explicit_regional_https_endpoint() -> None:
    key = secret_value("qwen")
    profile = ModelProfileRegistry.from_env(
        {
            "QWEN_MODEL": "qwen-coder-plus",
            "QWEN_BASE_URL": "https://example.aliyuncs.com/compatible-mode/v1",
            "DASHSCOPE_API_KEY": key,
        }
    ).resolve("qwen")

    assert profile.provider is ModelProvider.QWEN
    assert profile.model == "qwen-coder-plus"
    assert profile.base_url == "https://example.aliyuncs.com/compatible-mode/v1"
    assert profile.api_key is not None
    assert profile.api_key.get_secret_value() == key


def test_custom_profile_uses_only_custom_environment_names() -> None:
    key = secret_value("custom")
    profile = ModelProfileRegistry.from_env(
        {
            "MODEL_NAME": "private-coder",
            "MODEL_BASE_URL": "https://models.example.test/v1",
            "MODEL_API_KEY": key,
        }
    ).resolve("custom")

    assert profile.provider is ModelProvider.CUSTOM
    assert profile.model == "private-coder"
    assert profile.base_url == "https://models.example.test/v1"
    assert profile.api_key is not None
    assert profile.api_key.get_secret_value() == key


@pytest.mark.parametrize(
    ("name", "environment", "error_code"),
    [
        ("unknown", {}, "unknown_model_profile"),
        (
            "deepseek",
            {"DEEPSEEK_API_KEY": secret_value("deepseek")},
            "model_name_missing",
        ),
        (
            "qwen",
            {
                "QWEN_MODEL": "qwen-coder-plus",
                "DASHSCOPE_API_KEY": secret_value("qwen"),
            },
            "model_base_url_missing",
        ),
        (
            "custom",
            {
                "MODEL_NAME": "private-coder",
                "MODEL_BASE_URL": "https://models.example.test/v1",
            },
            "model_key_missing",
        ),
    ],
)
def test_profile_configuration_failures_have_stable_codes(
    name: str,
    environment: dict[str, str],
    error_code: str,
) -> None:
    registry = ModelProfileRegistry.from_env(environment)

    with pytest.raises(ModelProfileError) as caught:
        registry.resolve(name)

    assert caught.value.error_code == error_code


@pytest.mark.parametrize(
    "base_url",
    [
        "http://models.example.test/v1",
        "https://user:password@models.example.test/v1",
        "https://models.example.test/v1?tenant=secret",
        "https://models.example.test/v1#fragment",
        "not-a-url",
    ],
)
def test_rejects_unsafe_custom_endpoints(base_url: str) -> None:
    registry = ModelProfileRegistry.from_env(
        {
            "MODEL_NAME": "private-coder",
            "MODEL_BASE_URL": base_url,
            "MODEL_API_KEY": secret_value("custom"),
        }
    )

    with pytest.raises(ModelProfileError) as caught:
        registry.resolve("custom")

    assert caught.value.error_code == "model_base_url_invalid"


def test_key_is_masked_in_repr_and_serialization() -> None:
    key = secret_value("deepseek")
    profile = ModelProfileRegistry.from_env(
        {"DEEPSEEK_MODEL": "deepseek-chat", "DEEPSEEK_API_KEY": key}
    ).resolve("deepseek")

    assert key not in repr(profile)
    assert key not in profile.model_dump_json()
    assert "**********" in repr(profile)
