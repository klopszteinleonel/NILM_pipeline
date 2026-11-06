"""
Extracción de features por ventana. Dos estrategias intercambiables:

  compact  (~45 feats): tiempo + armónicas de f_red + estadísticos espectrales
                        + energía por bandas. Pensada para el ESP32-S3.
  rawfft   (base): SOLO la magnitud de los bins directos de la FFT (sin el
             bin DC, que se anula al quitar la media) hasta rawfft_fmax. Con
             W=2048 a 10 kHz: 409 bins de 4.88 Hz. Es exactamente lo que
             calcula el borde (FFT radix-2 de W puntos, |X|·2/Σw), sin
             interpolar. La amplitud absoluta va implícita en los bins (se
             multiplican por `escala` si hubo normalización). La dimensión
             depende de W y fs: cada modelo se entrena y despliega con un único W.

Decisión A (amplitud absoluta): `extraer_features` recibe `escala` (factor por
el que preprocess.py dividió cada ventana) y lo usa para expresar RMS, pico y
|H1| en Amperes reales aunque la ventana esté normalizada. Las features de
forma (H_k/H1, estadísticos, bandas) son invariantes a la escala.

Decisión D (robustez a f0): se estima la frecuencia de red real por
interpolación parabólica alrededor del pico de f_red, y cada armónica k se
toma como el pico (interpolado) dentro de ±armonica_tol_bins de k·f0. Así una
red a 49.8 Hz no corre la armónica 20 fuera del bin.

Espectro: magnitud de un solo lado normalizada por la suma de la ventana, de
modo que una senoidal de amplitud A da un pico ≈ A. Equivalente en esp-dsp:
dsps_fft2r_fc32 + |X|·2/Σw.

Precisión medida con senoidal sintética (W=1024..4096): f0 ±0.08 Hz y |H1|
±3.5 % con hann/hamming/blackman. `rect` sufre fuga espectral (|H1| -12 %) y
`flattop` estima bien amplitud pero mal f0 (lóbulo ancho): son experimentos.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
from scipy import stats
from scipy.signal import windows

from .config import Config

_EPS = 1e-12
_F0_BUSQUEDA_REL = 0.2      # ±20 % de f_red para buscar el pico de la fundamental


def ventana_fft(nombre: str, W: int) -> np.ndarray:
    """Coeficientes de la ventana (periódica, para análisis espectral)."""
    if nombre == "rect":
        return np.ones(W, dtype=np.float32)
    fn = {"hann": windows.hann, "hamming": windows.hamming,
          "blackman": windows.blackman, "flattop": windows.flattop}[nombre]
    return fn(W, sym=False).astype(np.float32)


def espectro(V: np.ndarray, cfg: Config, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """(n, W) -> (mag (n, n_fft//2+1) amplitud de un solo lado, freqs (n_fft//2+1,))."""
    V = np.asarray(V, dtype=np.float32)
    w = ventana_fft(cfg.ventana, V.shape[1])
    n_fft = cfg.n_fft_efectivo
    X = np.fft.rfft(V * w, n=n_fft, axis=1)
    mag = (2.0 * np.abs(X) / w.sum()).astype(np.float32)
    return mag, np.fft.rfftfreq(n_fft, d=1.0 / fs).astype(np.float32)


def _pico_parabolico(mag: np.ndarray, k: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Interpolación parabólica en log-magnitud alrededor del bin k (por fila).
    En dominio log el sesgo con Hann/Hamming es < 0.01 bin.
    Devuelve (posición fraccional del pico en bins, magnitud del pico)."""
    n, nb = mag.shape
    filas = np.arange(n)
    k = np.clip(k, 1, nb - 2)
    a, b, c = (np.log(mag[filas, j] + _EPS) for j in (k - 1, k, k + 1))
    den = a - 2 * b + c
    seguro = np.abs(den) > _EPS
    p = np.where(seguro, 0.5 * (a - c) / np.where(seguro, den, 1.0), 0.0)
    p = np.clip(p, -0.5, 0.5)
    m = np.exp(b - 0.25 * (a - c) * p)
    return k + p, m


def estimar_f0(mag: np.ndarray, freqs: np.ndarray, f_red: float) -> np.ndarray:
    """Frecuencia de red real por ventana (Hz), buscando el pico en ±20 % de f_red."""
    df = float(freqs[1] - freqs[0])
    lo = max(1, int(np.floor((1 - _F0_BUSQUEDA_REL) * f_red / df)))
    hi = min(mag.shape[1] - 2, int(np.ceil((1 + _F0_BUSQUEDA_REL) * f_red / df)))
    k = lo + np.argmax(mag[:, lo:hi + 1], axis=1)
    pos, _ = _pico_parabolico(mag, k)
    return (pos * df).astype(np.float32)


def armonicas(mag: np.ndarray, freqs: np.ndarray, f0: np.ndarray, n_armonicas: int,
              tol_bins: int) -> np.ndarray:
    """Magnitud (interpolada) de las armónicas 1..n_armonicas de f0. (n, n_armonicas)."""
    n, nb = mag.shape
    df = float(freqs[1] - freqs[0])
    filas = np.arange(n)[:, None]
    offsets = np.arange(-tol_bins, tol_bins + 1)
    H = np.zeros((n, n_armonicas), dtype=np.float32)
    for k in range(1, n_armonicas + 1):
        centro = np.rint(k * f0 / df).astype(int)
        cand = np.clip(centro[:, None] + offsets[None, :], 1, nb - 2)
        mejor = cand[np.arange(n), np.argmax(mag[filas, cand], axis=1)]
        _, H[:, k - 1] = _pico_parabolico(mag, mejor)
    return H


def _estadisticos_espectrales(mag: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    """centroide, dispersión, rolloff85, rolloff95, planitud, entropía (excluye DC)."""
    m = mag[:, 1:].astype(np.float64)
    f = freqs[1:].astype(np.float64)
    p = m ** 2
    ptot = p.sum(axis=1, keepdims=True) + _EPS
    pn = p / ptot
    centroide = (pn * f).sum(axis=1)
    dispersion = np.sqrt((pn * (f - centroide[:, None]) ** 2).sum(axis=1))
    acum = np.cumsum(pn, axis=1)
    r85 = f[np.argmax(acum >= 0.85, axis=1)]
    r95 = f[np.argmax(acum >= 0.95, axis=1)]
    planitud = np.exp(np.log(m + _EPS).mean(axis=1)) / (m.mean(axis=1) + _EPS)
    entropia = -(pn * np.log(pn + _EPS)).sum(axis=1) / np.log(pn.shape[1])
    return np.column_stack([centroide, dispersion, r85, r95, planitud, entropia])


def _bandas(mag: np.ndarray, freqs: np.ndarray, bordes: tuple) -> np.ndarray:
    p = mag[:, 1:].astype(np.float64) ** 2
    f = freqs[1:]
    ptot = p.sum(axis=1) + _EPS
    return np.column_stack([p[:, (f >= lo) & (f < hi)].sum(axis=1) / ptot
                            for lo, hi in zip(bordes[:-1], bordes[1:])])


def _tiempo(V: np.ndarray, escala: np.ndarray) -> np.ndarray:
    """rms_A, pico_A, cresta, zcr, curtosis, asimetria."""
    rms = np.sqrt(np.mean(V ** 2, axis=1))
    pico = np.abs(V).max(axis=1)
    cresta = pico / (rms + _EPS)
    zcr = (np.diff(np.signbit(V), axis=1) != 0).mean(axis=1)
    curt = stats.kurtosis(V, axis=1, fisher=True)
    asim = stats.skew(V, axis=1)
    return np.column_stack([rms * escala, pico * escala, cresta, zcr, curt, asim])


@lru_cache(maxsize=32)
def _sesgo_f0(W: int, n_fft: int, ventana: str, fs: float, f_red: float) -> float:
    """Cuánto lee de más estimar_f0 con esta ventana y esta FFT, medido sobre un seno de f_red.
    Con W = n_fft = 2048 son +0.074 Hz; con más puntos de FFT es casi cero."""
    t = np.arange(W) / fs
    w = ventana_fft(ventana, W)
    x = np.sin(2 * np.pi * f_red * t)
    mag = (2 * np.abs(np.fft.rfft((x - x.mean()) * w, n=n_fft)) / w.sum())[None, :].astype(np.float32)
    return float(estimar_f0(mag, np.fft.rfftfreq(n_fft, 1.0 / fs).astype(np.float32), f_red)[0]) - f_red


def _bins_armonicas(cfg: Config, fs: float) -> np.ndarray:
    """Bins a ±rawfft_armonicas_tol de cada armónica, pares e impares. Con el eje corregido por f0, el sesgo
    del estimador deja cada armónica un poco antes de k·f_red, y hay que elegir el bin donde cae realmente."""
    n_fft = cfg.n_fft_efectivo
    df = fs / n_fft
    sesgo = _sesgo_f0(cfg.W, n_fft, cfg.ventana, fs, cfg.f_red) if cfg.rawfft_f0 else 0.0
    bins = set()
    for k in range(1, int(cfg.rawfft_fmax // cfg.f_red) + 1):
        centro = int(round(k * cfg.f_red / df * (1 - sesgo / cfg.f_red)))
        bins.update(range(centro - cfg.rawfft_armonicas_tol, centro + cfg.rawfft_armonicas_tol + 1))
    return np.array(sorted(b for b in bins if 0 < b * df <= cfg.rawfft_fmax))


def bins_rawfft(cfg: Config, fs: float) -> np.ndarray:
    """Índices de bin de la FFT usados por rawfft: 1..k con k·fs/n_fft <= rawfft_fmax."""
    if cfg.rawfft_armonicas_tol is not None:
        return _bins_armonicas(cfg, fs)
    freqs = np.fft.rfftfreq(cfg.n_fft_efectivo, d=1.0 / fs)
    bins = (freqs > 0) & (freqs <= cfg.rawfft_fmax)
    if cfg.rawfft_mascara != "todos":
        bins &= mascara_armonica(freqs, cfg.f_red, "impares" if cfg.rawfft_mascara == "impares" else "armonicas")
    return np.where(bins)[0]


def mascara_armonica(freqs: np.ndarray, f_red: float, tipo: str = "armonicas") -> np.ndarray:
    """Bins que caen sobre armónicas de f_red. La tolerancia crece con k, porque un error de
    f0 de ±0.2 Hz corre la armónica k en k·0.2 Hz. H1 siempre queda incluida.
    tipo: "todo" | "armonicas" | "pares" | "impares"."""
    freqs = np.asarray(freqs, dtype=np.float64)
    if tipo == "todo":
        return np.ones(len(freqs), dtype=bool)
    df = freqs[1] - freqs[0]
    k = np.rint(freqs / f_red)
    cerca = (k >= 1) & (np.abs(freqs - k * f_red) <= (1.5 + np.ceil(k * 0.2 / df)) * df)
    if tipo == "pares":
        cerca &= (k % 2 == 0) | (k == 1)
    elif tipo == "impares":
        cerca &= k % 2 == 1
    return cerca


def _mag_alineada(mag: np.ndarray, freqs: np.ndarray, f_red: float) -> np.ndarray:
    """Magnitud remuestreada para que cada armónica caiga donde caería con f0 = f_red."""
    f0 = estimar_f0(mag, freqs, f_red).astype(np.float64)
    pos = np.arange(mag.shape[1], dtype=np.float64)[None, :] * (f0[:, None] / f_red)
    i = np.clip(pos.astype(np.int64), 0, mag.shape[1] - 2)
    d = np.clip(pos - i, 0.0, 1.0).astype(np.float32)
    return (1 - d) * np.take_along_axis(mag, i, axis=1) + d * np.take_along_axis(mag, i + 1, axis=1)


NOMBRES_EXTRAS = ("rms_A", "pico_A", "cresta", "zcr", "curtosis", "asimetria", "h1_A", "thd", "par_impar", "frac_armonica",
                  "centroide_hz", "dispersion_hz", "rolloff85_hz", "rolloff95_hz", "planitud", "entropia")


def _extras(V: np.ndarray, escala: np.ndarray, mag: np.ndarray, freqs: np.ndarray, cfg: Config) -> np.ndarray:
    """Escalares que no son bins: los de `compact`, sin las armónicas una por una."""
    f0 = estimar_f0(mag, freqs, cfg.f_red)
    H = armonicas(mag, freqs, f0, cfg.n_armonicas, cfg.armonica_tol_bins).astype(np.float64)
    h1, resto = H[:, 0], H[:, 1:]
    thd = np.sqrt((resto ** 2).sum(axis=1)) / (h1 + _EPS)
    par_impar = H[:, 1::2].sum(axis=1) / (H[:, 2::2].sum(axis=1) + _EPS)
    ptot = (mag[:, 1:].astype(np.float64) ** 2).sum(axis=1) + _EPS
    frac = np.clip((H ** 2).sum(axis=1) / ptot, 0.0, 1.0)
    return np.column_stack([_tiempo(V, escala), h1 * escala, thd, par_impar, frac, _estadisticos_espectrales(mag, freqs),
                            _bandas(mag, freqs, cfg.bandas_hz)])


def nombres_features(cfg: Config, fs: float = 10000.0) -> list:
    if cfg.estrategia == "rawfft":
        df = fs / cfg.n_fft_efectivo
        n = [f"fft_{k * df:.2f}Hz" for k in bins_rawfft(cfg, fs)]
        if cfg.rawfft_extras:
            n = n + list(NOMBRES_EXTRAS) + [f"banda_{lo:.0f}_{hi:.0f}" for lo, hi in zip(cfg.bandas_hz[:-1], cfg.bandas_hz[1:])]
        return ["h1_A"] + n if cfg.rawfft_norm == "h1" else n
    nombres = ["rms_A", "pico_A", "cresta", "zcr", "curtosis", "asimetria", "h1_A"]
    suf = "_rel" if cfg.normalizar_forma else "_A"
    nombres += [f"h{k}{suf}" for k in range(2, cfg.n_armonicas + 1)]
    nombres += ["thd", "par_impar", "frac_armonica",
                "centroide_hz", "dispersion_hz", "rolloff85_hz", "rolloff95_hz",
                "planitud", "entropia"]
    nombres += [f"banda_{lo:.0f}_{hi:.0f}" for lo, hi in zip(cfg.bandas_hz[:-1], cfg.bandas_hz[1:])]
    return nombres


def extraer_features(V: np.ndarray, escala: np.ndarray, cfg: Config, fs: float,
                     ) -> tuple[np.ndarray, list]:
    """(n, W) ventanas preprocesadas + escala (n,) -> (F (n, n_feats) float32, nombres)."""
    V = np.asarray(V, dtype=np.float32)
    escala = np.asarray(escala, dtype=np.float64)
    if V.ndim != 2 or escala.shape != (V.shape[0],):
        raise ValueError(f"shapes incompatibles: V {V.shape}, escala {escala.shape}")
    mag, freqs = espectro(V, cfg, fs)
    nombres = nombres_features(cfg, fs)

    if cfg.estrategia == "rawfft":
        extras = _extras(V, escala, mag, freqs, cfg) if cfg.rawfft_extras else None
        if cfg.rawfft_f0:
            mag = _mag_alineada(mag, freqs, cfg.f_red)
        F = mag[:, bins_rawfft(cfg, fs)] * escala[:, None]
        if extras is not None:
            F = np.hstack([F, extras])
        if cfg.rawfft_norm == "h1":
            cerca = (freqs > 0.8 * cfg.f_red) & (freqs < 1.2 * cfg.f_red)
            h1 = mag[:, cerca].max(axis=1, keepdims=True) * escala[:, None]
            F = np.hstack([h1, F / np.maximum(h1, 1e-4)])
        assert F.shape[1] == len(nombres), (F.shape, len(nombres))
        return F.astype(np.float32), nombres

    tiempo = _tiempo(V, escala)

    f0 = estimar_f0(mag, freqs, cfg.f_red) if cfg.f0_estimar \
        else np.full(V.shape[0], cfg.f_red, dtype=np.float32)
    H = armonicas(mag, freqs, f0, cfg.n_armonicas, cfg.armonica_tol_bins).astype(np.float64)
    h1 = H[:, 0]
    h1_abs = h1 * escala
    resto = H[:, 1:]
    forma = resto / (h1[:, None] + _EPS) if cfg.normalizar_forma else resto * escala[:, None]
    thd = np.sqrt((resto ** 2).sum(axis=1)) / (h1 + _EPS)
    pares = H[:, 1::2].sum(axis=1)          # k = 2, 4, 6, ...
    impares = H[:, 2::2].sum(axis=1)        # k = 3, 5, 7, ...
    par_impar = pares / (impares + _EPS)
    ptot = (mag[:, 1:].astype(np.float64) ** 2).sum(axis=1) + _EPS
    frac_arm = np.clip((H ** 2).sum(axis=1) / ptot, 0.0, 1.0)

    F = np.column_stack([
        tiempo, h1_abs, forma, thd, par_impar, frac_arm,
        _estadisticos_espectrales(mag, freqs),
        _bandas(mag, freqs, cfg.bandas_hz),
    ])
    assert F.shape[1] == len(nombres), (F.shape, len(nombres))
    return F.astype(np.float32), nombres
