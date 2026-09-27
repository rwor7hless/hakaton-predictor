"""API ML-ядра: прогноз ансамбля, интервалы, вероятности, объяснения."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, needs_models


@pytest.fixture(scope="module")
def client():
    from mlcore.api import app
    with TestClient(app) as c:
        yield c


@needs_models
def test_predict(client):
    cache = ROOT / "artifacts/cache/features_test.parquet"
    if not cache.exists():
        pytest.skip("нет кэша признаков")
    df = pd.read_parquet(cache).head(20)
    items = []
    for r in df.to_dict("records"):
        f = {k: (None if isinstance(v, float) and v != v else v) for k, v in r.items() if k != "seq"}
        f["seq"] = [float(x) for x in r["seq"]]
        items.append({"id": str(r["sample_id"]), "features": f})
    j = client.post("/v1/predict", json={"items": items}).json()
    assert len(j["items"]) == 20
    for it in j["items"]:
        assert it["q10_s"] <= it["delay_s"] <= it["q90_s"]
        assert 0 <= it["p_late"] <= 1 and 0 <= it["p_early"] <= 1
        assert it["contributions"]
    info = client.get("/v1/model").json()
    assert info["metrics"]["offline_mae_s"] < info["metrics"]["baseline_mae_s"]


@needs_models
@pytest.mark.parametrize("path, example_name", [
    ("/v1/predict", "online"),
    ("/v1/predict/offline", "offline"),
])
def test_swagger_examples_predict(client, path, example_name):
    spec = client.get("/openapi.json").json()
    body = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]
    example = body["examples"][example_name]["value"]
    response = client.post(path, json=example)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


@needs_models
def test_missing_categories_and_bad_offline_sequence(client):
    item = {"id": "without-categories", "features": {"lead": 660, "cur_dev": 10}}
    for path in ("/v1/predict", "/v1/predict/offline"):
        response = client.post(path, json={"items": [item], "explain": False})
        assert response.status_code == 200, response.text

    item["features"]["seq"] = ["invalid"] * 120
    response = client.post("/v1/predict/offline", json={"items": [item]})
    assert response.status_code == 422
    assert client.post("/v1/predict/offline", json={"items": []}).json()["items"] == []
