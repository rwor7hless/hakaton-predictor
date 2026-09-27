"""PyTorch-модель: GRU по последним 10 минутам телеметрии + MLP по табличным признакам.

Дополняет CatBoost в ансамбле онлайн-прогноза. Обучается на GPU (CUDA), если он
доступен. Для инференса экспортируется в ONNX и исполняется через onnxruntime
(CUDA- или CPU-провайдер) без PyTorch в горячем пути.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from transit_core.features import SEQ_BINS, SEQ_CHANNELS

from .models import ONLINE_COLS, PRED_CLIP

NUM_COLS = [c for c in ONLINE_COLS if c not in ("tgt_stop", "route")]
Y_SCALE = 100.0


def _net(n_num: int, n_stop: int, n_route: int, n_models: int):
    import torch
    from torch import nn

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.stop = nn.Embedding(n_stop, 8)
            self.route = nn.Embedding(n_route, 4)
            self.gru = nn.GRU(SEQ_CHANNELS, 32, batch_first=True)
            self.head = nn.Sequential(nn.Linear(2 * n_num + 8 + 4 + 32, 64), nn.ReLU(), nn.Dropout(0.1),
                                      nn.Linear(64, 32), nn.ReLU(), nn.Linear(32, 1))

        def forward(self, num, stop, route, seq):
            _, h = self.gru(seq)
            z = torch.cat([num, self.stop(stop), self.route(route), h[-1]], dim=1)
            return self.head(z).squeeze(1)

    class Ensemble(nn.Module):
        def __init__(self):
            super().__init__()
            self.nets = nn.ModuleList([Net() for _ in range(n_models)])

        def forward(self, num, stop, route, seq):
            return torch.stack([n(num, stop, route, seq) for n in self.nets]).mean(0) * Y_SCALE

    return Ensemble()


@dataclass
class SeqModel:
    """Обёртка: препроцессинг, обучение, экспорт/загрузка ONNX."""

    mean: np.ndarray | None = None
    std: np.ndarray | None = None
    stop_vocab: dict[str, int] = field(default_factory=dict)
    route_vocab: dict[str, int] = field(default_factory=dict)
    n_models: int = 5
    epochs: int = 80
    num_cols: list[str] = field(default_factory=lambda: list(NUM_COLS))
    net: object = None
    session: object = None

    def _inputs(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        X = df.reindex(columns=self.num_cols).apply(pd.to_numeric, errors="coerce").to_numpy(np.float64)
        miss = np.isnan(X)
        Z = np.where(miss, 0.0, (X - self.mean) / self.std)
        num = np.concatenate([np.clip(Z, -6, 6), miss.astype(float)], axis=1).astype(np.float32)
        stop = np.array([self.stop_vocab.get(str(s), 0) for s in df["tgt_stop"]], dtype=np.int64)
        route = np.array([self.route_vocab.get(str(s), 0) for s in df["route"]], dtype=np.int64)
        seq = np.stack([np.asarray(s, dtype=np.float32).reshape(SEQ_BINS, SEQ_CHANNELS) for s in df["seq"]])
        return {"num": num, "stop": stop, "route": route, "seq": seq}

    @classmethod
    def fit_frame(cls, df: pd.DataFrame, y: np.ndarray, n_models: int = 5, epochs: int = 80,
                  num_cols: list[str] | None = None) -> "SeqModel":
        """Обучить ансамбль из ``n_models`` сетей с L1-потерей."""
        import torch

        m = cls(n_models=n_models, epochs=epochs, num_cols=list(num_cols or NUM_COLS))
        X = df.reindex(columns=m.num_cols).apply(pd.to_numeric, errors="coerce").to_numpy(np.float64)
        m.mean = np.nanmean(X, axis=0)
        m.std = np.nanstd(X, axis=0) + 1e-6
        m.mean = np.nan_to_num(m.mean)
        m.std = np.where(np.isfinite(m.std), m.std, 1.0)
        m.stop_vocab = {k: i + 1 for i, k in enumerate(sorted(df["tgt_stop"].astype(str).unique()))}
        m.route_vocab = {k: i + 1 for i, k in enumerate(sorted(df["route"].astype(str).unique()))}
        dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        inp = {k: torch.from_numpy(v).to(dev) for k, v in m._inputs(df).items()}
        yt = torch.from_numpy((np.asarray(y, dtype=np.float32) / Y_SCALE)).to(dev)
        m.net = _net(len(m.num_cols), len(m.stop_vocab) + 1, len(m.route_vocab) + 1, n_models).to(dev)
        for k, net in enumerate(m.net.nets):
            torch.manual_seed(k)
            opt = torch.optim.AdamW(net.parameters(), lr=3e-3, weight_decay=1e-4)
            sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
            n = len(yt)
            for _ in range(epochs):
                net.train()
                perm = torch.randperm(n, device=dev)
                for b in range(0, n, 128):
                    ix = perm[b:b + 128]
                    opt.zero_grad()
                    loss = torch.nn.functional.l1_loss(net(inp["num"][ix], inp["stop"][ix], inp["route"][ix],
                                                           inp["seq"][ix]), yt[ix])
                    loss.backward()
                    opt.step()
                sched.step()
        m.net = m.net.cpu().eval()
        return m

    def predict_frame(self, df: pd.DataFrame) -> np.ndarray:
        """Прогноз задержки (с) для таблицы признаков с колонкой ``seq``."""
        inp = self._inputs(df)
        if self.session is not None:
            out = self.session.run(None, inp)[0]
        else:
            import torch
            with torch.no_grad():
                out = self.net(*[torch.from_numpy(inp[k]) for k in ("num", "stop", "route", "seq")]).numpy()
        return np.clip(out, *PRED_CLIP)

    def save(self, path: Path, name: str = "seq") -> None:
        """Сохранить препроцессинг (json), веса PyTorch и ONNX-граф."""
        import torch

        path.mkdir(parents=True, exist_ok=True)
        meta = {"mean": self.mean.tolist(), "std": self.std.tolist(), "stop_vocab": self.stop_vocab,
                "route_vocab": self.route_vocab, "n_models": self.n_models, "num_cols": self.num_cols}
        (path / f"{name}_meta.json").write_text(json.dumps(meta))
        torch.save(self.net.state_dict(), path / f"{name}.pt")
        n_num = 2 * len(self.num_cols)
        dummy = (torch.zeros(2, n_num), torch.zeros(2, dtype=torch.long), torch.zeros(2, dtype=torch.long),
                 torch.zeros(2, SEQ_BINS, SEQ_CHANNELS))
        torch.onnx.export(self.net, dummy, str(path / f"{name}.onnx"), input_names=["num", "stop", "route", "seq"],
                          output_names=["delay"], dynamic_axes={k: {0: "batch"} for k in ("num", "stop", "route", "seq", "delay")},
                          opset_version=17, dynamo=False)

    @classmethod
    def load(cls, path: Path, name: str = "seq") -> "SeqModel":
        """Загрузить модель; инференс через onnxruntime."""
        import onnxruntime as ort

        meta = json.loads((path / f"{name}_meta.json").read_text())
        m = cls(mean=np.array(meta["mean"]), std=np.array(meta["std"]), stop_vocab=meta["stop_vocab"],
                route_vocab=meta["route_vocab"], n_models=meta["n_models"], num_cols=meta["num_cols"])
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
        m.session = ort.InferenceSession(str(path / f"{name}.onnx"), so, providers=providers)
        return m
