"""
Preprocesamiento de señales de corriente.

Orden de operaciones (pensado para reproducirse igual en el ESP32-S3):
    1. filtro IIR sobre la captura completa   -> preprocesar_captura
    2. segmentación en ventanas               -> segment.py
    3. DC y normalización por ventana         -> preprocesar_ventanas

`preprocesar_ventanas` devuelve también el factor de escala aplicado a cada
ventana para que features.py pueda calcular las features absolutas (RMS, |H1|
en Amperes) aunque la ventana esté normalizada: la amplitud absoluta es el único
discriminador entre cargas resistivas puras (calefon/plancha/horno/secador).
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from .config import Config


def disenar_filtro(cfg: Config, fs: float) -> np.ndarray | None:
    """Devuelve secciones de segundo orden (n_sec, 6) o None si no hay filtro.
    Los coeficientes se exportan como biquads en export.py."""
    if cfg.filtro is None:
        return None
    if cfg.filtro == "notch":
        b, a = signal.iirnotch(cfg.filtro_fc[0], Q=30.0, fs=fs)
        return signal.tf2sos(b, a)
    btype = {"hp": "highpass", "lp": "lowpass", "bp": "bandpass"}[cfg.filtro]
    fc = cfg.filtro_fc[0] if cfg.filtro != "bp" else list(cfg.filtro_fc)
    return signal.butter(cfg.filtro_orden, fc, btype=btype, fs=fs, output="sos")


def filtrar(x: np.ndarray, sos: np.ndarray | None, causal: bool) -> np.ndarray:
    if sos is None:
        return np.asarray(x, dtype=np.float32)
    y = signal.sosfilt(sos, x) if causal else signal.sosfiltfilt(sos, x)
    return y.astype(np.float32)


def preprocesar_captura(x: np.ndarray, fs: float, cfg: Config) -> np.ndarray:
    """Etapa 1: filtro sobre la captura completa (antes de segmentar)."""
    return filtrar(x, disenar_filtro(cfg, fs), cfg.causal)


def preprocesar_ventanas(V: np.ndarray, cfg: Config) -> tuple[np.ndarray, np.ndarray]:
    """Etapa 3: DC y normalización por ventana, vectorizado sobre (n, W).

    Devuelve (V_proc, escala) donde escala[i] es el factor por el que se dividió
    la ventana i (1.0 si no se normalizó). señal_original ≈ V_proc * escala.
    """
    V = np.asarray(V, dtype=np.float32)
    if V.ndim != 2:
        raise ValueError(f"se esperaba (n, W), llegó {V.shape}")
    if cfg.quitar_dc:
        V = V - V.mean(axis=1, keepdims=True)
    escala = np.ones(V.shape[0], dtype=np.float32)
    if cfg.normalizar == "rms":
        escala = np.sqrt(np.mean(V ** 2, axis=1))
    elif cfg.normalizar == "maxabs":
        escala = np.abs(V).max(axis=1)
    if cfg.normalizar is not None:
        escala = np.where(escala > 0, escala, 1.0).astype(np.float32)
        V = V / escala[:, None]
    return V.astype(np.float32), escala


def preprocesar_ventana(v: np.ndarray, cfg: Config) -> tuple[np.ndarray, float]:
    """Versión de una sola ventana (para plots y sanity checks)."""
    Vp, esc = preprocesar_ventanas(np.asarray(v)[None, :], cfg)
    return Vp[0], float(esc[0])
