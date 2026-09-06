from erza.config.schema import DreamConfig


def test_dream_config_defaults_to_nightly_cron() -> None:
    cfg = DreamConfig()

    assert cfg.cron == "0 3 * * *"
    assert cfg.enabled is True
    assert not hasattr(cfg, "max_iterations")
    assert not hasattr(cfg, "annotate_line_ages")


def test_dream_config_builds_cron_schedule() -> None:
    cfg = DreamConfig(cron="0 */4 * * *")

    schedule = cfg.build_schedule("UTC")

    assert schedule.kind == "cron"
    assert schedule.expr == "0 */4 * * *"
    assert schedule.tz == "UTC"
    assert schedule.every_ms is None


def test_dream_config_describe_schedule_returns_cron_expr() -> None:
    cfg = DreamConfig(cron="30 2 * * *")

    assert cfg.describe_schedule() == "cron 30 2 * * *"


def test_dream_config_dump_preserves_cron_field() -> None:
    cfg = DreamConfig.model_validate({"cron": "0 */4 * * *"})

    dumped = cfg.model_dump(by_alias=True)

    assert dumped["cron"] == "0 */4 * * *"


def test_dream_config_uses_model_override_name_and_accepts_legacy_model() -> None:
    cfg = DreamConfig.model_validate({"model": "openrouter/sonnet"})

    dumped = cfg.model_dump(by_alias=True)

    assert cfg.model_override == "openrouter/sonnet"
    assert dumped["modelOverride"] == "openrouter/sonnet"
    assert "model" not in dumped
