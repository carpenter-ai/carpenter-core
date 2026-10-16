"""Config keys that were removed must warn, not silently linger."""

import pytest


@pytest.mark.parametrize("key", ["egress_policy", "egress_enforce"])
def test_removed_egress_keys_not_in_defaults(key):
    from carpenter.config import DEFAULTS
    assert key not in DEFAULTS


def test_removed_key_in_yaml_warns(tmp_path, caplog):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        f"base_dir: {tmp_path}\negress_policy: iptables\negress_enforce: true\n"
    )
    from carpenter.config import load_config
    with caplog.at_level("WARNING", logger="carpenter.config"):
        load_config(yaml_path=str(yaml_file))
    assert "'egress_policy'" in caplog.text
    assert "'egress_enforce'" in caplog.text
    assert "web_egress_allowlist" in caplog.text


def test_no_removed_key_warning_by_default(tmp_path, caplog):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(f"base_dir: {tmp_path}\n")
    from carpenter.config import load_config
    with caplog.at_level("WARNING", logger="carpenter.config"):
        load_config(yaml_path=str(yaml_file))
    assert "no longer used" not in caplog.text
