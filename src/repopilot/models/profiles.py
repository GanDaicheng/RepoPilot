"""Allowlisted model profiles whose credentials come only from the environment."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr


class ModelProvider(StrEnum):
    FAKE = "fake"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    CUSTOM = "custom"


class ModelProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    provider: ModelProvider
    model: str
    base_url: str | None


class ResolvedModelProfile(ModelProfile):
    api_key: SecretStr | None


class ModelProfileError(ValueError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class ModelProfileRegistry:
    allowed_names = ("fake", "deepseek", "qwen", "custom")

    def __init__(self, environ: Mapping[str, str]):
        self._environ = MappingProxyType(dict(environ))

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> ModelProfileRegistry:
        return cls(environ)

    def __repr__(self) -> str:
        return f"ModelProfileRegistry(allowed_names={self.allowed_names!r})"

    def resolve(self, name: str) -> ResolvedModelProfile:
        if name not in self.allowed_names:
            raise ModelProfileError(
                "unknown_model_profile",
                "The requested model profile is not allowlisted.",
            )
        provider = ModelProvider(name)
        if provider is ModelProvider.FAKE:
            return ResolvedModelProfile(
                name=name,
                provider=provider,
                model=self._value("FAKE_MODEL") or "scripted",
                base_url=None,
                api_key=None,
            )

        model_env, key_env, base_env = {
            ModelProvider.DEEPSEEK: (
                "DEEPSEEK_MODEL",
                "DEEPSEEK_API_KEY",
                "DEEPSEEK_BASE_URL",
            ),
            ModelProvider.QWEN: ("QWEN_MODEL", "DASHSCOPE_API_KEY", "QWEN_BASE_URL"),
            ModelProvider.CUSTOM: ("MODEL_NAME", "MODEL_API_KEY", "MODEL_BASE_URL"),
        }[provider]
        model = self._value(model_env)
        if model is None:
            raise ModelProfileError(
                "model_name_missing",
                "The model profile has no configured model name.",
            )
        base_url = self._value(base_env)
        if provider is ModelProvider.DEEPSEEK and base_url is None:
            base_url = "https://api.deepseek.com"
        if base_url is None:
            raise ModelProfileError(
                "model_base_url_missing",
                "The model profile has no configured base URL.",
            )
        if not _is_safe_https_url(base_url):
            raise ModelProfileError(
                "model_base_url_invalid",
                "The model base URL must be an HTTPS endpoint without credentials, query, or fragment.",
            )
        raw_key = self._environ.get(key_env)
        if raw_key is None or not raw_key.strip():
            raise ModelProfileError(
                "model_key_missing",
                "The model profile credential is not configured.",
            )
        return ResolvedModelProfile(
            name=name,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=SecretStr(raw_key),
        )

    def _value(self, name: str) -> str | None:
        value = self._environ.get(name)
        if value is None or not value.strip():
            return None
        return value.strip()


def _is_safe_https_url(value: str) -> bool:
    if any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )
