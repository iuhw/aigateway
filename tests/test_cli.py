"""CLI entry-point tests: `aigateway check` validation paths and dispatch.

The serve path is exercised end-to-end by every integration test through
create_app; here we cover argument handling and the check subcommand's
exit-code contract (0 = usable, 1 = invalid) without binding a port.
"""

import pytest

from aigateway.__main__ import check_main, main, serve_main
from tests.conftest import base_config, write_config

pytestmark = pytest.mark.unit


def test_check_valid_config_exits_zero(tmp_path, capsys):
    config = base_config()
    config["server"] = {"admin_api_key": "admin-secret"}
    config["shared_state"] = {"backend": "redis", "url": "redis://localhost:6379/0"}
    path = write_config(tmp_path, config)

    assert check_main([path]) == 0
    out = capsys.readouterr().out
    assert "RESULT: OK (config is usable)" in out
    assert "models (2)" in out
    assert "providers (2): p1, p2" in out


def test_check_missing_file_exits_one(tmp_path, capsys):
    assert check_main([str(tmp_path / "nope.yaml")]) == 1
    assert "FAIL: cannot load config" in capsys.readouterr().out


def test_check_warnings_do_not_fail_the_config(tmp_path, capsys):
    """memory shared-state and enabled passthrough are WARNINGs, not errors."""
    config = base_config()
    config["server"] = {"admin_api_key": "admin-secret", "passthrough": {"enabled": True}}
    path = write_config(tmp_path, config)

    assert check_main([path]) == 0
    out = capsys.readouterr().out
    assert "WARNING: server.passthrough.enabled=true" in out
    assert "WARNING: shared_state.backend=memory" in out
    assert "RESULT: OK" in out


def test_check_broken_models_section_exits_one(tmp_path, capsys):
    """A route-level validation error (canary pointing at itself) must fail check."""
    config = base_config()
    config["models"]["m1"]["canary"] = {"target": "m1", "percent": 10}
    path = write_config(tmp_path, config)

    assert check_main([path]) == 1
    out = capsys.readouterr().out
    assert "models:" in out  # the failing section is named
    assert "RESULT: INVALID" in out


def test_main_dispatches_check_subcommand(tmp_path, capsys):
    config = base_config()
    config["server"] = {"admin_api_key": "admin-secret"}
    path = write_config(tmp_path, config)

    with pytest.raises(SystemExit) as excinfo:
        main(["check", path])
    assert excinfo.value.code == 0
    assert "RESULT: OK" in capsys.readouterr().out


def test_serve_main_requires_config_flag():
    with pytest.raises(SystemExit) as excinfo:
        serve_main([])
    assert excinfo.value.code == 2  # argparse usage error
