"""
Data augmentation sobre ventanas (n, W). SOLO para el conjunto de train.

Transformaciones (todas configurables en Config):
  - ruido gaussiano con SNR uniforme en `snr_db`
  - escalado de amplitud uniforme en `escala` (±5 %: más borra la diferencia
    entre cargas resistivas, que solo se distinguen por amplitud)
  - shift circular aleatorio (fase de red aleatoria)
  - zumbido de f_red (`hum50`, amplitud `hum_rel` × RMS)
  - jitter de frecuencia de red (`jitter_red`): re-muestreo por un factor
    1 + δ/f_red con δ ~ U(-jitter_hz, +jitter_hz); simula red a 49.5–50.5 Hz

Balanceo (`aug_balance`): cada ventana recibe copias proporcionales a la
escasez de su clase más rara (n_max / n_clase), con tope aug_n*aug_max_factor.
Las ventanas "sin carga" (vector cero) reciben aug_n copias.

Las copias heredan grupo e idx_captura de su ventana original.
"""
from __future__ import annotations

import numpy as np

from .config import Config


def copias_por_ventana(Yw: np.ndarray, cfg: Config) -> np.ndarray:
    """Cantidad de copias aumentadas a generar por ventana (int, >= 0)."""
    Yw = np.asarray(Yw)
    n = Yw.shape[0]
    if cfg.aug_n == 0 or n == 0:
        return np.zeros(n, dtype=np.int64)
    if not cfg.aug_balance:
        return np.full(n, cfg.aug_n, dtype=np.int64)
    conteo = Yw.sum(axis=0).astype(np.float64)          # ventanas por clase
    n_max = conteo.max() if conteo.max() > 0 else 1.0
    factor = np.ones(n, dtype=np.float64)
    for i in range(n):
        activas = np.where(Yw[i])[0]
        if len(activas):
            factor[i] = (n_max / conteo[activas]).max()
    tope = cfg.aug_n * cfg.aug_max_factor
    return np.clip(np.rint(cfg.aug_n * factor), 0, tope).astype(np.int64)


def _jitter(v: np.ndarray, factor: float) -> np.ndarray:
    """Re-muestreo circular: v'(i) = v(i·factor mod W) (interpolación lineal)."""
    W = len(v)
    pos = (np.arange(W) * factor) % W
    vp = np.concatenate([v, v[:1]])          # extensión periódica para interpolar en [W-1, W)
    return np.interp(pos, np.arange(W + 1), vp).astype(np.float32)


def aumentar_ventana(v: np.ndarray, cfg: Config, rng: np.random.Generator, fs: float) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    W = len(v)
    if cfg.jitter_red:
        delta = rng.uniform(-cfg.jitter_hz, cfg.jitter_hz)
        v = _jitter(v, 1.0 + delta / cfg.f_red)
    if cfg.shift:
        v = np.roll(v, int(rng.integers(0, W)))
    v = v * np.float32(rng.uniform(*cfg.escala))
    rms = float(np.sqrt(np.mean(v ** 2)))
    if cfg.hum50:
        t = np.arange(W, dtype=np.float32) / fs
        fase = rng.uniform(0, 2 * np.pi)
        v = v + np.float32(cfg.hum_rel * rms * np.sqrt(2)) * np.sin(2 * np.pi * cfg.f_red * t + fase).astype(np.float32)
    snr = rng.uniform(*cfg.snr_db)
    sigma = rms / (10.0 ** (snr / 20.0))
    v = v + rng.normal(0.0, sigma, W).astype(np.float32)
    return v.astype(np.float32)


def aumentar_ventanas(X: np.ndarray, Yw: np.ndarray, Gw: np.ndarray, idx: np.ndarray,
                      cfg: Config, rng: np.random.Generator, fs: float,
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Devuelve originales + copias aumentadas (X, Yw, Gw, idx) con las copias
    al final, en el orden de sus originales."""
    X = np.asarray(X, dtype=np.float32)
    copias = copias_por_ventana(Yw, cfg)
    total = int(copias.sum())
    if total == 0:
        return X, np.asarray(Yw), np.asarray(Gw), np.asarray(idx)
    Xa = np.empty((total, X.shape[1]), dtype=np.float32)
    origen = np.repeat(np.arange(X.shape[0]), copias)
    for k, i in enumerate(origen):
        Xa[k] = aumentar_ventana(X[i], cfg, rng, fs)
    return (np.concatenate([X, Xa]),
            np.concatenate([Yw, np.asarray(Yw)[origen]]),
            np.concatenate([Gw, np.asarray(Gw)[origen]]),
            np.concatenate([idx, np.asarray(idx)[origen]]))
