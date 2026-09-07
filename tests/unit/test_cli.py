import json

import pytest

from resultsgen.cli import main


def test_new_config_validate_and_aggregated_config_exit(tmp_path, capsys):
    path = tmp_path / "dev.json"
    assert main(["new-config", "--preset", "dev", str(path)]) == 0
    assert main(["validate", str(path)]) == 0
    assert "Configuration is valid" in capsys.readouterr().out
    data = json.loads(path.read_text())
    data.update(seed=-1, timezone="Not/AZone", typo=True)
    path.write_text(json.dumps(data))
    assert main(["validate", str(path)]) == 2


def test_cli_rejects_invalid_run_limit_and_port():
    for args in (["generate", "unused.json", "--max-runs", "0"], ["serve", "--port", "65536"]):
        with pytest.raises(SystemExit) as exc:
            main(args)
        assert exc.value.code == 2
