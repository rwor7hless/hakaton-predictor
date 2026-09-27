"""Frozen platform v7: 80% residual CatBoost + 20% original GRU.

Predict: python -m mlcore.platform_v7 --output submission-v7.csv
Retrain residual: python -m mlcore.platform_v7 --retrain-residual-to artifacts/models/retrained-v7
This is an offline model requiring the organizer-provided cur_dev_s hint.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from .data import DATASET_DIR, load_points, load_traffic, replay_features, synthetic_shifts, leak_mask
from .models import prepare, PRED_CLIP
from .seq import SeqModel, _net

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODELS = ROOT / 'artifacts/models/platform-v7'


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path) -> dict:
    meta = json.loads((path / 'manifest.json').read_text())
    for name, expected in meta['files_sha256'].items():
        if sha(path / name) != expected:
            raise ValueError(f'Model checksum mismatch: {name}')
    return meta


def load_gru(path: Path) -> SeqModel:
    """Native PyTorch inference from pinned weights; no ONNX export/retraining."""
    import torch
    meta = json.loads((path / 'seq_offline_meta.json').read_text())
    model = SeqModel(mean=np.array(meta['mean']), std=np.array(meta['std']),
                     stop_vocab=meta['stop_vocab'], route_vocab=meta['route_vocab'],
                     n_models=meta['n_models'], num_cols=meta['num_cols'])
    model.net = _net(len(model.num_cols), len(model.stop_vocab) + 1,
                     len(model.route_vocab) + 1, model.n_models)
    model.net.load_state_dict(torch.load(path / 'seq_offline.pt', map_location='cpu', weights_only=True))
    model.net.eval()
    return model


def predict_frame(frame: pd.DataFrame, models: Path = DEFAULT_MODELS) -> np.ndarray:
    meta = load_manifest(models)
    if 'cur_dev' not in frame or not np.isfinite(frame.cur_dev.to_numpy(float)).all():
        raise ValueError('Finite cur_dev is required for offline prediction')
    X = prepare(frame, meta['columns'])
    predictions = []
    for seed in range(meta['seeds']):
        model = CatBoostRegressor()
        model.load_model(str(models / f'model_{seed}.cbm'))
        predictions.append(model.predict(X))
    # Add the hint BEFORE clipping the complete prediction, never clip residuals.
    residual = np.clip(np.mean(predictions, axis=0) + frame.cur_dev.to_numpy(), *PRED_CLIP)
    seq = load_gru(models).predict_frame(frame)
    weights = meta['weights']
    return weights['residual_catboost'] * residual + weights['gru'] * seq


def build_frame(dataset: Path, part: str) -> pd.DataFrame:
    points = dataset / ('validate/points.csv' if part == 'validate' else f'labels/labels_{part}.csv')
    schedule = dataset / part / ('schedule_plan.csv' if part == 'validate' else 'schedule.csv')
    return replay_features(load_points(points), load_traffic(dataset / part / 'traffic.csv'), schedule)


def submission(frame: pd.DataFrame, prediction: np.ndarray, template: pd.DataFrame) -> pd.DataFrame:
    if frame.sample_id.duplicated().any() or template.sample_id.duplicated().any():
        raise ValueError('Duplicate sample_id')
    if set(frame.sample_id) != set(template.sample_id):
        raise ValueError('Prediction IDs do not exactly cover the template')
    values = np.asarray(prediction, dtype=float)
    if values.shape != (len(frame),) or not np.isfinite(values).all():
        raise ValueError('Predictions must be a finite vector matching the frame')
    result = template[['sample_id']].copy()
    result['prediction'] = result.sample_id.map(pd.Series(values, index=frame.sample_id)).round(3)
    return result


def retrain_residual(dataset: Path, source: Path, output: Path) -> None:
    """Refit residual trees on train+test; keep original frozen GRU.

    The validate set contributes only forecast timestamps for the synthetic
    embargo. Its telemetry, schedule facts and hidden labels are never read here.
    """
    if output.exists():
        raise ValueError('Use a new output directory to preserve frozen weights')
    meta = load_manifest(source)
    tr, te = build_frame(dataset, 'train'), build_frame(dataset, 'test')
    va = load_points(dataset / 'validate/points.csv')
    shifts = synthetic_shifts(dataset / 'train/schedule.csv')
    fit = pd.concat([tr[~leak_mask(tr, va, shifts)], te], ignore_index=True)
    X = prepare(fit, meta['columns'])
    y = (fit.target_delay_s - fit.cur_dev).to_numpy()
    output.mkdir(parents=True)
    for seed in range(meta['seeds']):
        model = CatBoostRegressor(loss_function='MAE', iterations=1500, depth=6,
                                  learning_rate=.03, random_seed=seed, verbose=False,
                                  thread_count=4, cat_features=['tgt_stop', 'route'], allow_writing_files=False)
        model.fit(X, y, sample_weight=np.ones(len(fit)))
        model.save_model(str(output / f'model_{seed}.cbm'))
    for name in ['seq_offline.pt', 'seq_offline_meta.json']:
        shutil.copy2(source / name, output / name)
    meta.update(version="platform-v7-residual-refit", n_train=len(fit), platform_score=None, local_test_mae=None,
                platform_score_source='Not evaluated; retrained residual component',
                submission_sha256=None,
                files_sha256={p.name: sha(p) for p in sorted(output.iterdir())})
    (output / 'manifest.json').write_text(json.dumps(meta, indent=2) + '\n')


def main() -> None:
    import torch
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DATASET_DIR)
    parser.add_argument('--models', type=Path, default=DEFAULT_MODELS)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/submission-v7-reproduced.csv')
    parser.add_argument('--retrain-residual-to', type=Path)
    args = parser.parse_args()
    torch.set_num_threads(2)
    if args.retrain_residual_to is not None:
        retrain_residual(args.dataset, args.models, args.retrain_residual_to)
        return
    frame = build_frame(args.dataset, 'validate')
    result = submission(frame, predict_frame(frame, args.models), pd.read_csv(args.dataset / 'sample_submission.csv', sep=';'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, sep=';', index=False, mode='x')
    print(f'{args.output}: {len(result)} rows; sha256={sha(args.output)}')


if __name__ == '__main__':
    main()
