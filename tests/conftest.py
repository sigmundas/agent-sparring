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


@pytest.fixture(autouse=True)
def _isolated_git_environment(tmp_path, monkeypatch):
    """Every git process a test starts sees only the test's own configuration.

    Git reads user-level configuration from ``$HOME/.gitconfig``,
    ``$XDG_CONFIG_HOME/git/config`` and the default excludes file
    ``$XDG_CONFIG_HOME/git/ignore`` (``$HOME/.config/git/ignore`` when
    ``XDG_CONFIG_HOME`` is unset), plus the system configuration. Any of them
    can change what a test observes: a developer's global ignore of
    ``.DS_Store`` hides exactly the stray file the untracked-candidate tests
    rely on Git reporting. Point all of them at an empty, per-test home.
    """

    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    global_config = home / ".gitconfig"
    global_config.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
    ):
        monkeypatch.delenv(name, raising=False)
