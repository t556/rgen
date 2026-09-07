import json
import time

import pytest

from resultsgen.config import config_dict, load_config, preset
from resultsgen.web.app import create_app


@pytest.fixture
def web(tmp_path):
    app = create_app(configs_dir=tmp_path / "configs", jobs_dir=tmp_path / "jobs", test_config={"TESTING": True})
    yield app, app.test_client(), tmp_path
    for job in app.extensions["resultsgen_jobs"].jobs.values():
        if job.process.poll() is None:
            job.process.terminate()
            job.process.wait(timeout=5)


def _post_json(client, action, data, name="smoke"):
    return client.post("/", data={"action": action, "name": name, "use_raw_json": "on", "config_json": json.dumps(data)})


def _finish(client, location):
    identifier = location.rsplit("/", 1)[1]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{identifier}")
        assert response.status_code == 200
        status = response.get_json()
        if status["status"] in {"complete", "failed"}:
            assert status["status"] == "complete", status["log"]
            return status
        time.sleep(.03)
    pytest.fail("UI subprocess did not finish within 15 seconds")


def test_preset_edit_save_validate_preview_and_generate_without_terminal(web):
    app, client, path = web
    response = client.get("/?preset=dev")
    assert response.status_code == 200
    assert b'Build a nightly archive' in response.data
    assert b'value="600"' in response.data
    assert b'Generate archive' in response.data

    data = config_dict(preset("dev"))
    data["output_dir"] = str(path / "output")
    # Exercise the same grouped-field editing route as a browser form.
    response = client.post("/", data={
        "action": "save", "name": "smoke", "base_config": json.dumps(data),
        "seed": "7654", "app_0_test_count": "620",
    })
    assert response.status_code == 200
    assert b"Saved smoke.json." in response.data
    saved = load_config(path / "configs" / "smoke.json")
    assert saved.seed == 7654
    assert saved.apps[0].test_count == 620
    response = client.get("/?name=smoke")
    assert response.status_code == 200
    assert b'value="7654"' in response.data
    assert b'value="620"' in response.data

    data = config_dict(saved)
    response = _post_json(client, "validate", data)
    assert response.status_code == 200
    assert b"Configuration is valid." in response.data
    response = _post_json(client, "preview", data)
    assert response.status_code == 302
    status = _finish(client, response.location)
    assert status["report"]["runs"] > 0
    assert status["report"]["statistics"]["total_test_runs"] > 0
    page = client.get(response.location)
    assert b"Preview report" in page.data
    assert b"persistent" in page.data
    assert b"Jobs are forgotten when this server restarts" in page.data

    response = _post_json(client, "generate", data)
    assert response.status_code == 302
    generated = _finish(client, response.location)
    assert (path / "output" / data["archive_name"] / "1" / "junit").is_dir()
    assert (path / "output" / (data["archive_name"] + ".truth") / "truth.parquet").is_file()
    assert generated["report"]["statistics"] == status["report"]["statistics"]
    page = client.get(response.location)
    assert b"Archive generated" in page.data
    assert b"Results root:" in page.data
    assert b"Ground-truth sidecar:" in page.data


def test_generate_jobs_are_exclusive_and_release_when_finished(web, monkeypatch):
    app, client, _ = web

    class Process:
        return_code = None

        def poll(self):
            return self.return_code

        def terminate(self):
            self.return_code = -15

        def wait(self, timeout=None):
            return self.return_code

    launched = []

    def launch(command, **kwargs):
        assert command[1:4] == ["-m", "resultsgen", "generate"]
        assert "--progress-file" in command
        process = Process()
        launched.append(process)
        return process

    monkeypatch.setattr("resultsgen.web.jobs.subprocess.Popen", launch)
    data = config_dict(preset("dev"))
    first = _post_json(client, "generate", data)
    assert first.status_code == 302
    second = _post_json(client, "generate", data)
    assert second.status_code == 409
    assert b"A generate job is already running" in second.data
    assert len(launched) == 1
    launched[0].return_code = 0
    third = _post_json(client, "generate", data)
    assert third.status_code == 302
    assert len(launched) == 2


def test_validation_errors_and_safe_config_names_render_inline(web):
    _, client, path = web
    data = config_dict(preset("dev"))
    data["seed"] = -1
    data["apps"][0]["test_count"] = 0
    data["calendar"]["outage_night_rate"] = 2
    response = _post_json(client, "validate", data)
    assert response.status_code == 400
    assert b"Please fix these settings" in response.data
    assert b"seed" in response.data and b"test_count" in response.data and b"outage_night_rate" in response.data
    response = _post_json(client, "save", config_dict(preset("dev")), name="../escape")
    assert response.status_code == 400
    assert not (path / "escape.json").exists()
    assert client.get("/jobs/unknown").status_code == 404
    assert client.get("/api/jobs/unknown").status_code == 404


def test_grouped_validation_error_can_be_corrected_in_the_returned_form(web):
    _, client, _ = web
    from html import unescape
    import re
    data = config_dict(preset("dev"))
    response = client.post("/", data={"action": "validate", "base_config": json.dumps(data), "seed": "-1"})
    assert response.status_code == 400
    hidden = re.search(r'name="base_config" value="([^"]+)"', response.get_data(as_text=True)).group(1)
    response = client.post("/", data={"action": "validate", "base_config": unescape(hidden), "seed": "23"})
    assert response.status_code == 200
    assert b"Configuration is valid." in response.data
