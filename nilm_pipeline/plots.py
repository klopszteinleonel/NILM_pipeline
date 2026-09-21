"""Gráficos de sanity check y de resultados para el notebook (matplotlib)."""
from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

from .augment import aumentar_ventana
from .config import Config
from .evaluate import matrices_por_clase
from .features import armonicas, espectro, estimar_f0
from .preprocess import preprocesar_ventanas
from .segment import segmentar


def plot_captura(x: np.ndarray, fs: float, cfg: Config, titulo: str = "", fmax: float = 1500.0,
                 ms_detalle: float = 100.0):
    """Señal completa, detalle de los primeros ms y espectro de la primera
    ventana con las armónicas de f0 estimada marcadas."""
    x = np.asarray(x, dtype=np.float32)
    V = segmentar(x, cfg.W, cfg.solape)[:1]
    Vp, esc = preprocesar_ventanas(V, cfg)
    mag, freqs = espectro(Vp, cfg, fs)
    f0 = estimar_f0(mag, freqs, cfg.f_red)[0]
    H = armonicas(mag, freqs, np.array([f0]), cfg.n_armonicas, cfg.armonica_tol_bins)[0]

    fig, ax = plt.subplots(1, 3, figsize=(15, 3.6))
    t = np.arange(len(x)) / fs
    ax[0].plot(t, x, lw=0.6)
    ax[0].set(title=f"{titulo}  RMS={np.sqrt(np.mean(x**2)):.2f} A", xlabel="t [s]", ylabel="I [A]")
    n = int(ms_detalle / 1000 * fs)
    ax[1].plot(t[:n] * 1000, x[:n], lw=0.9)
    ax[1].set(title=f"detalle {ms_detalle:.0f} ms", xlabel="t [ms]")
    m = freqs <= fmax
    ax[2].semilogy(freqs[m], mag[0, m] * esc[0] + 1e-6, lw=0.8)
    ks = np.arange(1, cfg.n_armonicas + 1)
    fk = ks * f0
    mk = fk <= fmax
    ax[2].plot(fk[mk], H[mk] * esc[0] + 1e-6, "o", ms=4, color="C3", label=f"H_k (f0={f0:.2f} Hz)")
    ax[2].set(title=f"espectro ventana 0 (W={cfg.W}, {cfg.ventana})", xlabel="f [Hz]", ylabel="|I| [A]")
    ax[2].legend(fontsize=8)
    fig.tight_layout()
    return fig


def plot_mix(senales: list, etiquetas: list, mezcla: np.ndarray, fs: float, ms: float = 60.0):
    """Cargas individuales y su suma sintética (primeros ms)."""
    n = int(ms / 1000 * fs)
    t = np.arange(n) / fs * 1000
    fig, ax = plt.subplots(len(senales) + 1, 1, figsize=(10, 1.6 * (len(senales) + 1)), sharex=True)
    for a, s, e in zip(ax, senales, etiquetas):
        a.plot(t, s[:n], lw=0.8); a.set_ylabel(e, fontsize=8)
    ax[-1].plot(t, mezcla[:n], lw=0.8, color="C3"); ax[-1].set_ylabel("mezcla", fontsize=8)
    ax[-1].set_xlabel("t [ms]")
    fig.tight_layout()
    return fig


def plot_augment(v: np.ndarray, cfg: Config, fs: float, n_copias: int = 3, seed: int = 0, ms: float = 60.0):
    """Ventana original y copias aumentadas (tiempo y espectro)."""
    rng = np.random.default_rng(seed)
    copias = [aumentar_ventana(v, cfg, rng, fs) for _ in range(n_copias)]
    n = int(ms / 1000 * fs)
    t = np.arange(n) / fs * 1000
    fig, ax = plt.subplots(1, 2, figsize=(13, 3.4))
    ax[0].plot(t, v[:n], lw=1.2, color="k", label="original")
    for i, c in enumerate(copias):
        ax[0].plot(t, c[:n], lw=0.7, alpha=0.7, label=f"aug {i + 1}")
    ax[0].set(title="augment (tiempo)", xlabel="t [ms]", ylabel="I [A]"); ax[0].legend(fontsize=7)
    Vs = np.stack([v] + copias)
    mag, freqs = espectro(Vs, cfg, fs)
    m = freqs <= 1500
    for i, row in enumerate(mag):
        ax[1].semilogy(freqs[m], row[m] + 1e-6, lw=0.7, color="k" if i == 0 else None, alpha=1 if i == 0 else 0.6)
    ax[1].set(title="augment (espectro)", xlabel="f [Hz]")
    fig.tight_layout()
    return fig


def plot_matrices_por_clase(Y: np.ndarray, P: np.ndarray, umbral, clases: list):
    """Una matriz 2x2 por clase ([[TN, FP], [FN, TP]])."""
    M = matrices_por_clase(Y, P, umbral)
    n = len(clases)
    cols = min(6, n)
    filas = int(np.ceil(n / cols))
    fig, ax = plt.subplots(filas, cols, figsize=(2.2 * cols, 2.3 * filas))
    for a, c, m in zip(np.ravel(ax), clases, M):
        a.imshow(m, cmap="Blues", vmin=0)
        for (i, j), v in np.ndenumerate(m):
            a.text(j, i, int(v), ha="center", va="center", fontsize=9,
                   color="white" if v > m.max() / 2 else "black")
        a.set(title=c, xticks=[0, 1], yticks=[0, 1], xticklabels=["off", "on"], yticklabels=["off", "on"])
        a.tick_params(labelsize=7)
    for a in np.ravel(ax)[n:]:
        a.axis("off")
    fig.suptitle("filas = real, columnas = predicho", fontsize=9)
    fig.tight_layout()
    return fig


def plot_importancias(imp: dict, top: int = 20):
    items = list(imp.items())[:top][::-1]
    fig, ax = plt.subplots(figsize=(7, 0.3 * len(items) + 1))
    ax.barh([k for k, _ in items], [v for _, v in items])
    ax.set(title=f"importancia media (MDI), top {top}")
    fig.tight_layout()
    return fig


def plot_leaderboard(df, x: str = "nodos_totales", y: str = "mix_c_f1_micro", y2: str = "ind_c_f1_micro"):
    """Métrica vs costo del modelo, un punto por experimento."""
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.scatter(df[x], df[y], label=y)
    ax.scatter(df[x], df[y2], marker="x", label=y2)
    for _, r in df.iterrows():
        ax.annotate(r["nombre"], (r[x], r[y]), fontsize=6, alpha=0.7)
    ax.set(xlabel=x, ylabel="F1", xscale="log", title="leaderboard: métrica vs costo")
    ax.grid(alpha=0.3); ax.legend(fontsize=8)
    fig.tight_layout()
    return fig


def plot_robustez(rob: dict):
    fig, ax = plt.subplots(figsize=(5, 3.2))
    ks = sorted(rob, reverse=True)
    ax.plot(ks, [rob[k] for k in ks], "o-")
    ax.set(xlabel="SNR [dB]", ylabel="F1 micro (por captura)", title="robustez a ruido")
    ax.invert_xaxis(); ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig
