"""Application management works through the forms a browser actually submits."""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

import pytest

from resultsgen.config import load_config
from resultsgen.web.app import create_app


class EditorForm(HTMLParser):
    """Collect successful controls without submitting unclicked buttons."""

    def __init__(self, response):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.actions = set()
        self.in_editor = False
        self.textarea = None
        self.select = None
        self.option = None
        self.feed(response.get_data(as_text=True))
        assert "base_config" in self.fields

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.in_editor = attrs.get("id") == "config-editor"
        if not self.in_editor or "disabled" in attrs:
            return
        name = attrs.get("name")
        if tag == "input" and name:
            kind = attrs.get("type", "text")
            if kind in {"button", "submit", "reset", "file"}:
                return
            if kind in {"checkbox", "radio"} and "checked" not in attrs:
                return
            self.fields[name] = attrs.get("value", "on" if kind in {"checkbox", "radio"} else "")
        elif tag == "button" and name == "action":
            self.actions.add(attrs.get("value", ""))
        elif tag == "textarea" and name:
            self.textarea = name
            self.fields[name] = ""
        elif tag == "select" and name:
            self.select = name
        elif tag == "option" and self.select:
            self.option = {"text": "", "value": attrs.get("value"), "selected": "selected" in attrs}

    def handle_data(self, data):
        if self.textarea:
            self.fields[self.textarea] += data
        if self.option is not None:
            self.option["text"] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_editor = False
        elif tag == "textarea":
            self.textarea = None
        elif tag == "option" and self.option is not None:
            if self.select not in self.fields or self.option["selected"]:
                self.fields[self.select] = self.option["value"] if self.option["value"] is not None else self.option["text"]
            self.option = None
        elif tag == "select":
            self.select = None

    @property
    def app_names(self):
        indexes = sorted(int(match[1]) for key in self.fields if (match := re.fullmatch(r"app_(\d+)_name", key)))
        assert indexes == list(range(len(indexes)))
        return [self.fields[f"app_{index}_name"] for index in indexes]

    def post(self, client, action, **changes):
        return client.post("/", data={**self.fields, **changes, "action": action})


@pytest.fixture
def web(tmp_path):
    app = create_app(configs_dir=tmp_path / "configs", jobs_dir=tmp_path / "jobs", test_config={"TESTING": True})
    yield app.test_client(), tmp_path
    for job in app.extensions["resultsgen_jobs"].jobs.values():
        if job.process.poll() is None:
            job.process.terminate()
            job.process.wait(timeout=5)


def finish_job(client, location):
    identifier = location.rsplit("/", 1)[1]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{identifier}")
        assert response.status_code == 200
        state = response.get_json()
        if state["status"] in {"complete", "failed"}:
            assert state["status"] == "complete", state["log"]
            return state["report"]
        time.sleep(.03)
    pytest.fail("Application-management generation did not finish within 15 seconds")


def test_add_more_than_ten_apps_save_reload_and_generate_one_xml_per_app(web):
    client, path = web
    response = client.get("/")
    assert response.status_code == 200
    form = EditorForm(response)
    assert form.app_names == ["atlas", "beacon"]
    assert "add_app" in form.actions
    default = client.get("/?preset=default")
    assert default.status_code == 200
    assert len(EditorForm(default).app_names) == 10
    assert b"10 applications" in default.data

    form.fields.update(name="eleven", output_dir=str(path / "output"), start_date="2026-09-07",
                       end_date="2026-09-07", events_json="[]", outage_night_rate="0", partial_rate="0",
                       app_0_name="north", app_0_test_count="24", app_0_suite_count="2")
    for count in range(3, 12):
        response = form.post(client, "add_app")
        assert response.status_code == 200, response.get_data(as_text=True)
        form = EditorForm(response)
        assert len(form.app_names) == count
        assert len(set(form.app_names)) == count
        assert form.app_names[0] == "north"
        assert form.fields["app_0_test_count"] == "24"
        assert form.fields["start_date"] == form.fields["end_date"] == "2026-09-07"
        assert json.loads(form.fields["events_json"]) == []
        assert not (path / "configs" / "eleven.json").exists()

    expected_names = form.app_names
    for index in range(1, 11):
        form.fields[f"app_{index}_test_count"] = str(20 + index)
        form.fields[f"app_{index}_suite_count"] = "1"
    response = form.post(client, "save")
    assert response.status_code == 200
    saved = load_config(path / "configs" / "eleven.json")
    assert [app.name for app in saved.apps] == expected_names
    assert [app.test_count for app in saved.apps] == [24, *range(21, 31)]
    reloaded = EditorForm(client.get("/?name=eleven"))
    assert reloaded.app_names == expected_names
    response = reloaded.post(client, "validate")
    assert response.status_code == 200
    assert b"Configuration is valid." in response.data
    response = EditorForm(response).post(client, "generate")
    assert response.status_code == 302
    report = finish_job(client, response.location)
    assert report["runs"] == 1
    junit = Path(report["results_root"]) / "1" / "junit"
    assert {xml.name for xml in junit.iterdir()} == {f"{name}.xml" for name in expected_names}
    for app in saved.apps:
        assert len(ET.parse(junit / f"{app.name}.xml").findall(".//testcase")) == app.test_count


def test_remove_reindexes_edited_rows_and_cleans_events_without_resurrection(web):
    client, path = web
    form = EditorForm(client.get("/"))
    response = form.post(client, "add_app", name="removed")
    assert response.status_code == 200
    form = EditorForm(response)
    assert "remove_app:0" in form.actions
    response = form.post(client, "remove_app:0", app_1_test_count="123", app_2_name="cedar", app_2_test_count="17")
    assert response.status_code == 200, response.get_data(as_text=True)
    form = EditorForm(response)
    assert form.app_names == ["beacon", "cedar"]
    assert form.fields["app_0_test_count"] == "123"
    assert form.fields["app_1_test_count"] == "17"
    events = json.loads(form.fields["events_json"])
    assert any(event["type"] == "outage" for event in events)
    assert any(event.get("app") == "beacon" for event in events)
    assert not any(event.get("app") == "atlas" for event in events)
    for field in ("base_config", "config_json"):
        config = json.loads(form.fields[field])
        assert [app["name"] for app in config["apps"]] == ["beacon", "cedar"]
        assert config["events"] == events
    assert not (path / "configs" / "removed.json").exists()
    response = form.post(client, "save")
    assert response.status_code == 200
    saved = load_config(path / "configs" / "removed.json")
    assert [app.name for app in saved.apps] == ["beacon", "cedar"]
    assert [app.test_count for app in saved.apps] == [123, 17]
    assert saved.events == events
    assert EditorForm(client.get("/?name=removed")).app_names == ["beacon", "cedar"]


def test_last_application_cannot_be_removed(web):
    client, _ = web
    response = EditorForm(client.get("/")).post(client, "remove_app:0")
    assert response.status_code == 200
    form = EditorForm(response)
    assert form.app_names == ["beacon"]
    response = form.post(client, "remove_app:0")
    assert response.status_code == 400
    assert b"Please fix these settings" in response.data
    assert EditorForm(response).app_names == ["beacon"]
    response = EditorForm(response).post(client, "validate")
    assert response.status_code == 200


def test_added_application_survives_correction_of_an_invalid_draft(web):
    client, path = web
    form = EditorForm(client.get("/"))
    response = form.post(client, "add_app", seed="-1", name="corrected")
    assert response.status_code == 400
    form = EditorForm(response)
    assert len(form.app_names) == 3
    assert form.fields["seed"] == "-1"
    response = form.post(client, "save", seed="23")
    assert response.status_code == 200, response.get_data(as_text=True)
    saved = load_config(path / "configs" / "corrected.json")
    assert saved.seed == 23
    assert [app.name for app in saved.apps] == form.app_names


def randomize_markup(response):
    """The column menus and their controls, as a browser would find them."""
    html = response.get_data(as_text=True)
    return {
        "columns": re.findall(r'<div class="col-panel" id="range-([a-z_]+)"', html),
        "toggles": re.findall(r'<button type="button" class="col-toggle" data-column="([a-z_]+)"', html),
        "rows": html.count("data-randomize-row"),
        "panels_hidden": len(re.findall(r'<div class="col-panel"[^>]*\shidden>', html)),
        "named_range_inputs": re.findall(r'<input[^>]*data-range[^>]*name=', html),
        "all_button": 'id="randomize-all"' in html,
    }


def test_randomize_controls_are_present_but_never_submitted(web):
    client, path = web
    response = client.get("/?preset=default")
    markup = randomize_markup(response)
    columns = ["test_count", "suite_count", "personality", "removals", "additions",
               "duration_lo", "duration_hi", "partial_rate"]
    assert markup["columns"] == columns
    assert markup["toggles"] == columns
    assert markup["rows"] == 10
    assert markup["all_button"]
    # Without the script the menus stay closed and the plain table still works.
    assert markup["panels_hidden"] == len(columns)
    assert client.get("/static/apps.js").status_code == 200

    # The range controls carry no name, so a submitted form is byte-for-byte the
    # form without them: nothing about randomizing reaches the configuration.
    assert markup["named_range_inputs"] == []
    form = EditorForm(response)
    assert not any(key.startswith("range") or "_min" in key or "_max" in key for key in form.fields)
    saved = form.post(client, "save", name="plain")
    assert saved.status_code == 200
    assert load_config(path / "configs" / "plain.json").apps[0].name == "atlas"


def test_randomized_row_values_round_trip_through_save(web):
    """Values a browser can produce with the column ranges stay valid on save."""
    client, path = web
    form = EditorForm(client.get("/"))
    response = form.post(client, "save", name="randomized", app_0_test_count="4821", app_0_suite_count="37",
                         app_0_personality="noisy", app_0_removals="3", app_0_additions="2",
                         app_0_duration_lo="12.4", app_0_duration_hi="88.9", app_0_partial_rate="0.0073",
                         events_json="[]")
    assert response.status_code == 200, response.get_data(as_text=True)
    app = load_config(path / "configs" / "randomized.json").apps[0]
    assert (app.test_count, app.suite_count, app.personality) == (4821, 37, "noisy")
    assert (app.drift.removals, app.drift.additions) == (3, 2)
    assert app.duration_minutes == [12.4, 88.9] and app.partial_rate == 0.0073


def test_added_application_gets_its_own_randomize_button(web):
    client, _ = web
    response = EditorForm(client.get("/")).post(client, "add_app")
    assert response.status_code == 200
    assert randomize_markup(response)["rows"] == 3
