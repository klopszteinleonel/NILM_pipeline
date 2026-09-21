"""
Segmentación de capturas en ventanas de W muestras con solape.

Cada ventana hereda la etiqueta multi-hot y el grupo de su captura de origen.
`idx_captura` permite volver a agregar predicciones por captura (media de
probabilidades sobre sus ventanas), que es lo que haría el firmware con
suavizado temporal.
"""
from __future__ import annotations

import warnings

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .config import Config


def segmentar(x: np.ndarray, W: int, solape: float) -> np.ndarray:
    """(N,) -> (n_ventanas, W). Devuelve una copia contigua.
    n_ventanas = (N - W) // paso + 1; si N < W devuelve (0, W)."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim != 1:
        raise ValueError(f"se esperaba señal 1-D, llegó {x.shape}")
    if len(x) < W:
        return np.empty((0, W), dtype=np.float32)
    paso = max(1, int(round(W * (1.0 - solape))))
    return np.ascontiguousarray(sliding_window_view(x, W)[::paso])


def segmentar_dataset(senales: list, Y: np.ndarray, grupos: np.ndarray, cfg: Config,
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Segmenta una lista de capturas.

    Devuelve (X, Yw, Gw, idx_captura):
        X            (n_ventanas, W) float32
        Yw           (n_ventanas, n_clases) uint8, copia de Y[i] por ventana
        Gw           (n_ventanas,) grupo de la captura de origen
        idx_captura  (n_ventanas,) índice i de la captura de origen en `senales`
    """
    Y = np.asarray(Y)
    grupos = np.asarray(grupos)
    if not (len(senales) == len(Y) == len(grupos)):
        raise ValueError("senales, Y y grupos deben tener la misma longitud")

    bloques, ys, gs, idxs = [], [], [], []
    for i, x in enumerate(senales):
        V = segmentar(x, cfg.W, cfg.solape)
        if V.shape[0] == 0:
            warnings.warn(f"captura {grupos[i]}: {len(x)} muestras < W={cfg.W}, se omite")
            continue
        bloques.append(V)
        ys.append(np.repeat(Y[i][None, :], V.shape[0], axis=0))
        gs.append(np.repeat(grupos[i], V.shape[0]))
        idxs.append(np.full(V.shape[0], i, dtype=np.int64))

    if not bloques:
        n_clases = Y.shape[1] if Y.ndim == 2 else 0
        return (np.empty((0, cfg.W), np.float32), np.empty((0, n_clases), np.uint8),
                np.empty(0, grupos.dtype), np.empty(0, np.int64))
    return (np.concatenate(bloques), np.concatenate(ys).astype(np.uint8),
            np.concatenate(gs), np.concatenate(idxs))
