from multi_agent_ai.agent_catalog import CATALOG, get_entry


def test_catalog_unique_and_valid():
    keys = [item.key for item in CATALOG]
    assert len(keys) == len(set(keys))
    assert all(item.url.startswith("https://") for item in CATALOG)


def test_supported_adapters():
    assert {item.key for item in CATALOG if item.adapter} == {"codex", "copilot", "claude"}


def test_gemini_is_catalog_only():
    assert get_entry("gemini").adapter is False
