"""
Análisis exploratorio de las capturas de corriente (Parte A del notebook).

No entrena nada y no depende del resto del pipeline salvo por la ingesta y el
cálculo del espectro: usa las mismas funciones que el borde (ventana Hann de
2048 muestras, FFT, |X|·2/Σw), así que lo que se ve acá es lo que va a ver el
ESP32-S3.

Unidad de análisis. Las capturas de una ráfaga son casi duplicados, así que las
estadísticas por clase pesan cada ventana con `E.peso`: todas las ráfagas de una
clase valen lo mismo, tengan 2 o 17 capturas.
"""
from __future__ import annotations

from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, PowerNorm
from matplotlib.patches import Rectangle
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import squareform
from scipy.stats import rankdata, spearmanr

from .config import Config
from .data import TIPO_COMBINADA, TIPO_INDIVIDUAL, Dataset
from .features import armonicas, espectro, estimar_f0
from .preprocess import preprocesar_ventanas
from .segment import segmentar

LSB_A = 15.0 * 3.3 / 4096           # 12.08 mA por cuenta con k = 15
RIEL_A = 2048 * LSB_A               # ±24.75 A pico = 17.5 A RMS senoidal
N_ARMONICAS = 60                    # hasta 3 kHz: arriba no hay señal (ver snr_por_banda)
PISO_ETIQUETA = "nada"
# En estas clases cada ocasión es una configuración distinta (ej. otra cantidad de lámparas) y el
# cambio entre capturas puede tardar menos que la pausa que separa ráfagas. Una captura abre una
# ocasión nueva si su RMS salta más que SALTO_NIVEL_DB: las cargas estables saltan ~1 dB como mucho.
CLASES_POR_CONFIGURACION = ("luces",)
SALTO_NIVEL_DB = 1.5
H1_MIN_F0_A = 0.1                   # con menos fundamental, la f0 estimada no es confiable

# --- paleta: colores fijos por lugar, secuencial de un tono, divergente azul-rojo
SUPERFICIE, TINTA, TINTA2, APAGADO, GRILLA, EJE = ("#fcfcfb", "#0b0b0b", "#52514e", "#898781",
                                                   "#e1e0d9", "#c3c2b7")
COLOR_LUGAR = {"casa": ("#2a78d6", "-"), "depaleo": ("#eb6834", "-"), "depamica": ("#1baf7a", "-"),
               "local": (TINTA2, "--")}
SECUENCIAL = LinearSegmentedColormap.from_list(
    "nilm_azul", [SUPERFICIE, "#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"])
DIVERGENTE = LinearSegmentedColormap.from_list(
    "nilm_div", ["#0d366b", "#2a78d6", "#f0efec", "#e34948", "#8f1f1e"])


def estilo() -> None:
    """Fondo claro, grilla y ejes en segundo plano."""
    plt.rcParams.update({
        "figure.facecolor": SUPERFICIE, "axes.facecolor": SUPERFICIE, "savefig.facecolor": SUPERFICIE,
        "axes.edgecolor": EJE, "axes.labelcolor": TINTA2, "axes.titlecolor": TINTA, "text.color": TINTA,
        "xtick.color": APAGADO, "ytick.color": APAGADO, "xtick.labelcolor": TINTA2, "ytick.labelcolor": TINTA2,
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": False,
        "grid.color": GRILLA, "grid.linewidth": 0.6, "axes.axisbelow": True, "axes.linewidth": 0.8,
        "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9, "legend.frameon": False,
        "figure.dpi": 110, "lines.linewidth": 1.5,
    })


# ------------------------------------------------------------------ cálculo --

@dataclass
class Espectros:
    """Espectros de todas las ventanas y descriptores de cada captura."""
    tabla: pd.DataFrame     # una fila por captura
    freqs: np.ndarray       # (bins,)
    mag: np.ndarray         # (ventanas, bins) amplitud [A]
    H: np.ndarray           # (ventanas, N_ARMONICAS) amplitud de k·f0 [A]
    f0: np.ndarray          # (ventanas,)
    cap: np.ndarray         # (ventanas,) índice en tabla
    peso: np.ndarray        # (ventanas,) cada ráfaga pesa igual dentro de su clase
    ciclos: np.ndarray      # (capturas, 200) ciclo promedio alineado a la fundamental
    W: int
    fs: float

    def ventanas(self, etiqueta: str) -> np.ndarray:
        """Máscara de las ventanas de capturas con esa etiqueta."""
        filas = np.where(self.tabla["etiqueta"].values == etiqueta)[0]
        return np.isin(self.cap, filas)

    def clases(self, min_capturas: int = 3, con_piso: bool = True) -> list:
        """Individuales con suficientes capturas, de mayor a menor corriente."""
        t = self.tabla[self.tabla["tipo"] != TIPO_COMBINADA]
        g = t.groupby("etiqueta")["rms_A"].agg(["size", "median"]).sort_values("median", ascending=False)
        out = [c for c, f in g.iterrows() if f["size"] >= min_capturas and c != PISO_ETIQUETA]
        return out + [PISO_ETIQUETA] if con_piso and PISO_ETIQUETA in g.index else out


def _ciclo_promedio(x: np.ndarray, f0: float, fs: float, n: int = 200) -> np.ndarray:
    """Promedia la señal sobre un ciclo, con la fundamental cruzando cero al inicio."""
    t = np.arange(len(x)) / fs
    fase_fund = np.angle(np.sum(x * np.exp(-2j * np.pi * f0 * t)))
    fase = np.mod(2 * np.pi * f0 * t + fase_fund + np.pi / 2, 2 * np.pi)
    idx = np.minimum((fase / (2 * np.pi) * n).astype(int), n - 1)
    suma, cuenta = np.bincount(idx, x, n), np.bincount(idx, minlength=n)
    return suma / np.maximum(cuenta, 1)


def _separar_por_nivel(tabla: pd.DataFrame) -> pd.Series:
    """Ráfagas de CLASES_POR_CONFIGURACION divididas donde el nivel salta: 'r17' pasa a 'r17.0', 'r17.1'..."""
    out = tabla["rafaga"].copy()
    for _, g in tabla[tabla["etiqueta"].isin(CLASES_POR_CONFIGURACION)].groupby("rafaga"):
        saltos = np.abs(20 * np.log10(g["rms_A"].values[1:] / g["rms_A"].values[:-1])) > SALTO_NIVEL_DB
        if saltos.any():
            n = np.r_[0, np.cumsum(saltos)]
            out.loc[g.index] = [f"{r}.{i}" for r, i in zip(g["rafaga"], n)]
    return out


def calcular_espectros(ds: Dataset, W: int = 2048, solape: float = 0.5) -> Espectros:
    cfg = Config(W=W, solape=solape)
    fs = ds.fs()
    filas, mags, Hs, f0s, caps, ciclos = [], [], [], [], [], []
    for i, r in enumerate(ds.registros):
        V, _ = preprocesar_ventanas(segmentar(r.datos, W, solape), cfg)
        mag, freqs = espectro(V, cfg, fs)
        f0 = estimar_f0(mag, freqs, cfg.f_red)
        H = armonicas(mag, freqs, f0, N_ARMONICAS, 1)
        x = r.datos.astype(np.float64)
        ac = x - x.mean()
        rms_v = np.sqrt((V.astype(np.float64) ** 2).mean(axis=1))
        filas.append({
            "ruta": r.ruta, "etiqueta": "+".join(r.clases) or PISO_ETIQUETA, "tipo": r.tipo,
            "lugar": r.lugar, "sesion": r.sesion, "rafaga": r.rafaga, "n_ventanas": len(V),
            "duracion_s": len(x) / fs, "dc_mA": x.mean() * 1e3, "rms_A": ac.std(),
            "pico_A": np.abs(x).max(), "cresta": np.abs(ac).max() / max(ac.std(), 1e-9),
            "frac_riel": float(np.mean(np.abs(x) >= 0.99 * RIEL_A)),
            "h1_A": float(np.median(H[:, 0])),
            "thd": float(np.median(np.sqrt((H[:, 1:] ** 2).sum(axis=1)) / H[:, 0])),
            "cv_rms": float(rms_v.std() / rms_v.mean()), "f0_hz": float(np.median(f0)),
            "f0_min": float(f0.min()), "f0_max": float(f0.max()),
        })
        mags.append(mag); Hs.append(H); f0s.append(f0); caps.append(np.full(len(V), i))
        ciclos.append(_ciclo_promedio(ac, float(np.median(f0)), fs))
    tabla = pd.DataFrame(filas)
    tabla["rafaga"] = _separar_por_nivel(tabla)
    cap = np.concatenate(caps)

    # peso = 1 / (ventanas de la ráfaga · ráfagas de la etiqueta)
    v_raf = tabla.groupby("rafaga")["n_ventanas"].transform("sum").values
    n_raf = tabla.groupby("etiqueta")["rafaga"].transform("nunique").values
    peso = (1.0 / (v_raf * n_raf))[cap]
    return Espectros(tabla, freqs, np.concatenate(mags), np.concatenate(Hs), np.concatenate(f0s), cap,
                     peso.astype(np.float64), np.array(ciclos), W, fs)


def _cuantil_ponderado(M: np.ndarray, w: np.ndarray, q: float) -> np.ndarray:
    """Cuantil q por columna de M con pesos w por fila."""
    orden = np.argsort(M, axis=0)
    acum = np.cumsum(w[orden], axis=0)
    fila = np.argmax(acum >= q * acum[-1], axis=0)
    return np.take_along_axis(M, orden[fila, np.arange(M.shape[1])][None, :], axis=0)[0]


def piso(E: Espectros, q: float = 0.99) -> np.ndarray:
    """Amplitud por bin que `nada` no supera en una fracción q de sus ventanas."""
    s = E.ventanas(PISO_ETIQUETA)
    return _cuantil_ponderado(E.mag[s], E.peso[s], q)


def _potencia_media(E: Espectros, etiqueta: str) -> np.ndarray:
    s = E.ventanas(etiqueta)
    return np.average(E.mag[s].astype(np.float64) ** 2, axis=0, weights=E.peso[s])


def _malla(n: int, ncols: int, ancho: float, alto: float, **kw):
    filas = int(np.ceil(n / ncols))
    fig, axs = plt.subplots(filas, ncols, figsize=(ancho, alto * filas), squeeze=False, **kw)
    axs = axs.ravel()
    for a in axs[n:]:
        a.set_visible(False)
    return fig, axs


def _xticks_hz(ax, fmax: float) -> None:
    paso = 100 if fmax <= 600 else 500
    ax.set_xticks(np.arange(0, fmax + 1, paso))


# ------------------------------------------------------- inventario y calidad --

def inventario(E: Espectros) -> pd.DataFrame:
    """Capturas y ráfagas (ocasiones independientes) por clase y lugar."""
    t = E.tabla[E.tabla["tipo"] != TIPO_COMBINADA]
    filas = {}
    comb = E.tabla[E.tabla["tipo"] == TIPO_COMBINADA]["etiqueta"].str.split("+")
    for c, g in t.groupby("etiqueta"):
        f = {"capturas": len(g), "rafagas": g["rafaga"].nunique(), "lugares": g["lugar"].nunique(),
             "en_combinadas": int(comb.apply(lambda p: c in p).sum())}
        f.update({L: (g["lugar"] == L).sum() for L in sorted(E.tabla["lugar"].unique())})
        filas[c] = f
    return pd.DataFrame(filas).T.sort_values(["rafagas", "capturas"], ascending=False)


def combinadas_por_lugar(E: Espectros) -> pd.DataFrame:
    t = E.tabla[E.tabla["tipo"] == TIPO_COMBINADA]
    return t.pivot_table(index="etiqueta", columns="lugar", values="rms_A", aggfunc="size", fill_value=0)


def plot_inventario(E: Espectros):
    """Capturas contra ráfagas: el tamaño aparente y el tamaño efectivo del dataset."""
    d = inventario(E).sort_values("capturas")
    fig, ax = plt.subplots(figsize=(7.5, 0.3 * len(d) + 1))
    y = np.arange(len(d))
    ax.barh(y + 0.2, d["capturas"], 0.36, color="#86b6ef", label="capturas")
    ax.barh(y - 0.2, d["rafagas"], 0.36, color="#1c5cab", label="ráfagas (ocasiones independientes)")
    for yi, v in zip(y, d["rafagas"]):
        ax.text(v + 0.4, yi - 0.2, f"{v:d}", va="center", fontsize=8, color=TINTA2)
    ax.set_yticks(y, d.index)
    ax.grid(axis="x"); ax.set_xlabel("cantidad")
    ax.legend(loc="lower right")
    ax.set_title("Individuales: capturas y ráfagas por clase", loc="left")
    fig.tight_layout()
    return fig


def calidad(E: Espectros) -> dict:
    """Saturación, duración, offset por lugar y frecuencia de red por sesión."""
    t = E.tabla
    df_bin = E.fs / E.W
    r = t[t["h1_A"] > H1_MIN_F0_A]
    f0 = r.groupby("sesion").agg(f0_min=("f0_min", "min"), f0_mediana=("f0_hz", "median"), f0_max=("f0_max", "max"))
    desvio = (r["f0_hz"] - 50.0).abs().max()
    return {
        "capturas": len(t), "duracion_s": t["duracion_s"].value_counts().to_dict(),
        "max_abs_A": float(t["pico_A"].max()), "riel_A": RIEL_A,
        "con_muestras_en_riel": int((t["frac_riel"] > 0).sum()),
        "dc_mA_por_lugar": t.groupby("lugar")["dc_mA"].agg(["min", "median", "max"]).round(0),
        "f0_por_sesion": f0.round(3),
        "ancho_bin_hz": df_bin,
        "f0_captura_min_max": (float(r["f0_hz"].min()), float(r["f0_hz"].max())),
        "armonica_que_se_corre_un_bin": df_bin / desvio,
    }


def plot_calidad(E: Espectros):
    t = E.tabla
    fig, ax = plt.subplots(1, 2, figsize=(11, 3.2))
    rng = np.random.default_rng(0)
    lugares = sorted(t["lugar"].unique())
    for i, L in enumerate(lugares):
        v = t.loc[t["lugar"] == L, "dc_mA"]
        ax[0].scatter(v, i + rng.uniform(-0.18, 0.18, len(v)), s=10, color=COLOR_LUGAR.get(L, (TINTA2,))[0], alpha=0.6, lw=0)
    ax[0].set_yticks(range(len(lugares)), lugares)
    ax[0].axvline(0, color=EJE, lw=0.8); ax[0].grid(axis="x")
    ax[0].set(xlabel="media de la captura [mA]", title="Offset de continua por lugar")
    ses = sorted(t["sesion"].unique())
    r = t[t["h1_A"] > H1_MIN_F0_A]
    for i, S in enumerate(ses):
        g = r[r["sesion"] == S]
        ax[1].plot([g["f0_min"].min(), g["f0_max"].max()], [i, i], color="#86b6ef", lw=3, solid_capstyle="round")
        ax[1].scatter(g["f0_hz"], np.full(len(g), i), s=9, color="#1c5cab", lw=0)
    ax[1].set_yticks(range(len(ses)), [s.replace("_2026", " ") for s in ses])
    ax[1].axvline(50, color=EJE, lw=0.8); ax[1].grid(axis="x")
    ax[1].set(xlabel="f0 estimada [Hz]", title="Frecuencia de red por sesión (barra = rango, punto = captura)")
    fig.tight_layout()
    return fig


def atipicas(E: Espectros, umbral: float = 3.5) -> pd.DataFrame:
    """Capturas cuya corriente se aleja de su propia ráfaga (z robusto sobre log RMS), o sea
    de capturas tomadas segundos antes o después. Son para revisar a mano: una carga con
    varios estados legítimos también aparece. `nada` no se evalúa (está al nivel de 1 LSB)."""
    t = E.tabla[(E.tabla["tipo"] != TIPO_COMBINADA) & (E.tabla["etiqueta"] != PISO_ETIQUETA)].copy()
    t["log"] = np.log10(t["rms_A"])

    def z(g):
        mad = max(1.4826 * (g - g.median()).abs().median(), 0.02)   # 0.02 décadas ≈ 5 %
        return (g - g.median()) / mad

    t["z_clase"] = t.groupby("etiqueta")["log"].transform(z)
    t["z_rafaga"] = t.groupby("rafaga")["log"].transform(z)
    t["rms_mediana_clase"] = t.groupby("etiqueta")["rms_A"].transform("median")
    s = t[t["z_rafaga"].abs() > umbral]
    return (s[["ruta", "lugar", "rms_A", "rms_mediana_clase", "z_clase", "z_rafaga", "cv_rms"]]
            .sort_values("z_rafaga", key=np.abs, ascending=False).round(3).reset_index(drop=True))


# ----------------------------------------------------- dominio del tiempo --

def plot_ciclos(E: Espectros, clases=None, ncols: int = 4):
    """Ciclo promedio de cada captura, alineado a la fundamental; cada línea es una captura."""
    clases = clases or E.clases()
    t = E.tabla
    fig, axs = _malla(len(clases), ncols, 13, 2.1)
    ms = np.linspace(0, 20, E.ciclos.shape[1], endpoint=False)
    lugares = set()
    for ax, c in zip(axs, clases):
        for i in np.where(t["etiqueta"].values == c)[0]:
            L = t["lugar"].iat[i]
            col, ls = COLOR_LUGAR.get(L, (TINTA2, "-"))
            ax.plot(ms, E.ciclos[i], color=col, ls=ls, lw=0.8, alpha=0.7)
            lugares.add(L)
        ax.axhline(0, color=EJE, lw=0.6)
        ax.set_title(c, loc="left")
        ax.set_xticks([0, 10, 20])
    for ax in axs[:: ncols]:
        ax.set_ylabel("I [A]")
    for ax in axs[-ncols:]:
        ax.set_xlabel("t [ms]")
    _leyenda_lugares(fig, lugares)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return fig


def _leyenda_lugares(fig, lugares) -> None:
    from matplotlib.lines import Line2D
    h = [Line2D([], [], color=COLOR_LUGAR.get(L, (TINTA2, "-"))[0], ls=COLOR_LUGAR.get(L, (TINTA2, "-"))[1], label=L)
         for L in sorted(lugares)]
    fig.legend(handles=h, loc="upper right", ncols=len(h))


def plot_descriptores(E: Espectros, clases=None):
    """RMS, THD y factor de cresta por captura: tres números que ya separan familias de cargas."""
    clases = clases or E.clases()
    t = E.tabla
    fig, axs = plt.subplots(1, 3, figsize=(13, 0.3 * len(clases) + 1.1), sharey=True)
    rng = np.random.default_rng(0)
    y = {c: len(clases) - 1 - i for i, c in enumerate(clases)}
    for ax, (col, nombre, log) in zip(axs, (("rms_A", "RMS [A]", True), ("thd", "THD (hasta 3 kHz)", True),
                                            ("cresta", "factor de cresta", False))):
        for c in clases:
            v = t.loc[t["etiqueta"] == c, col].values
            ax.scatter(v, y[c] + rng.uniform(-0.2, 0.2, len(v)), s=11, color="#2a78d6", alpha=0.55, lw=0)
            ax.plot([np.median(v)] * 2, [y[c] - 0.34, y[c] + 0.34], color=TINTA, lw=1.5)
        if log:
            ax.set_xscale("log")
        ax.grid(axis="x"); ax.set_xlabel(nombre)
    axs[0].set_yticks(list(y.values()), list(y.keys()))
    fig.suptitle("Descriptores por captura (punto = captura, raya = mediana)", x=0.01, ha="left", fontsize=10)
    fig.tight_layout()
    return fig


# ------------------------------------------------------- firmas acumuladas --

def plot_firmas_acumuladas(E: Espectros, clases=None, fmax: float = 3000.0, ncols: int = 2,
                           amp_lim=(1e-4, 30.0), n_amp: int = 48):
    """Firma acumulada: superpone los espectros de TODAS las ventanas de cada clase.
    Color = fracción de ventanas de la clase con esa amplitud en esa frecuencia
    (cada ráfaga pesa igual). Línea oscura = mediana; gris = piso de `nada` (P99)."""
    clases = clases or E.clases()
    b = np.where(E.freqs <= fmax)[0][1:]
    df = E.fs / E.W
    bordes_f = np.append(E.freqs[b] - df / 2, E.freqs[b[-1]] + df / 2)
    bordes_a = np.logspace(np.log10(amp_lim[0]), np.log10(amp_lim[1]), n_amp + 1)
    p = piso(E)[b] if PISO_ETIQUETA in E.tabla["etiqueta"].values else None
    fig, axs = _malla(len(clases), ncols, 13.5, 2.5, sharex=True, sharey=True)
    norma = PowerNorm(0.45, vmin=0, vmax=1)
    for ax, c in zip(axs, clases):
        s = E.ventanas(c)
        M, w = E.mag[s][:, b], E.peso[s]
        D = np.zeros((n_amp, len(b)))
        for j in range(len(b)):
            D[:, j] = np.histogram(M[:, j], bordes_a, weights=w)[0]
        D /= np.maximum(D.sum(axis=0, keepdims=True), 1e-12)
        pc = ax.pcolormesh(bordes_f, bordes_a, D, cmap=SECUENCIAL, norm=norma, rasterized=True, shading="flat")
        if p is not None and c != PISO_ETIQUETA:
            ax.plot(E.freqs[b], p, color=APAGADO, lw=0.6)
        ax.plot(E.freqs[b], _cuantil_ponderado(M, w, 0.5), color=TINTA, lw=0.5)
        ax.set_yscale("log"); ax.set_ylim(*amp_lim)
        n_cap = E.tabla.loc[E.tabla["etiqueta"] == c]
        n_r = n_cap["rafaga"].nunique()
        ax.set_title(f"{c}  ({len(n_cap)} capturas, {n_r} {'ráfaga' if n_r == 1 else 'ráfagas'})", loc="left")
        _xticks_hz(ax, fmax)
    for ax in axs[:: ncols]:
        ax.set_ylabel("|I| [A]")
    for ax in axs[max(0, len(clases) - ncols):len(clases)]:
        ax.tick_params(labelbottom=True); ax.set_xlabel("f [Hz]")
    from matplotlib.lines import Line2D
    fig.legend(handles=[Line2D([], [], color=TINTA, lw=1, label="mediana de la clase"),
                        Line2D([], [], color=APAGADO, lw=1, label="piso: P99 de nada")],
               loc="upper left", ncols=2)
    fig.tight_layout(rect=(0, 0, 0.93, 0.98))
    cax = fig.add_axes([0.945, 0.3, 0.012, 0.4])
    fig.colorbar(pc, cax=cax, label="fracción de ventanas")
    return fig


def perfil_armonico(E: Espectros, etiqueta: str, lugar: str | None = None, q: float = 0.5,
                    relativo: bool = False) -> np.ndarray:
    """Cuantil q de |H_k| (o |H_k|/|H_1|) sobre las ventanas de una clase."""
    s = E.ventanas(etiqueta)
    if lugar is not None:
        s &= E.tabla["lugar"].values[E.cap] == lugar
    H = E.H[s] / E.H[s][:, :1] if relativo else E.H[s]
    return _cuantil_ponderado(H, E.peso[s], q)


def plot_firmas_armonicas(E: Espectros, clases=None, ncols: int = 3, kmax: int = 40, relativo: bool = False):
    """Firma por armónica y por lugar: mediana y banda P10-P90. Es la pregunta de una
    casa nunca vista: ¿la misma clase tiene la misma firma en otro lugar?"""
    clases = clases or E.clases(con_piso=False)
    t = E.tabla
    k = np.arange(1, kmax + 1)
    fig, axs = _malla(len(clases), ncols, 13.5, 2.5, sharex=True, sharey=True)
    lugares = set()
    p_h = np.median(armonicas(E.mag[E.ventanas(PISO_ETIQUETA)], E.freqs, E.f0[E.ventanas(PISO_ETIQUETA)], kmax, 1), axis=0)
    for ax, c in zip(axs, clases):
        for L in sorted(t.loc[t["etiqueta"] == c, "lugar"].unique()):
            col, ls = COLOR_LUGAR.get(L, (TINTA2, "-"))
            ax.fill_between(k, perfil_armonico(E, c, L, 0.1, relativo)[:kmax], perfil_armonico(E, c, L, 0.9, relativo)[:kmax],
                            color=col, alpha=0.16, lw=0)
            ax.plot(k, perfil_armonico(E, c, L, 0.5, relativo)[:kmax], color=col, ls=ls, lw=1.1)
            lugares.add(L)
        if not relativo:
            ax.plot(k, p_h, color=APAGADO, lw=0.8)
        ax.set_yscale("log"); ax.grid(axis="y")
        ax.set_title(c, loc="left")
    for ax in axs[:: ncols]:
        ax.set_ylabel("|H_k|/|H_1|" if relativo else "|H_k| [A]")
    for ax in axs[max(0, len(clases) - ncols):len(clases)]:
        ax.tick_params(labelbottom=True); ax.set_xlabel("armónica k (f ≈ k·50 Hz)")
    _leyenda_lugares(fig, lugares)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


# ----------------------------------------------- energía acumulada y franjas --

def energia_acumulada(E: Espectros, clases=None, fmax: float = 5000.0) -> tuple:
    """Potencia media sobre el piso, acumulada con f. Devuelve (tabla, freqs, curvas sin fundamental)."""
    clases = clases or E.clases(con_piso=False)
    P0 = _potencia_media(E, PISO_ETIQUETA)
    fr = E.freqs
    fund = fr < 75
    b = fr <= fmax
    filas, curvas = [], []
    for c in clases:
        P = np.clip(_potencia_media(E, c) - P0, 0, None) * b
        sin_f = P * ~fund
        acum = np.cumsum(sin_f) / max(sin_f.sum(), 1e-30)
        curvas.append(acum[b])
        filas.append({"clase": c, "fundamental_%": 100 * P[fund].sum() / P.sum(),
                      "resto_mA": 1e3 * np.sqrt(sin_f.sum() / 2),
                      "f50_hz": fr[np.searchsorted(acum, 0.5)], "f90_hz": fr[np.searchsorted(acum, 0.9)],
                      "f99_hz": fr[np.searchsorted(acum, 0.99)]})
    return pd.DataFrame(filas).set_index("clase"), fr[b], np.array(curvas)


def plot_energia_acumulada(E: Espectros, clases=None, fmax: float = 3000.0):
    """Para cada clase, qué fracción de la potencia fuera de la fundamental ya se juntó
    al llegar a cada frecuencia. Marcas: dónde se alcanza el 90 % y el 99 %."""
    tab, fr, C = energia_acumulada(E, clases)
    b = fr <= fmax
    fig, ax = plt.subplots(figsize=(11, 0.32 * len(tab) + 1.2))
    pc = ax.pcolormesh(fr[b], np.arange(len(tab)), C[:, b], cmap=SECUENCIAL, vmin=0, vmax=1, rasterized=True, shading="nearest")
    ax.scatter(tab["f90_hz"], np.arange(len(tab)), s=34, color=SUPERFICIE, edgecolor=TINTA, lw=1, marker="o", label="90 %", zorder=3)
    ax.scatter(np.minimum(tab["f99_hz"], fmax), np.arange(len(tab)), s=38, color=TINTA, edgecolor=SUPERFICIE, lw=1, marker="D", label="99 %", zorder=3)
    ax.set_yticks(np.arange(len(tab)), tab.index); ax.invert_yaxis()
    ax.set(xlabel="f [Hz]", title="Potencia acumulada fuera de la fundamental")
    ax.title.set_ha("left"); ax.title.set_x(0)
    ax.legend(loc="lower right", ncols=2, facecolor=SUPERFICIE, frameon=True, edgecolor="none")
    fig.colorbar(pc, ax=ax, label="fracción acumulada", fraction=0.025, pad=0.015)
    fig.tight_layout()
    return fig


def snr_por_banda(E: Espectros, clases=None, ancho: float = 100.0, fmax: float = 5000.0) -> pd.DataFrame:
    """Potencia de cada clase sobre el piso en bandas fijas [dB]."""
    clases = clases or E.clases(con_piso=False)
    P0 = _potencia_media(E, PISO_ETIQUETA)
    bordes = np.arange(0, fmax + ancho, ancho)
    d = np.digitize(E.freqs, bordes) - 1
    filas = {}
    for c in clases:
        P = _potencia_media(E, c)
        filas[c] = [10 * np.log10(P[d == i].sum() / P0[d == i].sum()) for i in range(len(bordes) - 1)]
    return pd.DataFrame(filas, index=bordes[:-1]).T


def plot_snr_por_banda(E: Espectros, clases=None, ancho: float = 100.0, fmax: float = 5000.0, vmax: float = 60.0):
    S = snr_por_banda(E, clases, ancho, fmax)
    fig, ax = plt.subplots(figsize=(13, 0.32 * len(S) + 1.2))
    bordes = np.append(S.columns.values, S.columns.values[-1] + ancho)
    pc = ax.pcolormesh(bordes, np.arange(len(S) + 1), S.values, cmap=SECUENCIAL, vmin=0, vmax=vmax, rasterized=True)
    ax.set_yticks(np.arange(len(S)) + 0.5, S.index); ax.invert_yaxis()
    ax.axvline(2200, color=TINTA2, lw=0.8)
    ax.text(2220, -0.15, "corte anti-alias 2.2 kHz", fontsize=8, color=TINTA2, va="bottom")
    ax.set_xlabel("f [Hz]")
    ax.set_title(f"Potencia sobre el piso de ruido por banda de {ancho:.0f} Hz", loc="left", pad=14)
    fig.colorbar(pc, ax=ax, label="dB sobre nada", fraction=0.025, pad=0.015, extend="max")
    fig.tight_layout()
    return fig


def plot_armonicas_heatmap(E: Espectros, clases=None, kmax: int = 40, vmin: float = -80.0):
    """Clase × armónica, en valor absoluto y relativo a la fundamental."""
    clases = clases or E.clases(con_piso=False)
    Hs = np.array([perfil_armonico(E, c)[:kmax] for c in clases])
    fig, axs = plt.subplots(2, 1, figsize=(12, 0.56 * len(clases) + 2), sharex=True)
    k = np.arange(1, kmax + 1)
    y = np.arange(len(clases))
    for ax, D, tit, lab, lim in ((axs[0], 20 * np.log10(Hs), "Amplitud absoluta de cada armónica", "dB re 1 A", (-70, 25)),
                                 (axs[1], 20 * np.log10(Hs / Hs[:, :1]), "Forma: amplitud relativa a la fundamental", "dB re H1", (vmin, 0))):
        pc = ax.pcolormesh(k, y, D, cmap=SECUENCIAL, vmin=lim[0], vmax=lim[1], rasterized=True, shading="nearest")
        ax.set_yticks(y, clases); ax.invert_yaxis()
        ax.set_title(tit, loc="left")
        fig.colorbar(pc, ax=ax, label=lab, fraction=0.025, pad=0.015)
    axs[1].set_xticks(np.arange(1, kmax + 1, 2))
    axs[1].set_xlabel("armónica k (f ≈ k·50 Hz); ejes marcados en las impares")
    fig.tight_layout()
    return fig


def energia_interarmonica(E: Espectros, clases=None, tol_bins: int = 2, fmax: float = 3000.0) -> pd.Series:
    """% de la potencia sobre el piso (sin fundamental) que cae FUERA de los lóbulos
    de las armónicas de 50 Hz. Alto = hay contenido que no es múltiplo de la red."""
    clases = clases or E.clases(con_piso=False)
    P0 = _potencia_media(E, PISO_ETIQUETA)
    df = E.fs / E.W
    k = np.round(E.freqs / 50.0)
    en_arm = np.abs(E.freqs - k * 50.0) <= (tol_bins + 0.5) * df
    b = (E.freqs >= 75) & (E.freqs <= fmax)
    out = {}
    for c in clases:
        P = np.clip(_potencia_media(E, c) - P0, 0, None)
        out[c] = 100 * P[b & ~en_arm].sum() / max(P[b].sum(), 1e-30)
    return pd.Series(out, name="interarmonica_%").round(1)


# --------------------------------------------- qué frecuencias separan clases --

def _auc(pos: np.ndarray, neg: np.ndarray) -> np.ndarray:
    """AUC de Mann-Whitney por columna: P(positivo > negativo). 0.5 = no separa."""
    R = rankdata(np.vstack([pos, neg]), axis=0)
    n_p, n_n = len(pos), len(neg)
    return (R[:n_p].sum(axis=0) - n_p * (n_p + 1) / 2) / (n_p * n_n)


def _por_rafaga(E: Espectros, tipo: str, relativo: bool, agrupar: dict) -> tuple:
    """Mediana de log|H_k| por ráfaga. Devuelve (matriz, etiquetas como conjuntos, lugares)."""
    t = E.tabla
    fila = t["tipo"].values[E.cap] == tipo
    filas, etiquetas, lugares = [], [], []
    for raf, g in t[t["tipo"] == tipo].groupby("rafaga"):
        s = np.isin(E.cap, g.index) & fila
        H = E.H[s]
        H = H / H[:, :1] if relativo else H
        filas.append(np.median(np.log10(H + 1e-5), axis=0))
        etiquetas.append({agrupar.get(c, c) for c in g["etiqueta"].iat[0].split("+")})
        lugares.append(g["lugar"].iat[0])
    return np.array(filas), etiquetas, np.array(lugares)


def _auc_clase(X: np.ndarray, tiene: np.ndarray, lug: np.ndarray, bloque: str, min_rafagas: int):
    """AUC por armónica de una clase. En combinadas se compara dentro de cada lugar y solo
    donde hay al menos `min_rafagas` ráfagas de cada lado, para no confundir clase con casa."""
    if bloque == "ind":
        return _auc(X[tiene], X[~tiene]) if tiene.sum() >= min_rafagas and (~tiene).sum() >= min_rafagas else None
    suma, peso = 0.0, 0.0
    for L in np.unique(lug):
        p, n = tiene & (lug == L), ~tiene & (lug == L)
        if p.sum() >= min_rafagas and n.sum() >= min_rafagas:
            w = min(p.sum(), n.sum())
            suma, peso = suma + w * _auc(X[p], X[n]), peso + w
    return suma / peso if peso else None


def auc_armonicas(E: Espectros, agrupar: dict | None = None, bloque: str = "ind", relativo: bool = False,
                  excluir=("nada",), min_rafagas: int = 2, n_perm: int = 500, seed: int = 0) -> tuple:
    """Separabilidad clase-contra-resto por armónica, con ráfagas como unidad (no ventanas).
    AUC = P(una ráfaga de la clase tiene más de esa armónica que una del resto); 0.5 = no separa.

    Con tan pocas ráfagas un AUC alto puede salir por azar, y además se miran muchas
    armónicas a la vez. El umbral sale de permutar las etiquetas (dentro de cada lugar en
    combinadas): es el |AUC − 0.5| máximo, sobre TODAS las armónicas, que el azar supera solo
    5 % de las veces. Devuelve (tabla de AUC, umbral por clase, ráfagas por clase)."""
    agrupar = agrupar or {}
    tipo = TIPO_INDIVIDUAL if bloque == "ind" else TIPO_COMBINADA
    X, etiq, lug = _por_rafaga(E, tipo, relativo, agrupar)
    rng = np.random.default_rng(seed)
    clases = sorted({c for e in etiq for c in e} - set(excluir))
    tabla, umbral, n = {}, {}, {}
    for c in clases:
        tiene = np.array([c in e for e in etiq])
        auc = _auc_clase(X, tiene, lug, bloque, min_rafagas)
        if auc is None:
            continue
        maximos = []
        for _ in range(n_perm):
            perm = tiene.copy()
            if bloque == "ind":
                perm = rng.permutation(perm)
            else:
                for L in np.unique(lug):
                    perm[lug == L] = rng.permutation(perm[lug == L])
            a = _auc_clase(X, perm, lug, bloque, min_rafagas)
            if a is not None:
                maximos.append(np.abs(a - 0.5).max())
        tabla[c], umbral[c], n[c] = auc, np.percentile(maximos, 95), f"{tiene.sum()}/{(~tiene).sum()}"
    return (pd.DataFrame(tabla, index=np.arange(1, N_ARMONICAS + 1)).T, pd.Series(umbral), pd.Series(n))


def plot_auc(E: Espectros, agrupar: dict | None = None, kmax: int = 40):
    """Rojo: la clase tiene MÁS de esa armónica que el resto. Azul: MENOS. Gris: no separa.
    Punto negro: supera el umbral de permutación (no se explica por azar)."""
    paneles = (("Individuales, amplitud absoluta", "ind", False), ("Individuales, forma (relativa a H1)", "ind", True),
               ("Combinadas reales, amplitud absoluta (dentro de cada lugar)", "comb", False))
    res = [auc_armonicas(E, agrupar, b, r) for _, b, r in paneles]
    fig, axs = plt.subplots(3, 1, figsize=(12, sum(0.32 * len(r[0]) + 0.8 for r in res)), sharex=True,
                            height_ratios=[len(r[0]) + 2 for r in res])
    k = np.arange(1, kmax + 1)
    for ax, (A, U, N), (tit, _, _) in zip(axs, res, paneles):
        pc = ax.pcolormesh(k, np.arange(len(A)), A.values[:, :kmax], cmap=DIVERGENTE, vmin=0, vmax=1, rasterized=True, shading="nearest")
        fil, col = np.where(np.abs(A.values[:, :kmax] - 0.5) >= U.reindex(A.index).values[:, None])
        ax.scatter(k[col], fil, s=9, color=TINTA, lw=0)
        ax.set_yticks(np.arange(len(A)), [f"{c} ({N[c]})" for c in A.index]); ax.invert_yaxis()
        ax.set_title(tit + "  [ráfagas clase/resto]", loc="left")
        fig.colorbar(pc, ax=ax, label="AUC", fraction=0.025, pad=0.015)
    axs[-1].set_xticks(np.arange(1, kmax + 1, 2)); axs[-1].set_xlabel("armónica k")
    fig.tight_layout()
    return fig


def armonicas_significativas(E: Espectros, agrupar: dict | None = None) -> pd.DataFrame:
    """Armónicas que superan el umbral de permutación, por clase. '+k': la clase tiene más que
    el resto; '-k': tiene menos. Hasta 3 kHz, porque N_ARMONICAS = 60."""
    salida = {}
    for nombre, b, r in (("individual_absoluta", "ind", False), ("individual_forma", "ind", True),
                         ("combinada_absoluta", "comb", False)):
        A, U, _ = auc_armonicas(E, agrupar, b, r)
        col = {}
        for c, f in A.iterrows():
            d = f - 0.5
            k = d[d.abs() >= U[c]].sort_values(key=np.abs, ascending=False).index
            col[c] = " ".join(f"{'+' if d[i] > 0 else '-'}{i}" for i in sorted(k)) or "—"
        salida[nombre] = col
    return pd.DataFrame(salida).fillna("sin datos")


# ------------------------------------------------- parecidos y redundancia --

def distancias_clases(E: Espectros, clases=None, kmax: int = 40, relativo: bool = False) -> pd.DataFrame:
    """Diferencia media (dB, RMS sobre armónicas) entre firmas medianas de cada par de clases."""
    clases = clases or E.clases(con_piso=False)
    F = np.array([20 * np.log10(perfil_armonico(E, c, relativo=relativo)[:kmax] + 1e-5) for c in clases])
    D = np.sqrt(((F[:, None] - F[None]) ** 2).mean(axis=2))
    return pd.DataFrame(D, index=clases, columns=clases)


def plot_distancias(E: Espectros, clases=None, kmax: int = 40):
    fig, axs = plt.subplots(1, 2, figsize=(12.5, 5))
    for ax, rel, tit in ((axs[0], False, "Amplitud absoluta"), (axs[1], True, "Solo forma (relativa a H1)")):
        D = distancias_clases(E, clases, kmax, rel)
        o = leaves_list(linkage(squareform(D.values, checks=False), "average"))
        D = D.iloc[o, o]
        pc = ax.imshow(D.values, cmap=SECUENCIAL.reversed(), vmin=0, vmax=40)
        ax.set_xticks(range(len(D)), D.index, rotation=60, ha="right"); ax.set_yticks(range(len(D)), D.index)
        ax.spines[:].set_visible(False); ax.tick_params(length=0)
        for i in range(len(D)):
            for j in range(len(D)):
                v = D.values[i, j]
                if i != j and v < 12:
                    ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=7.5, color="white")
        ax.set_title(tit, loc="left")
        fig.colorbar(pc, ax=ax, label="diferencia media [dB]", fraction=0.046, pad=0.03)
    fig.tight_layout()
    return fig


def variacion_entre_lugares(E: Espectros, clases=None, kmax: int = 40) -> pd.DataFrame:
    """¿Cuánto cambia una clase entre lugares comparado con lo que la separa de otra clase?
    Diferencia de forma y de amplitud en dB, sobre armónicas donde alguna firma supera el piso."""
    clases = clases or E.clases(con_piso=False)
    t = E.tabla
    D = distancias_clases(E, clases, kmax, relativo=True)
    filas = []
    for c in clases:
        L = sorted(t.loc[t["etiqueta"] == c, "lugar"].unique())
        vecina = D.loc[c].drop(c)
        f = {"clase": c, "lugares": len(L), "clase_mas_parecida": vecina.idxmin(), "dist_vecina_dB": vecina.min()}
        if len(L) > 1:
            P = np.array([20 * np.log10(perfil_armonico(E, c, l, relativo=True)[:kmax] + 1e-5) for l in L])
            A = [perfil_armonico(E, c, l)[0] for l in L]
            f["dist_entre_lugares_dB"] = np.sqrt(((P[:, None] - P[None]) ** 2).mean(axis=2)).max()
            f["h1_min_A"], f["h1_max_A"] = min(A), max(A)
        filas.append(f)
    return pd.DataFrame(filas).set_index("clase").round(2)


def plot_correlacion(E: Espectros, kmax: int = 30):
    """Correlación de Spearman entre armónicas (log), por ráfaga de individuales."""
    X, _, _ = _por_rafaga(E, TIPO_INDIVIDUAL, False, {})
    Xr, _, _ = _por_rafaga(E, TIPO_INDIVIDUAL, True, {})
    fig, axs = plt.subplots(1, 2, figsize=(12, 4.8))
    for ax, M, tit in ((axs[0], X, "Amplitud absoluta"), (axs[1], Xr[:, 1:], "Forma (relativa a H1, desde H2)")):
        desde = 1 if M is X else 2
        M = M[:, : kmax - desde + 1]
        C = spearmanr(M).statistic
        pc = ax.imshow(C, cmap=DIVERGENTE, vmin=-1, vmax=1, extent=(desde - 0.5, kmax + 0.5, kmax + 0.5, desde - 0.5))
        ax.spines[:].set_visible(False)
        ax.set(xlabel="armónica k", ylabel="armónica k"); ax.set_title(tit, loc="left")
        fig.colorbar(pc, ax=ax, label="Spearman", fraction=0.046, pad=0.03)
    fig.tight_layout()
    return fig


def _pca(X: np.ndarray):
    Xc = X - X.mean(axis=0)
    U, s, Vt = np.linalg.svd(Xc, full_matrices=False)
    return U * s, s ** 2 / (s ** 2).sum(), Vt


def _log_espectro_por_rafaga(E: Espectros, fmax: float, agrupar: dict):
    t = E.tabla
    b = (E.freqs > 0) & (E.freqs <= fmax)
    filas, etiq = [], []
    for raf, g in t[t["tipo"] == TIPO_INDIVIDUAL].groupby("rafaga"):
        filas.append(np.median(np.log10(E.mag[np.isin(E.cap, g.index)][:, b] + 2e-4), axis=0))
        c = g["etiqueta"].iat[0]
        etiq.append(agrupar.get(c, c))
    return np.array(filas), np.array(etiq)


def pca_resumen(E: Espectros, fmax: float = 3000.0) -> pd.DataFrame:
    """Componentes necesarias para explicar 90/95/99 % de la varianza (ráfagas, log magnitud)."""
    X, _ = _log_espectro_por_rafaga(E, fmax, {})
    _, ev, _ = _pca(X)
    acum = np.cumsum(ev)
    return pd.DataFrame({"bins_originales": X.shape[1], "rafagas": X.shape[0],
                         **{f"comp_{int(p * 100)}%": int(np.searchsorted(acum, p) + 1) for p in (0.9, 0.95, 0.99)}}, index=["pca"])


def plot_pca(E: Espectros, agrupar: dict | None = None, fmax: float = 3000.0, ncols: int = 4):
    """Cada punto es una ráfaga. Un panel por clase, para no pedirle al color más de lo que puede."""
    X, etiq = _log_espectro_por_rafaga(E, fmax, agrupar or {})
    Z, ev, Vt = _pca(X)
    clases = sorted(set(etiq))
    fig, axs = _malla(len(clases) + 1, ncols, 13.5, 2.9)
    axs[0].plot(np.arange(1, 16), np.cumsum(ev)[:15] * 100, color="#2a78d6", marker="o", ms=4)
    axs[0].set(xlabel="componentes", ylabel="varianza acumulada [%]", ylim=(0, 102))
    axs[0].grid(axis="y"); axs[0].set_title("Varianza explicada", loc="left")
    for ax, c in zip(axs[1:], clases):
        s = etiq == c
        ax.scatter(Z[~s, 0], Z[~s, 1], s=14, color=EJE, lw=0)
        ax.scatter(Z[s, 0], Z[s, 1], s=34, color="#eb6834", edgecolor=SUPERFICIE, lw=1)
        ax.set_title(f"{c} ({s.sum()})", loc="left"); ax.set_xticks([]); ax.set_yticks([])
        ax.set_xlabel(f"PC1 ({ev[0] * 100:.0f} %)"); ax.set_ylabel(f"PC2 ({ev[1] * 100:.0f} %)")
    fig.tight_layout()
    return fig


def plot_pca_cargas(E: Espectros, fmax: float = 3000.0, ver_hasta: float = 1000.0, n: int = 3):
    """Qué frecuencias pesa cada componente: lectura física de los ejes de PCA."""
    X, _ = _log_espectro_por_rafaga(E, fmax, {})
    _, ev, Vt = _pca(X)
    b = (E.freqs > 0) & (E.freqs <= fmax)
    fig, axs = plt.subplots(n, 1, figsize=(12, 1.6 * n), sharex=True)
    for i, ax in enumerate(axs):
        ax.plot(E.freqs[b], Vt[i], color="#2a78d6", lw=0.8)
        ax.axhline(0, color=EJE, lw=0.6); ax.set_xlim(0, ver_hasta)
        ax.set_xticks(np.arange(0, ver_hasta + 1, 100)); ax.grid(axis="x")
        ax.set_ylabel("carga"); ax.set_title(f"PC{i + 1} ({ev[i] * 100:.0f} % de la varianza)", loc="left")
    axs[-1].set_xlabel("f [Hz]")
    fig.tight_layout()
    return fig


# ---------------------------------------------- distribución dentro de cada clase --

UMBRAL_NIVEL_DB = 6.0       # el doble (o la mitad) de corriente
UMBRAL_FORMA_DB = 8.0       # ~percentil 95 de lo que difiere una captura del resto de su ráfaga
MARCADOR_LUGAR = {"casa": "o", "depaleo": "o", "depamica": "o", "local": "s"}


def _vectores_db(E: Espectros, kmax: int = 40) -> tuple:
    """Firma de cada captura: mediana de |H_k| en dB, recortada al piso de `nada`, para que
    las armónicas que nadie puede distinguir del ruido no cuenten como diferencias."""
    s = E.ventanas(PISO_ETIQUETA)
    p = _cuantil_ponderado(E.H[s][:, :kmax], E.peso[s], 0.99)
    A = np.array([20 * np.log10(np.maximum(np.median(E.H[E.cap == i][:, :kmax], axis=0), p))
                  for i in range(len(E.tabla))])
    return A, 20 * np.log10(p)


def _comparar(a: np.ndarray, b: np.ndarray, p: np.ndarray) -> tuple:
    """(nivel, forma, total) de a respecto de b, en dB.
    nivel = diferencia en H1 (consume más o menos). forma = RMS de la diferencia de H_k/H1
    sobre las armónicas que superan el piso en alguna de las dos (tiene otra firma)."""
    m = (a > p + 1e-6) | (b > p + 1e-6)
    m[0] = False
    forma = np.sqrt(np.mean(((a - a[0]) - (b - b[0]))[m] ** 2)) if m.any() else 0.0
    return a[0] - b[0], forma, np.sqrt(np.mean((a - b) ** 2))


def _centro(A: np.ndarray, t: pd.DataFrame) -> np.ndarray:
    """Mediana de las medianas por ráfaga: cada ocasión pesa igual."""
    return np.median([np.median(A[g.index], axis=0) for _, g in t.groupby("rafaga")], axis=0)


def distribucion_muestras(E: Espectros, agrupar: dict | None = None, bloque: str = "ind",
                          min_capturas: int = 3, kmax: int = 40) -> pd.DataFrame:
    """Una fila por captura: cómo se ubica dentro de su categoría.

    *_clase: contra el resto de la clase, SIN su propia ráfaga (si hay otra con qué comparar).
    *_rafaga: contra las demás capturas de su ráfaga, tomadas segundos antes o después.
    clase_vecina: la otra clase más cercana; `mas_cerca_de_otra` indica si queda más cerca de
    ella que de la propia. Solo en individuales; las clases de un mismo grupo no compiten."""
    agrupar = agrupar or {}
    A, p = _vectores_db(E, kmax)
    t = E.tabla
    tipo = t["tipo"] == TIPO_COMBINADA
    if bloque == "ind":
        base, etiquetas = t[~tipo], E.clases(min_capturas, con_piso=False)
    else:
        base = t[tipo]
        n = base["etiqueta"].value_counts()
        etiquetas = list(n[n >= min_capturas].index)
    centros = {c: _centro(A, base[base["etiqueta"] == c]) for c in etiquetas}
    filas = []
    for c in etiquetas:
        g = base[base["etiqueta"] == c]
        for i, f in g.iterrows():
            resto = g[g["rafaga"] != f["rafaga"]]
            ref = resto if len(resto) else g
            nivel, forma, total = _comparar(A[i], _centro(A, ref), p)
            propia = g[(g["rafaga"] == f["rafaga"]) & (g.index != i)]
            n_r, f_r = (_comparar(A[i], np.median(A[propia.index], axis=0), p)[:2] if len(propia) else (np.nan, np.nan))
            fila = {"ruta": f["ruta"], "etiqueta": c, "lugar": f["lugar"], "rafaga": f["rafaga"], "rms_A": f["rms_A"],
                    "nivel_clase_dB": nivel, "forma_clase_dB": forma, "dist_propia_dB": total,
                    "nivel_rafaga_dB": n_r, "forma_rafaga_dB": f_r, "clase_vecina": None, "dist_vecina_dB": np.nan}
            if bloque == "ind":
                otras = {k: _comparar(A[i], centros[k], p)[2] for k in etiquetas
                         if agrupar.get(k, k) != agrupar.get(c, c)}
                if otras:
                    fila["clase_vecina"] = min(otras, key=otras.get)
                    fila["dist_vecina_dB"] = otras[fila["clase_vecina"]]
            filas.append(pd.Series(fila, name=i))
    D = pd.DataFrame(filas)
    D["cambia_en_rafaga"] = (D["nivel_rafaga_dB"].abs() >= UMBRAL_NIVEL_DB) | (D["forma_rafaga_dB"] >= UMBRAL_FORMA_DB)
    D["mas_cerca_de_otra"] = D["dist_vecina_dB"] < D["dist_propia_dB"]
    marcada = D["cambia_en_rafaga"] | D["mas_cerca_de_otra"]
    D["id"] = pd.NA
    D.loc[marcada, "id"] = np.arange(1, marcada.sum() + 1)
    return D


def muestras_atipicas(D: pd.DataFrame) -> pd.DataFrame:
    """Capturas para revisar a mano, con el motivo. Que aparezca acá no significa que esté mal:
    una carga con varios estados también se aparta de su ráfaga."""
    s = D[D["id"].notna()].copy()
    s["motivo"] = [" y ".join(m for m, v in (("cambia dentro de la ráfaga", a), ("más cerca de otra clase", b)) if v)
                   for a, b in zip(s["cambia_en_rafaga"], s["mas_cerca_de_otra"])]
    s["archivo"] = s["ruta"].str.split("/").str[-1].str[:15]
    cols = ["id", "etiqueta", "lugar", "archivo", "rms_A", "nivel_rafaga_dB", "forma_rafaga_dB", "nivel_clase_dB",
            "forma_clase_dB", "dist_propia_dB", "dist_vecina_dB", "clase_vecina", "motivo"]
    return s[cols].set_index("id").round(2)


def rafagas_atipicas(D: pd.DataFrame) -> pd.DataFrame:
    """Ráfagas enteras que se apartan del resto de su clase: suele ser otro equipo u otro estado."""
    filas = []
    for (c, r), g in D.groupby(["etiqueta", "rafaga"], sort=False):
        if D.loc[D["etiqueta"] == c, "rafaga"].nunique() < 2:
            continue
        nivel, forma = g["nivel_clase_dB"].median(), g["forma_clase_dB"].median()
        if abs(nivel) >= UMBRAL_NIVEL_DB or forma >= UMBRAL_FORMA_DB:
            filas.append({"etiqueta": c, "lugar": g["lugar"].iat[0], "rafaga": r.rsplit("_", 1)[-1], "capturas": len(g),
                          "rms_A": g["rms_A"].median(), "nivel_clase_dB": nivel, "forma_clase_dB": forma})
    return pd.DataFrame(filas).round(2)


def _puntos_lugar(ax, x, y, lugares, s=20, alpha=0.75, **kw) -> None:
    for L in pd.unique(lugares):
        m = np.asarray(lugares) == L
        ax.scatter(np.asarray(x)[m], np.asarray(y)[m], s=s, color=COLOR_LUGAR.get(L, (TINTA2,))[0], alpha=alpha, lw=0,
                   marker=MARCADOR_LUGAR.get(L, "o"), **kw)


def _leyenda_puntos(fig, lugares) -> None:
    from matplotlib.lines import Line2D
    h = [Line2D([], [], ls="", marker=MARCADOR_LUGAR.get(L, "o"), ms=6, color=COLOR_LUGAR.get(L, (TINTA2,))[0], label=L)
         for L in sorted(lugares)]
    fig.legend(handles=h, loc="upper right", ncols=len(h))


def plot_distribucion_clases(D: pd.DataFrame):
    """Vista general: qué tan dispersa es cada clase, en nivel y en forma."""
    clases = list(pd.unique(D["etiqueta"]))
    y = {c: len(clases) - 1 - i for i, c in enumerate(clases)}
    rng = np.random.default_rng(0)
    fig, axs = plt.subplots(1, 2, figsize=(13, 0.34 * len(clases) + 1.3), sharey=True)
    for ax, col, tit, umbrales in ((axs[0], "nivel_clase_dB", "Nivel: H1 contra el resto de la clase [dB]", (-UMBRAL_NIVEL_DB, UMBRAL_NIVEL_DB)),
                                   (axs[1], "forma_clase_dB", "Forma: firma relativa a H1 contra el resto de la clase [dB]", (UMBRAL_FORMA_DB,))):
        for u in umbrales:
            ax.axvline(u, color=APAGADO, lw=0.8)
        for c in clases:
            g = D[D["etiqueta"] == c]
            q0, q1, q2, q3, q4 = np.percentile(g[col].values, [0, 25, 50, 75, 100])
            ax.plot([q0, q4], [y[c]] * 2, color=EJE, lw=0.9, zorder=1)
            ax.add_patch(Rectangle((q1, y[c] - 0.3), q3 - q1, 0.6, facecolor=SUPERFICIE, edgecolor=EJE, lw=0.9, zorder=2))
            ax.plot([q2, q2], [y[c] - 0.3, y[c] + 0.3], color=TINTA, lw=1.5, zorder=4)
            _puntos_lugar(ax, g[col], y[c] + rng.uniform(-0.2, 0.2, len(g)), g["lugar"], s=16, zorder=3)
        ax.grid(axis="x"); ax.set_xlabel(tit)
    axs[0].axvline(0, color=EJE, lw=0.8)
    axs[0].set_yticks(list(y.values()), list(y.keys()))
    _leyenda_puntos(fig, D["lugar"].unique())
    fig.suptitle("Distribución dentro de cada categoría (punto = captura)", x=0.01, ha="left", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


def plot_nivel_forma(D: pd.DataFrame, ncols: int = 4):
    """Nivel contra forma, por clase. Las líneas son los umbrales de revisión de ráfagas; los
    números son las capturas de `muestras_atipicas`."""
    clases = list(pd.unique(D["etiqueta"]))
    fig, axs = _malla(len(clases), ncols, 13.5, 2.6, sharex=True, sharey=True)
    for ax, c in zip(axs, clases):
        g = D[D["etiqueta"] == c]
        for u in (-UMBRAL_NIVEL_DB, UMBRAL_NIVEL_DB):
            ax.axvline(u, color=GRILLA, lw=0.8)
        ax.axhline(UMBRAL_FORMA_DB, color=GRILLA, lw=0.8)
        _puntos_lugar(ax, g["nivel_clase_dB"], g["forma_clase_dB"], g["lugar"], s=24, zorder=3)
        for _, f in g[g["id"].notna()].iterrows():
            ax.scatter(f["nivel_clase_dB"], f["forma_clase_dB"], s=44, facecolor="none", edgecolor=TINTA, lw=0.8, zorder=4)
            ax.annotate(str(f["id"]), (f["nivel_clase_dB"], f["forma_clase_dB"]), xytext=(4, 3), textcoords="offset points",
                        fontsize=7.5, color=TINTA, zorder=5)
        ax.set_title(c, loc="left")
    for ax in axs[:: ncols]:
        ax.set_ylabel("forma [dB]")
    for ax in axs[max(0, len(clases) - ncols):len(clases)]:
        ax.tick_params(labelbottom=True); ax.set_xlabel("nivel [dB]")
    _leyenda_puntos(fig, D["lugar"].unique())
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


def plot_mapa_muestras(E: Espectros, D: pd.DataFrame, kmax: int = 40, ncols: int = 3, lim: float = 20.0):
    """Cada fila es una captura; cada columna, una armónica. Rojo: tiene más que el centro de
    su clase. Azul: menos. Las filas se agrupan por lugar y ráfaga, separadas por una línea."""
    A, _ = _vectores_db(E, kmax)
    clases = list(pd.unique(D["etiqueta"]))
    fig, axs = _malla(len(clases), ncols, 13.5, 2.7, sharex=True)
    for ax, c in zip(axs, clases):
        g = D[D["etiqueta"] == c].sort_values("lugar", kind="stable")     # dentro de cada lugar, en orden de captura
        M = A[g.index] - _centro(A, E.tabla.loc[g.index])
        pc = ax.imshow(M, cmap=DIVERGENTE, vmin=-lim, vmax=lim, aspect="auto", interpolation="nearest",
                       extent=(0.5, kmax + 0.5, len(g) - 0.5, -0.5))
        cambios = np.where(g["rafaga"].values[1:] != g["rafaga"].values[:-1])[0]
        for b in cambios:
            ax.axhline(b + 0.5, color=SUPERFICIE, lw=1.5)
        inicios = np.r_[0, cambios + 1, len(g)]
        ax.set_yticks((inicios[:-1] + inicios[1:] - 1) / 2,
                      [f"{g['lugar'].iat[i]} {g['rafaga'].iat[i].rsplit('_', 1)[-1]}" for i in inicios[:-1]], fontsize=7)
        ax.tick_params(axis="y", length=0)
        ax.spines[:].set_visible(False)
        for fila, v in enumerate(g["id"].values):
            if pd.notna(v):
                ax.text(kmax + 0.9, fila, str(v), fontsize=6.5, va="center", color=TINTA, clip_on=False)
        ax.set_title(c, loc="left")
    for ax in axs[max(0, len(clases) - ncols):len(clases)]:
        ax.tick_params(labelbottom=True); ax.set_xlabel("armónica k")
    fig.tight_layout(rect=(0, 0, 0.92, 1), w_pad=2.2)
    cax = fig.add_axes([0.935, 0.3, 0.012, 0.4])
    fig.colorbar(pc, cax=cax, label="dB contra el centro de la clase", extend="both")
    return fig


def plot_pca_muestras(E: Espectros, D: pd.DataFrame, kmax: int = 40, ncols: int = 4):
    """Todas las capturas en un mismo plano. El PCA se ajusta sobre ráfagas para que los casi
    duplicados no decidan los ejes; después se proyectan todas las capturas."""
    A, _ = _vectores_db(E, kmax)
    X = A[D.index]
    R = np.array([np.median(X[(D["rafaga"] == r).values], axis=0) for r in D["rafaga"].unique()])
    medio = R.mean(axis=0)
    _, ev, Vt = _pca(R)
    Z = (X - medio) @ Vt[:2].T
    clases = list(pd.unique(D["etiqueta"]))
    fig, axs = _malla(len(clases), ncols, 13.5, 2.75, sharex=True, sharey=True)
    for ax, c in zip(axs, clases):
        s = (D["etiqueta"] == c).values
        ax.scatter(Z[~s, 0], Z[~s, 1], s=8, color=GRILLA, lw=0)
        _puntos_lugar(ax, Z[s, 0], Z[s, 1], D["lugar"].values[s], s=24, zorder=3)
        for (zx, zy), v in zip(Z[s], D["id"].values[s]):
            if pd.notna(v):
                ax.scatter(zx, zy, s=44, facecolor="none", edgecolor=TINTA, lw=0.8, zorder=4)
                ax.annotate(str(v), (zx, zy), xytext=(4, 3), textcoords="offset points", fontsize=7.5, zorder=5)
        ax.set_title(c, loc="left")
    for ax in axs[:: ncols]:
        ax.set_ylabel(f"PC2 ({ev[1] * 100:.0f} %)")
    for ax in axs[max(0, len(clases) - ncols):len(clases)]:
        ax.tick_params(labelbottom=True); ax.set_xlabel(f"PC1 ({ev[0] * 100:.0f} %)")
    _leyenda_puntos(fig, D["lugar"].unique())
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


# ---------------------------------------------------------- señales reales --

def superposicion(E: Espectros, kmax: int = 25) -> tuple:
    """¿La firma de A+B es la suma de las firmas? Para cada combinada real, compara cada
    armónica con la suma lineal y cuadrática de sus cargas medidas solas en el MISMO lugar.
    Devuelve (tabla por captura para H1, ratios real/lineal, ratios real/cuadrática)."""
    t = E.tabla
    ref = {}
    for (c, L), g in t[t["tipo"] == TIPO_INDIVIDUAL].groupby(["etiqueta", "lugar"]):
        ref[c, L] = np.median(E.H[np.isin(E.cap, g.index)], axis=0)
    filas, r_lin, r_cuad = [], [], []
    for i, f in t[t["tipo"] == TIPO_COMBINADA].iterrows():
        partes = [ref.get((c, f["lugar"])) for c in f["etiqueta"].split("+")]
        if any(p is None for p in partes):
            continue
        P = np.array(partes)
        real = np.median(E.H[E.cap == i], axis=0)
        lin, cuad = P.sum(axis=0), np.sqrt((P ** 2).sum(axis=0))
        filas.append({"etiqueta": f["etiqueta"], "lugar": f["lugar"], "h1_real": real[0], "h1_lineal": lin[0], "h1_cuadratica": cuad[0]})
        r_lin.append((real / lin)[:kmax]); r_cuad.append((real / cuad)[:kmax])
    return pd.DataFrame(filas), np.array(r_lin), np.array(r_cuad)


def plot_superposicion(E: Espectros, kmax: int = 25):
    T, rl, rc = superposicion(E, kmax)
    fig, axs = plt.subplots(1, 2, figsize=(12, 3.8), width_ratios=[1, 1.7])
    m = max(T["h1_real"].max(), T["h1_lineal"].max()) * 1.05
    axs[0].plot([0, m], [0, m], color=EJE, lw=0.8)
    axs[0].scatter(T["h1_lineal"], T["h1_real"], s=16, color="#2a78d6", alpha=0.6, lw=0, label="contra suma lineal")
    axs[0].scatter(T["h1_cuadratica"], T["h1_real"], s=16, color="#eb6834", alpha=0.6, lw=0, label="contra suma cuadrática")
    axs[0].set(xlabel="suma de las cargas medidas solas [A]", ylabel="H1 real de la combinada [A]", xlim=(0, m), ylim=(0, m))
    axs[0].legend(loc="upper left"); axs[0].grid(); axs[0].set_title("Fundamental", loc="left")
    k = np.arange(1, kmax + 1)
    for R, col, nom in ((rl, "#2a78d6", "real / suma lineal"), (rc, "#eb6834", "real / suma cuadrática")):
        axs[1].fill_between(k, np.percentile(R, 10, axis=0), np.percentile(R, 90, axis=0), color=col, alpha=0.16, lw=0)
        axs[1].plot(k, np.median(R, axis=0), color=col, marker="o", ms=3, label=nom)
    axs[1].axhline(1, color=TINTA2, lw=0.8); axs[1].set_ylim(0, 2.2); axs[1].grid(axis="y")
    axs[1].set(xlabel="armónica k", ylabel="cociente"); axs[1].legend(loc="upper right")
    axs[1].set_title(f"Por armónica ({len(T)} combinadas, mediana y P10-P90)", loc="left")
    fig.tight_layout()
    return fig


def visibilidad(E: Espectros, clases=None, kmax: int = 40) -> tuple:
    """¿Se ve la carga chica S con la carga grande G prendida? Para cada armónica,
    margen = |H_k(S)| / dispersión de |H_k(G)| entre ventanas de una misma ráfaga de G.
    Margen < 1: lo que aporta S queda dentro de lo que G varía por sí sola.
    Devuelve (mejor margen por par, armónica donde se alcanza)."""
    clases = clases or E.clases(con_piso=False)
    t = E.tabla
    disp = {}
    for G in clases:
        grupos = t[t["etiqueta"] == G].groupby("rafaga")
        s_raf = []
        for _, g in grupos:
            H = E.H[np.isin(E.cap, g.index)][:, :kmax]
            s_raf.append(np.percentile(H, 90, axis=0) - np.percentile(H, 10, axis=0))
        disp[G] = np.median(s_raf, axis=0)
    M = pd.DataFrame(index=clases, columns=clases, dtype=float)
    K = pd.DataFrame(index=clases, columns=clases, dtype=float)
    for S in clases:
        a = perfil_armonico(E, S)[:kmax]
        for G in clases:
            r = a / np.maximum(disp[G], LSB_A / 8)
            M.loc[S, G], K.loc[S, G] = r.max(), r.argmax() + 1
    return M, K


def plot_visibilidad(E: Espectros, clases=None, kmax: int = 40):
    M, K = visibilidad(E, clases, kmax)
    fig, ax = plt.subplots(figsize=(8.5, 6))
    D = np.log10(M.values.astype(float))
    np.fill_diagonal(D, np.nan)
    pc = ax.imshow(D, cmap=DIVERGENTE.reversed(), vmin=-2, vmax=2)
    ax.set_xticks(range(len(M)), M.columns, rotation=60, ha="right"); ax.set_yticks(range(len(M)), M.index)
    ax.spines[:].set_visible(False); ax.tick_params(length=0)
    for i in range(len(M)):
        for j in range(len(M)):
            if i != j:
                ax.text(j, i, f"{int(K.values[i, j])}", ha="center", va="center", fontsize=7.5,
                        color="white" if abs(D[i, j]) > 1.1 else TINTA)
    ax.set(xlabel="carga grande G (ya prendida)", ylabel="carga S que se quiere detectar")
    ax.set_title("Margen de detección por armónica (número = mejor armónica)", loc="left")
    c = fig.colorbar(pc, ax=ax, fraction=0.04, pad=0.03, label="mejor margen |H_k(S)| / dispersión de H_k(G)")
    c.set_ticks([-2, -1, 0, 1, 2], labels=["0.01", "0.1", "1", "10", "100"])
    fig.tight_layout()
    return fig


# --------------------------------------------------------------- síntesis --

def sintesis_franjas(E: Espectros, agrupar: dict | None = None, clases=None, frac: float = 0.5) -> pd.DataFrame:
    """Resumen por clase para decidir franjas: dónde está la energía, hasta dónde se
    distingue del piso, y qué armónicas la separan del resto entre ráfagas."""
    clases = clases or E.clases(con_piso=False)
    tab, _, _ = energia_acumulada(E, clases)
    p = piso(E)
    inter = energia_interarmonica(E, clases)
    filas = []
    for c in clases:
        s = E.ventanas(c)
        sobre = np.array([np.average(E.mag[s][:, j] > p[j], weights=E.peso[s]) for j in range(len(p))])
        idx = np.where((sobre >= frac) & (E.freqs >= 75))[0]
        filas.append({"clase": c, "fund_%": tab.loc[c, "fundamental_%"], "resto_mA": tab.loc[c, "resto_mA"],
                      "f90_hz": tab.loc[c, "f90_hz"], "f99_hz": tab.loc[c, "f99_hz"],
                      "bins_sobre_piso": len(idx), "f_max_sobre_piso": E.freqs[idx.max()] if len(idx) else 0.0,
                      "interarm_%": inter[c]})
    return pd.DataFrame(filas).set_index("clase").round({"fund_%": 1, "resto_mA": 0, "f90_hz": 0, "f99_hz": 0, "f_max_sobre_piso": 0})


def costo_franjas(E: Espectros, tol_bins: int = 1, f0_margen: float = 0.2, fmaxs=(1000, 1500, 2000, 3000)) -> pd.DataFrame:
    """Bins que habría que leer si solo se usan las armónicas. La tolerancia crece con k porque
    un error de f0 de ±f0_margen Hz corre la armónica k en k·f0_margen Hz."""
    df = E.fs / E.W
    filas = []
    for fmax in fmaxs:
        bins = set()
        for k in range(1, int(fmax // 50) + 1):
            ancho = tol_bins + int(np.ceil(k * f0_margen / df))
            c = int(round(k * 50.0 / df))
            bins |= set(range(c - ancho, c + ancho + 1))
        filas.append({"f_max_hz": fmax, "n_armonicas": int(fmax // 50), "todos_los_bins": int(fmax / df),
                      "bins_en_armonicas": len(bins)})
    return pd.DataFrame(filas).set_index("f_max_hz")
