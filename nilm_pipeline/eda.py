"""
Análisis exploratorio de las capturas de corriente (Parte A del notebook).

No entrena nada. Usa las mismas funciones de espectro que el borde (ventana Hann, FFT, |X|·2/Σw),
así que lo que se ve acá es lo que va a ver el ESP32-S3.
"""
from __future__ import annotations

from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from scipy.signal import windows

from .config import Config
from .data import TIPO_COMBINADA, TIPO_INDIVIDUAL, Dataset, _timestamp
from .features import _mag_alineada, armonicas, espectro, estimar_f0
from .mix import combinar
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

# --- paleta: colores fijos por clase y por lugar
SUPERFICIE, TINTA, TINTA2, APAGADO, GRILLA, EJE = ("#fcfcfb", "#0b0b0b", "#52514e", "#898781",
                                                   "#e1e0d9", "#c3c2b7")
COLOR_CLASE = {"aire": "#2a78d6", "aireinverter": "#eb6834", "heladera": "#1baf7a", "lavarropa": "#eda100",
               "luces": "#e87ba4", "resistiva": "#008300", "tele": "#6250d6", "parasito": "#e34948"}
COLOR_LUGAR = {"casa": ("#2a78d6", "-"), "depaleo": ("#eb6834", "-"), "depamica": ("#1baf7a", "-"),
               "local": (TINTA2, "--")}


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
    """Descriptores y ciclo promedio de cada captura."""
    tabla: pd.DataFrame     # una fila por captura
    ciclos: np.ndarray      # (capturas, 200) ciclo promedio alineado a la fundamental

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
    filas, ciclos = [], []
    for r in ds.registros:
        V, _ = preprocesar_ventanas(segmentar(r.datos, W, solape), cfg)
        mag, freqs = espectro(V, cfg, fs)
        f0 = estimar_f0(mag, freqs, cfg.f_red)
        H = armonicas(mag, freqs, f0, N_ARMONICAS, 1)
        x = r.datos.astype(np.float64)
        ac = x - x.mean()
        filas.append({
            "ruta": r.ruta, "etiqueta": "+".join(r.clases) or PISO_ETIQUETA, "tipo": r.tipo,
            "lugar": r.lugar, "sesion": r.sesion, "rafaga": r.rafaga,
            "duracion_s": len(x) / fs, "dc_mA": x.mean() * 1e3, "rms_A": ac.std(), "pico_A": np.abs(x).max(),
            "frac_riel": float(np.mean(np.abs(x) >= 0.99 * RIEL_A)), "h1_A": float(np.median(H[:, 0])),
            "f0_hz": float(np.median(f0)), "f0_min": float(f0.min()), "f0_max": float(f0.max()),
        })
        ciclos.append(_ciclo_promedio(ac, float(np.median(f0)), fs))
    tabla = pd.DataFrame(filas)
    tabla["rafaga"] = _separar_por_nivel(tabla)
    return Espectros(tabla, np.array(ciclos))


def _malla(n: int, ncols: int, ancho: float, alto: float, **kw):
    filas = int(np.ceil(n / ncols))
    fig, axs = plt.subplots(filas, ncols, figsize=(ancho, alto * filas), squeeze=False, **kw)
    axs = axs.ravel()
    for a in axs[n:]:
        a.set_visible(False)
    return fig, axs


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
    r = t[t["h1_A"] > H1_MIN_F0_A]
    f0 = r.groupby("sesion").agg(f0_min=("f0_min", "min"), f0_mediana=("f0_hz", "median"), f0_max=("f0_max", "max")) - SESGO_F0_HZ
    return {
        "capturas": len(t), "duracion_s": t["duracion_s"].value_counts().to_dict(),
        "max_abs_A": float(t["pico_A"].max()), "riel_A": RIEL_A,
        "con_muestras_en_riel": int((t["frac_riel"] > 0).sum()),
        "dc_mA_por_lugar": t.groupby("lugar")["dc_mA"].agg(["min", "median", "max"]).round(0),
        "f0_por_sesion": f0.round(3),
        "f0_captura_min_max": (float(r["f0_hz"].min()) - SESGO_F0_HZ, float(r["f0_hz"].max()) - SESGO_F0_HZ),
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


def _fft_senal(x: np.ndarray, n_fft: int, fs: float, f_red: float, alinear_f0: bool = True) -> tuple:
    """Espectro de una captura completa, con ventana Hann y completada con ceros hasta n_fft."""
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    w = windows.hann(len(x), sym=False)
    mag = (2 * np.abs(np.fft.rfft(x * w, n=n_fft)) / w.sum())[None, :].astype(np.float32)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / fs).astype(np.float32)
    return (_mag_alineada(mag, freqs, f_red) if alinear_f0 else mag), freqs


def fft_por_clase(ds: Dataset, modo: str = "senal", alinear_f0: bool = True, W: int = 2048, solape: float = 0.5) -> FFTClases:
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
            if alinear_f0:
                mag = _mag_alineada(mag, freqs, cfg.f_red)
        else:
            mag, freqs = _fft_senal(r.datos, n_fft, fs, cfg.f_red, alinear_f0)
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


def _y_minimo(f: FFTClases) -> float:
    """Abajo del piso de ruido, que es más bajo con la señal completa."""
    return 3e-5 if f.modo == "senal" else 1e-4


def _dibujar_fft(ax, f: FFTClases, c: str, color: str, escala: str, fmax: float, lw: float = 0.8, alpha: float = 0.2,
                 fmin: float = 0.0) -> None:
    b = (f.freqs > fmin) & (f.freqs <= fmax)
    x, m, s = f.freqs[b], f.media[c][b], f.desvio[c][b]
    abajo = np.maximum(m - s, _y_minimo(f)) if escala == "log" else np.maximum(m - s, 0.0)
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
            ax.set_yscale("log"); ax.set_ylim(_y_minimo(f), 30)
        else:
            ax.set_ylim(bottom=0)
        ax.set_xlim(0, fmax); ax.grid(axis="y"); ax.set_xticks(np.arange(0, fmax + 1, 500 if fmax > 1000 else 100))
        ax.set_title(f"{c}  ({f.n[c]} espectros)", loc="left")
    for ax in axs[:: ncols]:
        ax.set_ylabel("|I| [A]")
    for ax in axs[max(0, len(clases) - ncols):len(clases)]:
        ax.tick_params(labelbottom=True); ax.set_xlabel("f [Hz]")
    gris = "; gris: media de nada" if log and PISO_ETIQUETA in f.media else ""
    fig.suptitle(f"Media y ±1 desvío de la FFT por clase, escala {'logarítmica' if log else 'lineal'} ({_titulo_fft(f)}{gris})", x=0.01, ha="left", fontsize=10)
    fig.tight_layout()
    return fig


def plot_fft_superpuestas(f: FFTClases, escala: str = "log", paso: float | None = None, fmax: float = 3000.0,
                          ncols: int | None = None, alpha: float = 0.12):
    """Todas las clases en los mismos ejes. Sin `paso`, un solo panel de 0 a fmax. Con `paso`, un panel por
    rango de frecuencia, cada uno con su escala vertical, para que se vean las armónicas de ese rango."""
    log = escala == "log"
    clases = f.clases
    orden = sorted(clases, key=lambda c: -f.media[c].max())     # las de mayor corriente quedan detrás
    bordes = np.arange(0.0, fmax + 1, paso) if paso else np.array([0.0, fmax])
    n = len(bordes) - 1
    ncols = ncols or (2 if n > 3 else 1)
    alto = 3.0 if n > 1 else 4.6
    fig, axs = _malla(n, ncols, 13.5, alto)
    for ax, ini, fin in zip(axs, bordes[:-1], bordes[1:]):
        b = (f.freqs > ini) & (f.freqs <= fin)
        if log and PISO_ETIQUETA in f.media:
            ax.plot(f.freqs[b], f.media[PISO_ETIQUETA][b], color=APAGADO, lw=0.7, zorder=0)
        for c in orden:
            _dibujar_fft(ax, f, c, COLOR_CLASE.get(c, "#2a78d6"), escala, fin, lw=0.9, alpha=alpha, fmin=ini)
        tope = max((f.media[c] + f.desvio[c])[b].max() for c in clases)
        if log:
            ax.set_yscale("log"); ax.set_ylim(_y_minimo(f), tope * 1.5)
        else:
            ax.set_ylim(0, tope * 1.05)
        ax.set_xlim(ini, fin); ax.grid(axis="y")
        ancho = fin - ini
        ax.set_xticks(np.arange(ini, fin + 1, 500.0 if ancho > 1000 else (200.0 if ancho > 500 else 100.0)))
        ax.set_ylabel("|I| [A]")
        if n > 1:
            ax.set_title(f"{ini:.0f} a {fin:.0f} Hz", loc="left")
    for ax in axs[max(0, n - ncols):n]:
        ax.set_xlabel("f [Hz]")
    h = [Line2D([], [], color=COLOR_CLASE.get(c, "#2a78d6"), lw=2, label=c) for c in clases]
    if log and PISO_ETIQUETA in f.media:
        h.append(Line2D([], [], color=APAGADO, lw=2, label="nada"))
    rango = f"rangos de {paso:.0f} Hz" if paso else f"0 a {fmax:.0f} Hz"
    fig.suptitle(f"Todas las clases, escala {'logarítmica' if log else 'lineal'}, {rango} ({_titulo_fft(f)})", x=0.01, ha="left", fontsize=10)
    alto_total = fig.get_size_inches()[1]
    fig.legend(handles=h, loc="upper left", bbox_to_anchor=(0.005, 1 - 0.3 / alto_total), ncols=len(h))
    fig.tight_layout(rect=(0, 0, 1, 1 - 0.65 / alto_total))
    return fig


# ---------------------------------------------- combinada real, individuales y sintética --

COLOR_SINTETICA = "#6250d6"
GRUPO_DE_COLOR = {"calefon": "resistiva", "horno": "resistiva", "pava": "resistiva", "plancha": "resistiva", "airecalor": "aire"}


@dataclass
class ComparacionCombinada:
    """Espectros de una captura combinada real, de cada carga medida sola y de su mezcla sintética."""
    etiqueta: str
    lugar: str
    freqs: np.ndarray
    senales: list           # [{"rol", "archivo", "rms_A", "mag", "color"}]; real, individuales, sintética


def _nombre_archivo(r) -> str:
    return r.ruta.split("/")[-1][:15]


def _combinadas_disponibles(ds: Dataset) -> dict:
    """{(clases, lugar): capturas} de combinadas que tienen todas sus cargas medidas solas en ese lugar."""
    solas = {(r.clases[0], r.lugar) for r in ds.registros if r.tipo == TIPO_INDIVIDUAL}
    out = {}
    for r in ds.registros:
        if r.tipo == TIPO_COMBINADA and all((c, r.lugar) in solas for c in r.clases):
            out.setdefault((r.clases, r.lugar), []).append(r)
    return out


def comparar_combinada(ds: Dataset, combinacion: str, lugar: str | None = None, seed: int = 0, cfg: Config | None = None,
                       alinear_f0: bool = True) -> ComparacionCombinada:
    """Toma la captura real de RMS mediano de esa combinación. Para cada carga elige la captura individual
    más cercana en el tiempo y en el mismo lugar, y las mezcla con la misma función y los mismos parámetros
    que el entrenamiento. `ds` tiene que tener las clases originales: "calefon", no "resistiva"."""
    cfg = cfg or Config()
    clases = tuple(sorted(c.strip() for c in combinacion.replace("+", ",").split(",")))
    disponibles = _combinadas_disponibles(ds)
    grupos = {k: v for k, v in disponibles.items() if k[0] == clases and (lugar is None or k[1] == lugar)}
    if not grupos:
        opciones = sorted({"+".join(k[0]) + f" ({k[1]})" for k in disponibles})
        raise ValueError(f"no hay '{combinacion}' con sus cargas medidas solas. Disponibles: {opciones}")
    (_, lugar), capturas = max(grupos.items(), key=lambda kv: len(kv[1]))
    capturas = sorted(capturas, key=lambda r: float(np.std(r.datos)))
    real = capturas[len(capturas) // 2]
    t_real = _timestamp(real.ruta)
    individuales = []
    for c in clases:
        solas = [r for r in ds.registros if r.tipo == TIPO_INDIVIDUAL and r.clases == (c,) and r.lugar == lugar]
        individuales.append(min(solas, key=lambda r: abs((_timestamp(r.ruta) - t_real).total_seconds())))
    fs = ds.fs()
    sintetica = combinar([r.datos for r in individuales], np.random.default_rng(seed), cfg.mix_escala, fs, cfg.f_red, cfg.mix_fase,
                         cfg.mix_jitter_fase, alinear_f0=cfg.mix_alinear_f0, armonicas_azar=cfg.mix_armonicas_azar)
    n_fft = max(len(x) for x in [real.datos, sintetica] + [r.datos for r in individuales])
    espectro_de = lambda x: _fft_senal(x, n_fft, fs, cfg.f_red, alinear_f0)
    senales = [{"rol": "Combinada real", "archivo": _nombre_archivo(real), "rms_A": float(np.std(real.datos)), "mag": espectro_de(real.datos)[0][0], "color": TINTA}]
    for c, r in zip(clases, individuales):
        senales.append({"rol": f"Individual: {c}", "archivo": _nombre_archivo(r), "rms_A": float(np.std(r.datos)), "mag": espectro_de(r.datos)[0][0],
                        "color": COLOR_CLASE.get(GRUPO_DE_COLOR.get(c, c), "#2a78d6")})
    senales.append({"rol": "Sintética", "archivo": "suma de las individuales", "rms_A": float(np.std(sintetica)), "mag": espectro_de(sintetica)[0][0], "color": COLOR_SINTETICA})
    return ComparacionCombinada("+".join(clases), lugar, espectro_de(real.datos)[1].astype(np.float64), senales)


def _picos_armonicos(C: ComparacionCombinada, mag: np.ndarray, armonicas_k) -> np.ndarray:
    return np.array([mag[np.abs(C.freqs - k * 50.0) <= 8.0].max() for k in armonicas_k])


def tabla_combinada(C: ComparacionCombinada, armonicas_k=(1, 2, 3, 4, 5, 7, 9, 11, 13)) -> pd.DataFrame:
    """Amplitud de cada armónica en cada señal [mA], y cuánto se aparta la sintética de la real [dB]."""
    tabla = {s["rol"]: _picos_armonicos(C, s["mag"], armonicas_k) * 1e3 for s in C.senales}
    k = np.rint(C.freqs / 50.0)
    fuera = (np.abs(C.freqs - k * 50.0) >= 15.0) & (C.freqs >= 1000.0) & (C.freqs <= 3000.0)
    for s in C.senales:
        tabla[s["rol"]] = np.append(tabla[s["rol"]], 1e3 * float(np.median(s["mag"][fuera])))
    t = pd.DataFrame(tabla, index=[f"H{k}" for k in armonicas_k] + ["piso de 1 a 3 kHz"]).round(2)
    t["sintética − real [dB]"] = (20 * np.log10(tabla["Sintética"] / tabla["Combinada real"])).round(1)
    return t


def resumen_combinaciones(ds: Dataset, seed: int = 0) -> pd.DataFrame:
    """Para elegir qué mirar: qué tan parecida es la sintética a la real en cada combinación.
    dif_H2_H40: mediana de |sintética − real| en dB, en las armónicas que superan 1 mA en alguna de las dos."""
    filas = []
    for (clases, lugar), capturas in sorted(_combinadas_disponibles(ds).items(), key=lambda kv: -len(kv[1])):
        C = comparar_combinada(ds, "+".join(clases), lugar, seed)
        real, sint = C.senales[0], C.senales[-1]
        k = np.arange(2, 41)
        a, b = _picos_armonicos(C, real["mag"], k), _picos_armonicos(C, sint["mag"], k)
        m = (a > 1e-3) | (b > 1e-3)
        filas.append({"combinacion": C.etiqueta, "lugar": lugar, "capturas": len(capturas), "rms_real_A": real["rms_A"], "rms_sintetica_A": sint["rms_A"],
                      "dif_H1_dB": 20 * np.log10(_picos_armonicos(C, sint["mag"], [1])[0] / _picos_armonicos(C, real["mag"], [1])[0]),
                      "dif_H3_dB": 20 * np.log10(_picos_armonicos(C, sint["mag"], [3])[0] / _picos_armonicos(C, real["mag"], [3])[0]),
                      "dif_H2_H40_dB": float(np.median(np.abs(20 * np.log10(b[m] / a[m]))))})
    return pd.DataFrame(filas).round(2).set_index("combinacion")


def plot_combinada(C: ComparacionCombinada, escala: str = "log", paso: float | None = None, fmax: float = 3000.0, columnas: int = 3) -> list:
    """Una fila por señal: real, individuales y sintética. Sin `paso`, de 0 a fmax. Con `paso`, una columna por rango;
    dentro de cada rango todas las señales comparten la escala vertical, para poder compararlas.
    Devuelve una lista de figuras, porque los rangos de a 500 Hz no entran en una sola."""
    log = escala == "log"
    bordes = np.arange(0.0, fmax + 1, paso) if paso else np.array([0.0, fmax])
    rangos = list(zip(bordes[:-1], bordes[1:]))
    tandas = [rangos[i:i + columnas] for i in range(0, len(rangos), columnas)] if paso else [rangos]
    figuras = []
    for parte, tanda in enumerate(tandas):
        fig, axs = plt.subplots(len(C.senales), len(tanda), figsize=(13.5, 1.9 * len(C.senales) + 0.9), squeeze=False)
        for j, (ini, fin) in enumerate(tanda):
            b = (C.freqs > ini) & (C.freqs <= fin)
            tope = max(s["mag"][b].max() for s in C.senales)
            ancho = fin - ini
            for i, s in enumerate(C.senales):
                ax = axs[i, j]
                ax.plot(C.freqs[b], s["mag"][b], color=s["color"], lw=0.8)
                if log:
                    ax.set_yscale("log"); ax.set_ylim(3e-5, tope * 1.5)
                else:
                    ax.set_ylim(0, tope * 1.05)
                ax.set_xlim(ini, fin); ax.grid(axis="y")
                ax.set_xticks(np.arange(ini, fin + 1, 500.0 if ancho > 1000 else (200.0 if ancho > 500 else 100.0)))
                ax.set_title(f"{s['rol']}" + (f", {ini:.0f} a {fin:.0f} Hz" if paso else f" ({s['archivo']}, {s['rms_A']:.2f} A RMS)"), loc="left", fontsize=9)
                if i < len(C.senales) - 1:
                    ax.tick_params(labelbottom=False)
                if j == 0:
                    ax.set_ylabel("|I| [A]")
            axs[-1, j].set_xlabel("f [Hz]")
        rango = f"rangos de {paso:.0f} Hz" + (f" ({parte + 1} de {len(tandas)})" if len(tandas) > 1 else "") if paso else f"0 a {fmax:.0f} Hz"
        fig.suptitle(f"{C.etiqueta} en {C.lugar}, escala {'logarítmica' if log else 'lineal'}, {rango}", x=0.01, ha="left", fontsize=10)
        fig.tight_layout()
        figuras.append(fig)
    return figuras
