"""Task 2 — Root Config owns Runtime settings; no ``enabled`` switch."""

from miniunicorn.config.runtime import RuntimeConfig, resolve_runtime_mode
from miniunicorn.config.schema import Config


def test_root_config_owns_runtime_settings() -> None:
    cfg = Config.model_validate({"runtime": {"workerCount": 3}})
    assert cfg.runtime.worker_count == 3
    assert not hasattr(cfg.runtime, "enabled")


def test_runtime_mode_precedence() -> None:
    assert (
        resolve_runtime_mode(
            configured="lightweight",
            cli_value="supervised",
            environment="lightweight",
            launcher_default="lightweight",
        )
        == "supervised"
    )
    assert (
        resolve_runtime_mode(
            configured="supervised",
            cli_value=None,
            environment="lightweight",
            launcher_default="lightweight",
        )
        == "lightweight"
    )
    assert (
        resolve_runtime_mode(
            configured="supervised",
            cli_value=None,
            environment=None,
            launcher_default="lightweight",
        )
        == "supervised"
    )
    assert (
        resolve_runtime_mode(
            configured=None,
            cli_value=None,
            environment=None,
            launcher_default="supervised",
        )
        == "supervised"
    )


def test_supervised_defaults_to_three_single_concurrency_workers() -> None:
    cfg = RuntimeConfig()
    assert cfg.worker_count == 3
    assert cfg.worker_concurrency == 1
