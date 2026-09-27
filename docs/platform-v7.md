# Frozen platform model v7

The team reported **1.00000** for `artifacts/submission-v7.csv`. This is a capped,
normalized platform score, **not 100% accuracy or zero MAE**. The report was not
independently verified. Repeated platform submissions informed ensemble weights;
these results must not be presented as independent generalization evidence.

## Exact model

`0.8 * clip(cur_dev + mean(residual CatBoost), -600, 900) + 0.2 * original GRU`

Five CatBoost models learn `target_delay_s - cur_dev_s`: MAE loss, depth 6,
1500 iterations, learning rate 0.03, seeds 0–4. The features match the original
offline CatBoost. The original five-network GRU is retained unchanged from
commit `90be581481c8830db8e4fd8cac12c66e1de98f51`.
The bundle in `artifacts/models/platform-v7` is self-contained and checksummed.
`manifest.json` records the exact submission SHA-256 and provenance.

This separate offline entry point requires the official current-delay hint.
It does not change the deployed online predictor, which lacks that hint.
The original training command and original models/submissions remain available.

## Reproduce the submission

From the repository root, with the existing ML dependencies installed:

```bash
PYTHONPATH=services/ml:libs/transit_core python -m mlcore.platform_v7 \
  --dataset data/dataset --output artifacts/submission-v7-reproduced.csv
```

The command replays raw validate telemetry only up to each forecast time,
uses planned schedules, restores the frozen weights with native PyTorch,
and writes all template IDs with three decimal places and `;` delimiters.
An existing output is not overwritten. No ONNX export, feature pickle, or
external Hakaton directory is needed. Compare the printed SHA-256 with the
manifest. Exact reproduction was checked using Python 3.12, NumPy 2.5.3,
pandas 2.3.3, CatBoost 1.2.10, and PyTorch 2.14.0.

## Refit the residual component

```bash
PYTHONPATH=services/ml:libs/transit_core python -m mlcore.platform_v7 \
  --dataset data/dataset --retrain-residual-to artifacts/models/retrained-v7
```

The output must be a new directory. This rebuilds features from raw train/test,
keeps the original synthetic-source 40-minute embargo around validate points,
and trains on 3893 rows. The original frozen GRU is copied unchanged. Predict
with `--models artifacts/models/retrained-v7`. This is **residual retraining**,
not a claim that retraining the original GRU reproduces its weights: its original
initialization was not fixed before network construction.

## Local evidence and limitations

On the previously explored 353-row local test, v7 has MAE **66.1606 s**; the
50/50 residual/GRU blend has **65.6232 s**. Increasing residual weight from 70%
to 80% helped 2 of 6 robustness blocks and hurt 4. Thus v7 is the best reported
platform submission, **not the best established local or new-day model**.
Final weights include train+test; never evaluate those weights on labels_test.
These local metrics came from models retrained without test and nearby synthetic
copies. Synthetic dependencies and all prior model selection remain relevant.

## Verification

```bash
python -m unittest discover -s tests -p test_platform_v7.py -v
```

Checks cover the model checksums, required hint, IDs, ordering, finite predictions,
and rounding. Full raw-data reproduction is a separate integration check via
the command above. Keep this bundle frozen while conducting local experiments.
