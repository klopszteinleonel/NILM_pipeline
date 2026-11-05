"""
Análisis exploratorio de las capturas de corriente (Parte A del notebook).

No entrena nada. Usa las mismas funciones de espectro que el borde (ventana Hann, FFT, |X|·2/Σw),
así que lo que se ve acá es lo que va a ver el ESP32-S3.

Las capturas de una ráfaga son casi duplicados: `E.peso` hace que todas las ráfagas de una clase
pesen lo mismo. La media y el desvío de `fft_por_clase` son sobre todas las muestras, sin ponderar.
"""
from __future__ import annotations

from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, PowerNorm
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from scipy.signal import windows

from .config import Config
from .data import TIPO_COMBINADA, Dataset
from .features import _mag_alineada, armonicas, espectro, estimar_f0
from .preprocess import preprocesar_ventanas
from .segment import segmentar

LSB_A = 15.0 * 3.3 / 4096           # 12.08 mA por cuenta con k = 15
RIEL_A = 2048 * LSB_A               # ±24.75 A pico = 17.5 A RMS senoidal
N_ARMONICAS = 60                    # hasta 3 kHz: arriba casi no hay señal
PISO_ETIQUETA = "nada"
# En estas clases cada ocasión es una configuración distinta (ej. otra cantidad de lámparas) y el
# cambio entre capturas puede tardar menos que la pausa que separa ráfagas. Una captura abre una
# ocasión nueva si su RMS salta más que SALTO_NIVEL_DB: las cargas estables saltan ~1 dB como mucho.
CLASES_POR_CONFIGURACION = ("luces",)
SALTO_NIVEL_DB = 1.5
H1_MIN_F0_A = 0.1                   # con menos fundamental, la f0 estimada no es confiable
SESGO_F0_HZ = 0.074                 # estimar_f0 lee de más con W=2048 (medido contra la fase de las capturas)

# --- paleta: colores fijos por lugar, secuencial de un tono, divergente azul-rojo
SUPERFICIE, TINTA, TINTA2, APAGADO, GRILLA, EJE = ("#fcfcfb", "#0b0b0b", "#52514e", "#898781",
                                                   "#e1e0d9", "#c3c2b7")
COLOR_CLASE = {"aire": "#2a78d6", "aireinverter": "#eb6834", "heladera": "#1baf7a", "lavarropa": "#eda100",
               "luces": "#e87ba4", "resistiva": "#008300", "tele": "#6250d6", "parasito": "#e34948"}
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


def calidad(E: Espectros) -> dict:
    """Saturación, duración, offset por lugar y frecuencia de red por sesión."""
    t = E.tabla
    df_bin = E.fs / E.W
    r = t[t["h1_A"] > H1_MIN_F0_A]
    f0 = r.groupby("sesion").agg(f0_min=("f0_min", "min"), f0_mediana=("f0_hz", "median"), f0_max=("f0_max", "max")) - SESGO_F0_HZ
    desvio = (r["f0_hz"] - SESGO_F0_HZ - 50.0).abs().max()
    return {
        "capturas": len(t), "duracion_s": t["duracion_s"].value_counts().to_dict(),
        "max_abs_A": float(t["pico_A"].max()), "riel_A": RIEL_A,
        "con_muestras_en_riel": int((t["frac_riel"] > 0).sum()),
        "dc_mA_por_lugar": t.groupby("lugar")["dc_mA"].agg(["min", "median", "max"]).round(0),
        "f0_por_sesion": f0.round(3),
        "ancho_bin_hz": df_bin,
        "f0_captura_min_max": (float(r["f0_hz"].min()) - SESGO_F0_HZ, float(r["f0_hz"].max()) - SESGO_F0_HZ),
        "armonica_que_se_corre_un_bin": df_bin / desvio,
    }


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
    h = [Line2D([], [], color=COLOR_LUGAR.get(L, (TINTA2, "-"))[0], ls=COLOR_LUGAR.get(L, (TINTA2, "-"))[1], label=L)
         for L in sorted(lugares)]
    fig.legend(handles=h, loc="upper right", ncols=len(h))


# ------------------------------------------------- FFT media y desvío por clase --

@dataclass
class FFTClases:
    """Media y desvío estándar de la amplitud por frecuencia, para cada clase."""
    modo: str               # "ventanas" | "senal"
    alinear_f0: bool
    freqs: np.ndarray
    media: dict             # clase -> (bins,) [A]
    desvio: dict            # clase -> (bins,) [A]
    n: dict                 # clase -> espectros promediados
    pico_individual: dict   # clase -> (N_ARMONICAS,): media del pico de cada armónica antes de promediar

    @property
    def clases(self) -> list:
        return [c for c in self.media if c != PISO_ETIQUETA]


def fft_por_clase(ds: Dataset, modo: str = "ventanas", alinear_f0: bool = True, W: int = 2048, solape: float = 0.5) -> FFTClases:
    """Media y desvío de la FFT de todas las muestras individuales de cada clase.

    modo="ventanas": un espectro por ventana de W muestras, lo mismo que recibe el modelo.
    modo="senal": un espectro por captura completa. Para que todas compartan el eje, las capturas de
    1 s se completan con ceros hasta el largo de las de 2 s: 0.5 Hz entre puntos.
    alinear_f0: corrige el eje con la f0 estimada, para que las armónicas de todas caigan en el mismo lugar."""
    if modo not in ("ventanas", "senal"):
        raise ValueError(f"modo={modo!r}: debe ser 'ventanas' o 'senal'")
    cfg = Config(W=W, solape=solape)
    fs = ds.fs()
    registros = [r for r in ds.registros if r.tipo != TIPO_COMBINADA]
    n_fft = max(len(r.datos) for r in registros)
    orden = {}
    for r in registros:
        c = "+".join(r.clases) or PISO_ETIQUETA
        if modo == "ventanas":
            V, _ = preprocesar_ventanas(segmentar(r.datos, W, solape), cfg)
            mag, freqs = espectro(V, cfg, fs)
        else:
            x = r.datos.astype(np.float64) - float(r.datos.mean())
            w = windows.hann(len(x), sym=False)
            mag = (2 * np.abs(np.fft.rfft(x * w, n=n_fft)) / w.sum())[None, :].astype(np.float32)
            freqs = np.fft.rfftfreq(n_fft, 1.0 / fs).astype(np.float32)
        if alinear_f0:
            mag = _mag_alineada(mag, freqs, cfg.f_red)
        m = mag.astype(np.float64)
        picos = np.array([m[:, np.abs(freqs - k * cfg.f_red) <= 8.0].max(axis=1) for k in range(1, N_ARMONICAS + 1)]).T
        d = orden.setdefault(c, {"s1": 0.0, "s2": 0.0, "n": 0, "pico": 0.0})
        d["s1"], d["s2"], d["n"], d["pico"] = d["s1"] + m.sum(axis=0), d["s2"] + (m ** 2).sum(axis=0), d["n"] + len(m), d["pico"] + picos.sum(axis=0)
    media, desvio, n, pico = {}, {}, {}, {}
    clave = lambda c: (c == PISO_ETIQUETA, c)
    for c in sorted(orden, key=clave):
        d = orden[c]
        media[c] = d["s1"] / d["n"]
        desvio[c] = np.sqrt(np.maximum(d["s2"] / d["n"] - media[c] ** 2, 0.0))
        n[c], pico[c] = d["n"], d["pico"] / d["n"]
    return FFTClases(modo, alinear_f0, freqs.astype(np.float64), media, desvio, n, pico)


def comparar_modos_fft(ds: Dataset) -> tuple:
    """Calcula las cuatro variantes y las resume. Devuelve (tabla, {nombre: FFTClases}).
    perdida_Hk: cuánto baja el pico de esa armónica al promediar, porque las armónicas de cada
    espectro no caen exactamente en el mismo lugar. Cuanto más cerca de 0 dB, mejor promedia."""
    variantes = {"ventanas": ("ventanas", False), "ventanas + f0": ("ventanas", True),
                 "señal completa": ("senal", False), "señal completa + f0": ("senal", True)}
    F = {k: fft_por_clase(ds, m, a) for k, (m, a) in variantes.items()}
    filas = {}
    for nombre, f in F.items():
        df = f.freqs[1] - f.freqs[0]
        k = np.rint(f.freqs / 50.0)
        fuera = (k >= 1) & (np.abs(f.freqs - k * 50.0) >= 15.0) & (f.freqs < 3000.0)
        perdida = np.median([[20 * np.log10(f.media[c][np.abs(f.freqs - h * 50.0) <= 8.0].max() / f.pico_individual[c][h - 1]) for h in (10, 20, 40)]
                             for c in f.clases], axis=0)
        filas[nombre] = {"resolucion_Hz": df, "puntos_hasta_3kHz": int((f.freqs <= 3000.0).sum()),
                         "piso_mA": 1e3 * float(np.median(f.media[PISO_ETIQUETA][fuera])),
                         "perdida_H10_dB": perdida[0], "perdida_H20_dB": perdida[1], "perdida_H40_dB": perdida[2]}
    return pd.DataFrame(filas).T.round({"resolucion_Hz": 2, "piso_mA": 2, "perdida_H10_dB": 1, "perdida_H20_dB": 1, "perdida_H40_dB": 1}), F


def _dibujar_fft(ax, f: FFTClases, c: str, color: str, escala: str, fmax: float, lw: float = 0.8, alpha: float = 0.2, piso_grafico: float = 1e-4) -> None:
    b = (f.freqs > 0) & (f.freqs <= fmax)
    x, m, s = f.freqs[b], f.media[c][b], f.desvio[c][b]
    abajo = np.maximum(m - s, piso_grafico) if escala == "log" else np.maximum(m - s, 0.0)
    ax.fill_between(x, abajo, m + s, color=color, alpha=alpha, lw=0, zorder=1)
    ax.plot(x, m, color=color, lw=lw, zorder=2)


def _titulo_fft(f: FFTClases) -> str:
    return f"{'ventanas de 2048' if f.modo == 'ventanas' else 'señal completa'}{', eje corregido con f0' if f.alinear_f0 else ''}"


def plot_comparar_modos_fft(F: dict, clase: str = "aire", fmin: float = 900.0, fmax: float = 1100.0):
    """Las cuatro variantes en un mismo tramo de frecuencia."""
    fig, axs = plt.subplots(2, 2, figsize=(13, 5.2), sharex=True, sharey=True)
    for ax, (nombre, f) in zip(axs.ravel(), F.items()):
        _dibujar_fft(ax, f, clase, COLOR_CLASE.get(clase, "#2a78d6"), "log", fmax, lw=1.0)
        ax.set_yscale("log"); ax.set_xlim(fmin, fmax); ax.grid(axis="y"); ax.set_title(nombre, loc="left")
    for ax in axs[:, 0]:
        ax.set_ylabel("|I| [A]")
    for ax in axs[1]:
        ax.set_xlabel("f [Hz]")
    fig.suptitle(f"{clase}: media y ±1 desvío entre {fmin:.0f} y {fmax:.0f} Hz", x=0.01, ha="left", fontsize=10)
    fig.tight_layout()
    return fig


def plot_fft_clases(f: FFTClases, escala: str = "log", fmax: float = 3000.0, ncols: int = 2, alto: float = 2.6):
    """Media (línea) y ±1 desvío (banda) de cada clase, en un panel por clase.
    En log, gris = media de `nada`, y la banda se corta abajo donde media − desvío es ≤ 0."""
    clases = f.clases
    log = escala == "log"
    fig, axs = _malla(len(clases), ncols, 13.5, alto, sharex=True, sharey=log)
    for ax, c in zip(axs, clases):
        if log and PISO_ETIQUETA in f.media:
            b = (f.freqs > 0) & (f.freqs <= fmax)
            ax.plot(f.freqs[b], f.media[PISO_ETIQUETA][b], color=APAGADO, lw=0.5, zorder=0)
        _dibujar_fft(ax, f, c, COLOR_CLASE.get(c, "#2a78d6"), escala, fmax, alpha=0.2 if log else 0.3)
        if log:
            ax.set_yscale("log"); ax.set_ylim(1e-4, 30)
        else:
            ax.set_ylim(bottom=0)
        ax.set_xlim(0, fmax); ax.grid(axis="y"); _xticks_hz(ax, fmax)
        ax.set_title(f"{c}  ({f.n[c]} espectros)", loc="left")
    for ax in axs[:: ncols]:
        ax.set_ylabel("|I| [A]")
    for ax in axs[max(0, len(clases) - ncols):len(clases)]:
        ax.tick_params(labelbottom=True); ax.set_xlabel("f [Hz]")
    gris = "; gris: media de nada" if log and PISO_ETIQUETA in f.media else ""
    fig.suptitle(f"Media y ±1 desvío de la FFT por clase, escala {'logarítmica' if log else 'lineal'} ({_titulo_fft(f)}{gris})", x=0.01, ha="left", fontsize=10)
    fig.tight_layout()
    return fig


def plot_fft_superpuestas(f: FFTClases, escala: str = "log", fmax: float = 3000.0, zoom: float = 500.0, alpha: float = 0.12):
    """Todas las clases en los mismos ejes: rango completo arriba y detalle de la zona baja abajo."""
    clases = f.clases
    log = escala == "log"
    fig, axs = plt.subplots(2, 1, figsize=(13.5, 8))
    # primero las de mayor corriente, para que las chicas queden encima
    orden = sorted(clases, key=lambda c: -f.media[c].max())
    for ax, tope in zip(axs, (fmax, zoom)):
        if log and PISO_ETIQUETA in f.media:
            b = (f.freqs > 0) & (f.freqs <= tope)
            ax.plot(f.freqs[b], f.media[PISO_ETIQUETA][b], color=APAGADO, lw=0.7, zorder=0)
        for c in orden:
            _dibujar_fft(ax, f, c, COLOR_CLASE.get(c, "#2a78d6"), escala, tope, lw=0.9, alpha=alpha)
        if log:
            ax.set_yscale("log"); ax.set_ylim(1e-4, 30)
        else:
            ax.set_ylim(bottom=0)
        ax.set_xlim(0, tope); ax.grid(axis="y"); _xticks_hz(ax, tope)
        ax.set_xlabel("f [Hz]"); ax.set_ylabel("|I| [A]")
    h = [Line2D([], [], color=COLOR_CLASE.get(c, "#2a78d6"), lw=2, label=c) for c in clases]
    if log and PISO_ETIQUETA in f.media:
        h.append(Line2D([], [], color=APAGADO, lw=2, label="nada"))
    fig.legend(handles=h, loc="upper right", ncols=len(h))
    fig.suptitle(f"Todas las clases, escala {'logarítmica' if log else 'lineal'} ({_titulo_fft(f)})", x=0.01, ha="left", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
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
    fig.legend(handles=[Line2D([], [], color=TINTA, lw=1, label="mediana de la clase"),
                        Line2D([], [], color=APAGADO, lw=1, label="piso: P99 de nada")],
               loc="upper left", ncols=2)
    fig.tight_layout(rect=(0, 0, 0.93, 0.98))
    cax = fig.add_axes([0.945, 0.3, 0.012, 0.4])
    fig.colorbar(pc, cax=cax, label="fracción de ventanas")
    return fig


def _pca(X: np.ndarray):
    Xc = X - X.mean(axis=0)
    U, s, Vt = np.linalg.svd(Xc, full_matrices=False)
    return U * s, s ** 2 / (s ** 2).sum(), Vt


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
