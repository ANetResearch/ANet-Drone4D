import json

import pytest
from lifelib import ring_population

from awr.sim.activities.designer import apply_design, design


def recipe():
    doc = ring_population(2)
    for a in doc["actors"]:
        a.update(**{"class": "person", "role": "resident"})
    for f in doc["facilities"].values():
        f["kind"] = "shop"
    return doc


def draft():
    return {
        "summary": "A resident shops then returns.",
        "assignments": [
            {
                "actor_id": "a000",
                "visits": [
                    {"facility_id": "a000-1", "purpose": "buy groceries", "dwell_s": 30},
                    {"facility_id": "a000-3", "purpose": "return home", "dwell_s": 45},
                ],
            }
        ],
    }


def test_design_changes_intents_only_and_retains_all_other_actors():
    doc = recipe()
    out = apply_design(doc, draft())
    assert out["actors"][1] == doc["actors"][1]
    assert out["nodes"] == doc["nodes"] and out["edges"] == doc["edges"]
    assert out["actors"][0]["id"] == doc["actors"][0]["id"]
    assert out["actors"][0]["itinerary"][0]["dwell_s"] == 30


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(script="arbitrary code"),
        lambda d: d["assignments"][0].update(pos=[100, 200, 0]),
        lambda d: d["assignments"][0].update(actor_id="missing"),
        lambda d: d["assignments"][0]["visits"][0].update(facility_id="missing"),
        lambda d: d["assignments"][0]["visits"][0].update(dwell_s=float("nan")),
        lambda d: d["assignments"][0]["visits"][0].update(dwell_s=10000),
    ],
)
def test_untrusted_model_output_cannot_bypass_scene_constraints(mutate):
    d = draft()
    mutate(d)
    with pytest.raises(ValueError):
        apply_design(recipe(), d)


def test_provider_is_bounded_and_records_usage_without_credentials(tmp_path, monkeypatch):
    config = tmp_path / "provider.json"
    config.write_text(json.dumps({"base_url": "https://api.gravitex.ai", "api_key": "fake-secret", "max_calls": 2}))
    config.chmod(0o600)
    ledger = tmp_path / "ledger.jsonl"
    requests = []

    class Response:
        def __init__(self):
            self.headers = {"x-request-id": "test-request"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            return json.dumps(
                {
                    "model": "deepseek-v4-pro",
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(draft())}}],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600},
                }
            ).encode()

    def fake(req, timeout):
        payload = json.loads(req.data)
        requests.append(payload)
        assert payload["max_tokens"] == 1800 and payload["thinking"] == {"type": "disabled"}
        assert req.full_url == "https://api.gravitex.ai/v1/chat/completions"
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    for _ in range(2):
        _, record = design(recipe(), "Shopping errands", config_path=config, ledger_path=ledger)
        assert record["usage"]["total_tokens"] == 600
    with pytest.raises(ValueError, match="budget"):
        design(recipe(), "Again", config_path=config, ledger_path=ledger)
    assert len(requests) == 2 and "fake-secret" not in ledger.read_text()


def test_context_excludes_disconnected_facilities_and_fixed_objects():
    from awr.sim.activities.designer import design_context

    doc = recipe()
    doc["actors"][1].update(stationary=True)
    context = design_context(doc)
    assert all(fid.startswith("a000-") for fid in context["actors"][0]["allowed_facilities"])
    assert context["actors"][1]["allowed_facilities"] == []
    assert all(fid.startswith("a000-") for fid in context["facilities"])
    d = draft()
    d["assignments"][0]["actor_id"] = "a001"
    with pytest.raises(ValueError, match="Installed objects"):
        apply_design(doc, d)
