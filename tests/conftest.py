"""Test-wide isolation from the developer's own machine."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_preferences(tmp_path, monkeypatch):
    """Every test gets its own, initially absent, user preference file.

    Model and effort resolve from ``~/.config/agent-sparring/config.toml`` in
    real use; a test must never read the developer's preferences there, nor
    leave its own behind for the next test.
    """

    monkeypatch.setenv("SPARRING_USER_CONFIG", str(tmp_path / "user-config" / "config.toml"))
    for name in ("XDG_CONFIG_HOME",):
        monkeypatch.delenv(name, raising=False)
