"""Формат submission.csv: строго ``sample_id;prediction``, полное покрытие без дублей."""
import json

import pandas as pd

from conftest import DATASET, ROOT, needs_dataset


@needs_dataset
def test_submission_format():
    sub = pd.read_csv(ROOT / "artifacts/submission.csv", sep=";")
    tmpl = pd.read_csv(DATASET / "sample_submission.csv", sep=";")
    assert list(sub.columns) == ["sample_id", "prediction"]
    assert sub.sample_id.is_unique and set(sub.sample_id) == set(tmpl.sample_id)
    assert sub.prediction.notna().all() and pd.api.types.is_numeric_dtype(sub.prediction)


def test_make_submission_uses_configured_dataset(tmp_path, monkeypatch):
    from mlcore import train

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    pd.DataFrame({"sample_id": [2, 1]}).to_csv(dataset / "sample_submission.csv", sep=";", index=False)
    monkeypatch.setattr(train, "DATASET_DIR", dataset)

    models = tmp_path / "models"
    models.mkdir()
    (models / "meta.json").write_text(json.dumps({"ensemble": {"seq": 0}}))

    class Model:
        def predict(self, frame):
            return frame["value"].to_numpy()

    monkeypatch.setattr(train.CatBag, "load", lambda *_: Model())
    output = tmp_path / "submission.csv"
    result = train.make_submission(
        {"val": pd.DataFrame({"sample_id": [1, 2], "value": [10.2, 20.4]})}, models, output
    )

    assert result["sample_id"].tolist() == [2, 1]
    assert result["prediction"].tolist() == [20.4, 10.2]
    pd.testing.assert_frame_equal(result, pd.read_csv(output, sep=";"))
