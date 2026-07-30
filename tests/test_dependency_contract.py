from importlib.util import find_spec
import sys


def test_api_test_dependency_is_installed() -> None:
    assert find_spec("aiohttp") is not None


def test_windows_zoneinfo_dependency_is_installed() -> None:
    if sys.platform == "win32":
        assert find_spec("tzdata") is not None
