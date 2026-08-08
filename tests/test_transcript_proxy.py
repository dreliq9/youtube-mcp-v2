from __future__ import annotations

import pytest

from youtube_mcp_v2.adapters import transcript_api


PROXY_ENV = [
    "YOUTUBE_TRANSCRIPT_PROXY",
    "YOUTUBE_TRANSCRIPT_HTTP_PROXY",
    "YOUTUBE_TRANSCRIPT_HTTPS_PROXY",
    "YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME",
    "YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD",
    "YOUTUBE_TRANSCRIPT_WEBSHARE_LOCATIONS",
]


def _clear(monkeypatch) -> None:
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)


def test_direct_mode_when_no_proxy_env(monkeypatch) -> None:
    _clear(monkeypatch)
    settings = transcript_api.proxy_settings_from_env()
    assert settings.mode == "direct"
    assert settings.config is None
    assert settings.summary == {"mode": "direct"}


def test_shared_generic_proxy_configures_both_schemes_without_leaking_url(
    monkeypatch,
) -> None:
    _clear(monkeypatch)
    secret_url = "socks5://user:super-secret@proxy.example:1080"
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_PROXY", secret_url)
    monkeypatch.setattr(
        transcript_api,
        "GenericProxyConfig",
        lambda **kwargs: {"kind": "generic", **kwargs},
    )

    settings = transcript_api.proxy_settings_from_env()
    assert settings.mode == "generic"
    assert settings.config["http_url"] == secret_url
    assert settings.config["https_url"] == secret_url
    assert settings.summary == {
        "mode": "generic",
        "http_configured": True,
        "https_configured": True,
    }
    assert "super-secret" not in repr(settings.summary)
    assert secret_url not in repr(settings.summary)


def test_webshare_proxy_preserves_locations_but_not_credentials(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME", "secret-user")
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD", "secret-password")
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_WEBSHARE_LOCATIONS", "US, de")
    monkeypatch.setattr(
        transcript_api,
        "WebshareProxyConfig",
        lambda **kwargs: {"kind": "webshare", **kwargs},
    )

    settings = transcript_api.proxy_settings_from_env()
    assert settings.mode == "webshare"
    assert settings.config["proxy_username"] == "secret-user"
    assert settings.config["proxy_password"] == "secret-password"
    assert settings.config["filter_ip_locations"] == ["us", "de"]
    assert settings.summary == {"mode": "webshare", "locations": ["us", "de"]}
    serialized_summary = repr(settings.summary)
    assert "secret-user" not in serialized_summary
    assert "secret-password" not in serialized_summary


def test_incomplete_webshare_config_fails_without_echoing_secret(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME", "do-not-echo-me")

    with pytest.raises(ValueError) as exc_info:
        transcript_api.proxy_settings_from_env()

    assert "both username and password" in str(exc_info.value)
    assert "do-not-echo-me" not in str(exc_info.value)


def test_generic_and_webshare_modes_are_mutually_exclusive(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_PROXY", "http://generic-secret@proxy")
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME", "web-user")
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD", "web-password")

    with pytest.raises(ValueError, match="either generic transcript proxy"):
        transcript_api.proxy_settings_from_env()


def test_build_client_passes_proxy_config_only_when_configured(monkeypatch) -> None:
    _clear(monkeypatch)
    calls: list[dict] = []

    class FakeApi:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(transcript_api, "YouTubeTranscriptApi", FakeApi)
    transcript_api._build_client()
    assert calls == [{}]

    calls.clear()
    monkeypatch.setenv("YOUTUBE_TRANSCRIPT_PROXY", "http://proxy")
    monkeypatch.setattr(
        transcript_api,
        "GenericProxyConfig",
        lambda **kwargs: {"proxy": kwargs},
    )
    transcript_api._build_client()
    assert calls == [
        {"proxy_config": {"proxy": {"http_url": "http://proxy", "https_url": "http://proxy"}}}
    ]
