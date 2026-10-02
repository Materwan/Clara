import json

import pytest
from conftest import FakeBackend, fake_providers

from clara.llm import OllamaBackend
from clara.providers import ProviderError, ProviderManager, configs_from_settings, default_factory
from clara.settings import Settings, SettingsError


def settings_without_key(settings: Settings) -> Settings:
    from dataclasses import replace

    return replace(settings, ollama_api_key=None)


def test_starts_on_the_default_provider(settings):
    providers = fake_providers(settings)
    assert providers.active == "local"
    assert providers.model == "fake"
    assert providers.model_of("cloud") == "fake-big"


@pytest.mark.parametrize("name", ["cloud", "CLOUD", "apikey", "api-key", "ollama-api-key"])
def test_switch_accepts_aliases(settings, name):
    providers = fake_providers(settings)
    assert providers.switch(name).id == "cloud"
    assert providers.active == "cloud"
    assert providers.switch("localhost").id == "local"


def test_unknown_provider_is_refused(settings):
    with pytest.raises(ProviderError):
        fake_providers(settings).switch("openai")


def test_cloud_needs_an_api_key(settings):
    providers = fake_providers(settings_without_key(settings))
    with pytest.raises(ProviderError, match="OLLAMA_API_KEY"):
        providers.switch("cloud")
    assert providers.active == "local"


def test_switching_changes_the_backend_used_for_chat(settings):
    local, cloud = FakeBackend(model="local-fake"), FakeBackend(model="cloud-fake")
    by_host = {configs_from_settings(settings)["local"].host: local}
    providers = ProviderManager.from_settings(
        settings, factory=lambda config, model: by_host.get(config.host, cloud)
    )
    assert providers._backend is local
    providers.switch("cloud")
    assert providers._backend is cloud


def test_choice_and_models_survive_a_restart(settings):
    providers = fake_providers(settings)
    providers.switch("cloud")
    providers.set_model("other-model")

    again = fake_providers(settings)
    assert again.active == "cloud"
    assert again.model == "other-model"
    assert again.model_of("local") == "fake"


def test_saved_state_never_contains_the_api_key(settings):
    fake_providers(settings).switch("cloud")
    saved = settings.runtime_state_file.read_text(encoding="utf-8")
    assert "key-123" not in saved
    assert json.loads(saved)["provider"] == "cloud"


def test_saved_cloud_choice_is_ignored_when_the_key_is_gone(settings):
    fake_providers(settings).switch("cloud")
    assert fake_providers(settings_without_key(settings)).active == "local"


def test_corrupt_state_file_is_ignored(settings):
    settings.data_dir.mkdir(parents=True)
    settings.runtime_state_file.write_text("{not json", encoding="utf-8")
    assert fake_providers(settings).active == "local"


async def test_check_reports_unreachable_providers(settings):
    class Down(FakeBackend):
        async def list_models(self):
            raise ConnectionError("refused")

    providers = fake_providers(settings, Down())
    assert "refused" in await providers.check()
    assert await fake_providers(settings).check() is None


def test_cloud_backend_sends_the_api_key_and_local_does_not(settings):
    configs = configs_from_settings(settings)
    cloud = default_factory(configs["cloud"], "gpt-oss:120b")
    local = default_factory(configs["local"], "llama3")
    assert isinstance(cloud, OllamaBackend)
    assert cloud._client._client.headers["authorization"] == "Bearer key-123"
    assert "authorization" not in local._client._client.headers
    assert str(cloud._client._client.base_url).startswith("https://ollama.com")


def test_secrets_are_not_in_the_settings_repr(settings):
    shown = repr(settings)
    assert "key-123" not in shown and "secret-cli" not in shown and "secret-admin" not in shown


def test_settings_cloud_default_needs_a_key():
    with pytest.raises(SettingsError):
        Settings.from_env({"CLARA_TOKENS": "a:b", "CLARA_PROVIDER": "cloud"})


def test_settings_reject_a_token_used_for_chat_and_admin():
    with pytest.raises(SettingsError):
        Settings.from_env({"CLARA_TOKENS": "a:same", "CLARA_ADMIN_TOKENS": "b:same"})


async def test_api_key_provider_rejects_a_bad_key(settings):
    import httpx

    from clara.llm import OllamaBackend

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/me"
        ok = request.headers["authorization"] == "Bearer good"
        return httpx.Response(200 if ok else 401, json={})

    real_client = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    try:
        class Listing:  # stands in for the ollama client's /api/tags
            async def list(self):
                from types import SimpleNamespace as NS

                return NS(models=[NS(model="gpt-oss:120b")])

        bad = OllamaBackend("m", host="https://ollama.com", api_key="bad", client=Listing())
        good = OllamaBackend("m", host="https://ollama.com", api_key="good", client=Listing())
        with pytest.raises(PermissionError, match="rejected"):
            await bad.verify()
        await good.verify()
    finally:
        httpx.AsyncClient = real_client
