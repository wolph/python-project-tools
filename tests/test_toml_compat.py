from python_project_tools import toml_compat


def test_tomllib_parses() -> None:
    assert toml_compat.tomllib.loads('a = 1') == {'a': 1}
