"""
Parte C del notebook: eventos y resta con fase.

Un evento es una carga que se prende o se apaga durante una captura (`eventos/` del dataset, siempre con `--canal iv`).
Estas capturas NO entran al entrenamiento ni a las combinadas (`data.CARPETAS_IGNORADAS`): solo sirven para validar la
idea de la placa en tiempo real, que es clasificar la DIFERENCIA entre el antes y el después de cada evento.

La resta necesita fase. Cada ciclo se corta en el cruce ascendente de la tensión y se remuestrea a `N_CICLO` puntos:
así el ángulo de cada armónica de corriente queda medido desde el mismo cero antes y después del evento, y restar
ciclos es restar fasores. La corriente no sirve de referencia porque su fase cambia justamente con el evento.

Se resta el ciclo PROMEDIO de cada meseta (no muestra a muestra): los motores tienen componentes que no están enganchadas
a la red (el aire TCL, ~99 y ~199 Hz, que giran una vuelta por segundo respecto de V), y al promediar ~1 s se cancelan.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
from dataclasses import dataclass, field

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .config import Config
from .data import Dataset, CARPETAS_IGNORADAS
from .evaluate import agregar_por_grupo
from .train import armar_conjunto, costo_modelo, entrenar_final, predecir_proba

N_CICLO = 200             # puntos por ciclo: a 50 Hz equivale a fs = 10 kHz
CARPETA_EVENTOS = "eventos"
ARM_ERROR = slice(1, 25)  # H2..H25 para el error de forma


# ------------------------------------------------------------------ captura --

@dataclass
class CapturaIV:
    """Captura con corriente y tensión, cortada en ciclos sincronizados con el cruce ascendente de V."""
    ruta: str                 # relativa a la carpeta de datos
    clases: tuple             # clases originales, en el orden de `categoria`
    equipos: tuple            # uno por clase ("" si no se registró)
    lugar: str
    fs: float
    Ci: np.ndarray            # (ciclos, N_CICLO) corriente [A]
    Cv: np.ndarray            # (ciclos, N_CICLO) tensión [V]
    t: np.ndarray             # inicio de cada ciclo [s]

    @property
    def nombre(self) -> str:
        return os.path.basename(self.ruta)[:-5]

    @property
    def P(self) -> np.ndarray:          # potencia activa por ciclo [W]
        return (self.Ci * self.Cv).mean(axis=1)

    @property
    def Irms(self) -> np.ndarray:       # corriente RMS por ciclo [A]
        return np.sqrt((self.Ci ** 2).mean(axis=1))


def ciclos(i: np.ndarray, v: np.ndarray, fs: float, n: int = N_CICLO, f_red: float = 50.0):
    """Corta en los cruces ascendentes de V (interpolados) y remuestrea cada ciclo a n puntos.
    Devuelve (Ci, Cv, t_inicio). Quita la continua de ambas señales."""
    i = np.asarray(i, float) - np.mean(i)
    v = np.asarray(v, float) - np.mean(v)
    s = np.where((v[:-1] < 0) & (v[1:] >= 0))[0]
    cruces = s + (-v[s]) / (v[s + 1] - v[s])
    minimo = 0.75 * fs / f_red            # descarta cruces dobles por ruido
    buenos = []
    for c in cruces:
        if not buenos or c - buenos[-1] > minimo:
            buenos.append(c)
    buenos = np.asarray(buenos)
    k = np.arange(len(i))
    Ci, Cv = [], []
    for a, b in zip(buenos[:-1], buenos[1:]):
        q = a + (b - a) * np.arange(n) / n
        Ci.append(np.interp(q, k, i))
        Cv.append(np.interp(q, k, v))
    return np.asarray(Ci).reshape(-1, n), np.asarray(Cv).reshape(-1, n), buenos[:-1] / fs


def fasores(ciclo: np.ndarray, n_arm: int = 40) -> np.ndarray:
    """Armónicas H1..H<n_arm> de un ciclo (amplitud RMS, ángulo medido desde el cruce ascendente de V)."""
    X = np.fft.rfft(ciclo) / len(ciclo) * np.sqrt(2)
    return X[1:n_arm + 1]


def _leer(ruta: str) -> dict:
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def cargar_iv(ruta_datos: str, rel: str) -> CapturaIV | None:
    """Carga `rel` (corriente) y su `_v.json`. None si no tiene tensión o tiene menos de 2 ciclos."""
    ruta_i = os.path.join(ruta_datos, rel)
    ruta_v = ruta_i[:-5] + "_v.json"
    if not os.path.exists(ruta_v):
        return None
    pi, pv = _leer(ruta_i), _leer(ruta_v)
    clases = tuple(c.strip().lower() for c in str(pi["categoria"]).split(",") if c.strip())
    equipos = tuple(e.strip() for e in str(pi.get("equipo", "")).split(","))
    if len(equipos) != len(clases):
        equipos = ("",) * len(clases)
    fs = float(pi.get("fs", 10000))
    Ci, Cv, t = ciclos(np.asarray(pi["datos"]), np.asarray(pv["datos"]), fs)
    if len(Ci) < 2:
        return None
    return CapturaIV(rel, clases, equipos, pi.get("lugar", ""), fs, Ci, Cv, t)


def _timestamp(rel: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(os.path.basename(rel)[:15], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _norm_equipo(clase: str, equipo: str) -> str:
    """'pava_britania' en una individual y 'britania' en un evento son el mismo aparato."""
    e = equipo.strip().lower()
    return e[len(clase) + 1:] if e.startswith(clase + "_") else e


# ------------------------------------------------------------- referencias --

@dataclass
class Referencia:
    """Una captura individual con tensión: el ciclo promedio y su potencia."""
    ruta: str
    clase: str
    equipo: str
    lugar: str
    X: np.ndarray             # fasores H1..H40 del ciclo promedio
    P: float


def referencias(ruta_datos: str) -> list:
    """Individuales con tensión (fuera de las carpetas ignoradas): las referencias contra las que se compara la resta."""
    refs = []
    for ruta_v in sorted(glob.glob(os.path.join(ruta_datos, "**", "*_v.json"), recursive=True)):
        rel = os.path.relpath(ruta_v, ruta_datos)[:-7] + ".json"
        if set(rel.split(os.sep)[:-1]) & set(CARPETAS_IGNORADAS):
            continue
        cap = cargar_iv(ruta_datos, rel)
        if cap is None or len(cap.clases) != 1 or cap.clases[0] == "nada" or len(cap.Ci) < 10:
            continue
        c = cap.clases[0]
        refs.append(Referencia(rel, c, _norm_equipo(c, cap.equipos[0]), cap.lugar,
                               fasores(cap.Ci.mean(axis=0)), float(cap.P.mean())))
    return refs


def _refs_de(refs: list, clase: str, equipo: str, lugar: str) -> list:
    """Referencias del mismo aparato; si no hay, de la misma clase y lugar."""
    eq = _norm_equipo(clase, equipo)
    mismas = [r for r in refs if r.clase == clase and r.equipo == eq and eq]
    return mismas or [r for r in refs if r.clase == clase and r.lugar == lugar]


def _mas_cercana(refs: list, rel: str) -> Referencia | None:
    """La referencia más cercana en el tiempo a la captura `rel`."""
    if not refs:
        return None
    t = _timestamp(rel)
    if t is None:
        return refs[0]
    return min(refs, key=lambda r: abs(((_timestamp(r.ruta) or t) - t).total_seconds()))


def errores(D: np.ndarray, R: np.ndarray, magnitudes: np.ndarray | None = None) -> tuple:
    """(e_H1, e_arm): error relativo de la fundamental y error de H2..H25 dividido por |H1| de la referencia.
    Con `magnitudes`, el error de armónicas se calcula restando solo amplitudes (sin fase)."""
    e1 = abs(D[0] - R[0]) / abs(R[0])
    if magnitudes is None:
        dif = np.abs(D[ARM_ERROR] - R[ARM_ERROR])
    else:
        dif = np.abs(magnitudes[ARM_ERROR] - np.abs(R[ARM_ERROR]))
    return float(e1), float(np.sqrt((dif ** 2).sum()) / abs(R[0]))


def piso_individual(refs: list) -> pd.DataFrame:
    """Error entre dos individuales del mismo aparato: lo mínimo esperable para la resta."""
    filas = []
    for c, e in sorted({(r.clase, r.equipo) for r in refs}):
        rr = [r for r in refs if r.clase == c and r.equipo == e]
        pares = [errores(a.X, b.X) for k, a in enumerate(rr) for b in rr[k + 1:]]
        if pares:
            filas.append({"clase": c, "equipo": e, "capturas": len(rr),
                          "e_H1": np.median([p[0] for p in pares]), "e_arm": np.median([p[1] for p in pares])})
    return pd.DataFrame(filas)


# ----------------------------------------------------------------- eventos --

@dataclass
class Evento:
    captura: CapturaIV
    tipo: str                 # "ON" o "OFF"
    clase: str                # clase original de la carga que cambió
    equipo: str
    antes: slice              # ciclos de la meseta previa
    despues: slice            # ciclos de la meseta posterior
    dP: float                 # potencia de la carga que cambió (positiva) [W]
    P_ref: float | None       # potencia media de sus individuales [W]
    estado_despues: tuple     # clases originales activas después del evento
    referencia: Referencia | None = None
    D: np.ndarray = field(default=None, repr=False)      # fasores de la diferencia (con el signo de la carga)
    M: np.ndarray = field(default=None, repr=False)      # diferencia de amplitudes (sin fase)

    @property
    def t(self) -> float:
        return float(self.captura.t[self.despues.start])

    @property
    def ciclos_min(self) -> int:
        return min(self.antes.stop - self.antes.start, self.despues.stop - self.despues.start)

    @property
    def parcial(self) -> bool:
        """El escalón no se parece a la carga entera (una etapa de un motor, una lámpara que parpadea)."""
        return self.P_ref is not None and not 0.5 <= self.dP / self.P_ref <= 2.0

    @property
    def ciclo_delta(self) -> np.ndarray:
        """Ciclo de la carga que cambió: promedio de después − promedio de antes (al revés en un apagado)."""
        s = 1.0 if self.tipo == "ON" else -1.0
        C = self.captura.Ci
        return s * (C[self.despues].mean(axis=0) - C[self.antes].mean(axis=0))


def detectar_saltos(P: np.ndarray, umbral_w: float = 8.0, rel: float = 0.01, juntar: int = 10) -> list:
    """Ciclos donde la potencia salta más de max(umbral_w, rel·|P| mediana), agrupados si están a menos de `juntar`
    ciclos. Devuelve [(primer_ciclo, ultimo_ciclo)]."""
    umbral = max(umbral_w, rel * float(np.median(np.abs(P))))
    grupos = []
    for k in np.where(np.abs(np.diff(P)) > umbral)[0] + 1:
        if grupos and k - grupos[-1][1] <= juntar:
            grupos[-1][1] = int(k)
        else:
            grupos.append([int(k), int(k)])
    return [tuple(g) for g in grupos]


def mesetas(cap: CapturaIV, saltos: list, asentamiento: int = 15, asentamiento_motor: int = 40,
            minimo: int = 8) -> list:
    """Tramos estables entre saltos. Después de un salto con pico de arranque (motor) espera más ciclos."""
    r = cap.Irms
    bordes = [(None, 0)] + list(saltos) + [(len(r), None)]
    out = []
    for (_, fin0), (ini1, _) in zip(bordes[:-1], bordes[1:]):
        ini = 0
        if fin0:
            resto = r[fin0 + 10:ini1] if ini1 - fin0 > 12 else r[fin0:]
            pico = r[fin0:fin0 + 5].max() > 1.5 * np.median(resto)
            ini = fin0 + (asentamiento_motor if pico else asentamiento)
        fin = ini1 - 2 if ini1 < len(r) else len(r)
        if fin - ini >= minimo:
            out.append(slice(ini, fin))
    return out


def _cercania(dP: float, P_ref: float | None) -> float:
    return abs(np.log(abs(dP) / P_ref)) if P_ref and dP else np.inf


def eventos_de(cap: CapturaIV, refs: list, umbral_w: float = 8.0) -> list:
    """Eventos de una captura. Qué carga cambió se deduce del estado: la meseta inicial se asigna a la carga cuya
    individual tiene la potencia más parecida; un encendido es una carga que no estaba; un apagado, la más parecida
    entre las que estaban."""
    P = cap.P
    ms = mesetas(cap, detectar_saltos(P, umbral_w))
    if len(ms) < 2:
        return []
    Pref = {}
    for c, e in zip(cap.clases, cap.equipos):
        rr = _refs_de(refs, c, e, cap.lugar)
        Pref[c] = float(np.mean([r.P for r in rr])) if rr else None
    P0 = float(P[ms[0]].mean())
    estado = [] if P0 < umbral_w else [min(cap.clases, key=lambda c: _cercania(P0, Pref[c]))]
    out = []
    for a, b in zip(ms[:-1], ms[1:]):
        dP = float(P[b].mean() - P[a].mean())
        if abs(dP) < umbral_w:
            continue
        if dP > 0:
            tipo, cand = "ON", [c for c in cap.clases if c not in estado] or list(cap.clases)
        else:
            tipo, cand = "OFF", list(estado) or list(cap.clases)
        clase = min(cand, key=lambda c: (_cercania(dP, Pref[c]), cap.clases.index(c)))
        if tipo == "ON" and clase not in estado:
            estado.append(clase)
        elif tipo == "OFF" and clase in estado:
            estado.remove(clase)
        equipo = cap.equipos[cap.clases.index(clase)]
        ev = Evento(cap, tipo, clase, equipo, a, b, abs(dP), Pref[clase], tuple(estado))
        X_desp, X_antes = fasores(cap.Ci[b].mean(axis=0)), fasores(cap.Ci[a].mean(axis=0))
        s = 1.0 if tipo == "ON" else -1.0
        ev.D = s * (X_desp - X_antes)
        ev.M = s * (np.abs(X_desp) - np.abs(X_antes))
        ev.referencia = _mas_cercana(_refs_de(refs, clase, equipo, cap.lugar), cap.ruta)
        out.append(ev)
    return out


def cargar_eventos(ruta_datos: str, refs: list | None = None) -> tuple:
    """(capturas, eventos, referencias) de la carpeta `eventos/`."""
    refs = referencias(ruta_datos) if refs is None else refs
    capturas, evs = [], []
    for ruta in sorted(glob.glob(os.path.join(ruta_datos, CARPETA_EVENTOS, "*.json"))):
        if ruta.endswith("_v.json"):
            continue
        cap = cargar_iv(ruta_datos, os.path.relpath(ruta, ruta_datos))
        if cap is None:
            continue
        capturas.append(cap)
        evs += eventos_de(cap, refs)
    return capturas, evs, refs


def tabla_eventos(evs: list) -> pd.DataFrame:
    """Un renglón por evento: la diferencia contra la individual más cercana del mismo aparato, con fase y sin fase."""
    filas = []
    for e in evs:
        f = {"captura": e.captura.nombre, "t_s": round(e.t, 2), "tipo": e.tipo, "carga": e.clase, "equipo": e.equipo,
             "ciclos": e.ciclos_min, "dP_W": round(e.dP, 1),
             "P_ref_W": round(e.P_ref, 1) if e.P_ref else np.nan, "parcial": e.parcial}
        if e.referencia is not None:
            e1, ea = errores(e.D, e.referencia.X)
            f.update(e_H1=e1, e_arm_fase=ea, e_arm_magnitud=errores(e.D, e.referencia.X, magnitudes=e.M)[1])
        filas.append(f)
    return pd.DataFrame(filas)


def resumen_resta(tabla: pd.DataFrame) -> pd.DataFrame:
    """Mediana por carga, sin los escalones parciales."""
    t = tabla[~tabla["parcial"]].dropna(subset=["e_H1"])
    return t.groupby("carga").agg(eventos=("e_H1", "size"), e_H1=("e_H1", "median"),
                                  e_arm_fase=("e_arm_fase", "median"),
                                  e_arm_magnitud=("e_arm_magnitud", "median")).round(3)


# --------------------------------------------------------------- gráficos --

def plot_perfiles(capturas: list, evs: list, columnas: int = 3):
    """Corriente RMS por ciclo (azul) y potencia (rojo) de cada captura; en gris, el tramo descartado en cada evento."""
    filas = int(np.ceil(len(capturas) / columnas))
    fig, axs = plt.subplots(filas, columnas, figsize=(5.2 * columnas, 2.1 * filas), squeeze=False)
    for ax, cap in zip(axs.ravel(), capturas):
        ax.plot(cap.t, cap.Irms, lw=1, color="tab:blue")
        ax2 = ax.twinx()
        ax2.plot(cap.t, cap.P, lw=0.6, color="tab:red", alpha=0.6)
        for e in evs:
            if e.captura is cap:
                ax.axvspan(cap.t[e.antes.stop - 1], cap.t[e.despues.start], color="0.85", zorder=0)
        ax.set_title(f"{cap.nombre[9:]}  ({', '.join(cap.equipos)})", fontsize=8)
        ax.tick_params(labelsize=7)
        ax2.tick_params(labelsize=7, colors="tab:red")
    for ax in axs.ravel()[len(capturas):]:
        ax.axis("off")
    fig.supxlabel("tiempo [s]")
    fig.supylabel("I RMS por ciclo [A] (azul) · P [W] (rojo)")
    fig.tight_layout()
    return fig


def plot_resta(ev: Evento, n_arm: int = 15):
    """Amplitud de H1..H<n_arm>: la individual del aparato, la diferencia con fase y la diferencia de amplitudes."""
    R = ev.referencia
    k = np.arange(1, n_arm + 1)
    w = 0.27
    fig, ax = plt.subplots(figsize=(11, 3.4))
    if R is not None:
        ax.bar(k - w, np.abs(R.X[:n_arm]) * 1e3, w, color="0.55",
               label=f"individual ({os.path.basename(R.ruta)[9:15]})")
    ax.bar(k, np.abs(ev.D[:n_arm]) * 1e3, w, color="tab:blue", label="diferencia con fase")
    ax.bar(k + w, np.abs(ev.M[:n_arm]) * 1e3, w, color="tab:orange", label="diferencia de amplitudes (sin fase)")
    ax.set_yscale("log")
    ax.set_xticks(k, [f"H{x}" for x in k])
    ax.set_ylabel("amplitud RMS [mA]")
    titulo = f"{ev.captura.nombre[9:]}, t = {ev.t:.2f} s: {ev.tipo} {ev.clase} ({ev.equipo})"
    if R is not None:
        e1, ea = errores(ev.D, R.X)
        em = errores(ev.D, R.X, magnitudes=ev.M)[1]
        titulo += f"  ·  e_H1 {e1:.3f}  ·  e_arm con fase {ea:.3f}, sin fase {em:.3f}"
    ax.set_title(titulo, fontsize=9)
    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig


def tabla_asincronia(cap: CapturaIV, n_arm: int = 8) -> pd.DataFrame:
    """Por armónica: amplitud media por ciclo, amplitud del promedio de fasores y cuánto gira su ángulo por segundo.
    Una componente enganchada a la red no gira y su promedio vale lo mismo que cada ciclo; una asincrónica gira
    y su promedio tiende a cero."""
    X = np.fft.rfft(cap.Ci, axis=1) / N_CICLO * np.sqrt(2)
    dur = cap.t[-1] - cap.t[0]
    filas = []
    for h in range(1, n_arm + 1):
        fase = np.unwrap(np.angle(X[:, h]))
        filas.append({"armonica": f"H{h}", "por_ciclo_mA": np.abs(X[:, h]).mean() * 1e3,
                      "promedio_mA": abs(X[:, h].mean()) * 1e3,
                      "giro_grados_por_s": np.degrees(fase[-1] - fase[0]) / dur})
    return pd.DataFrame(filas).round(1)


def plot_asincronia(cap: CapturaIV, armonicas=(1, 2, 3, 4)):
    """Ángulo de cada armónica, ciclo a ciclo, medido desde el cruce de V."""
    X = np.fft.rfft(cap.Ci, axis=1)
    fig, ax = plt.subplots(figsize=(11, 3))
    for h in armonicas:
        ax.plot(cap.t, np.degrees(np.unwrap(np.angle(X[:, h]))), label=f"H{h}")
    ax.set(xlabel="tiempo [s]", ylabel="ángulo [°]", title=f"{cap.nombre}: ángulo de cada armónica respecto de V")
    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig


# ----------------------------------------------------------- clasificación --

def _clases_agrupadas(clases, cfg: Config) -> set:
    return {cfg.agrupar.get(c, c) for c in clases}


def clasificar_eventos(ds: Dataset, evs: list, modelos: dict, semillas: int = 3, min_ciclos: int = 11,
                       n_ciclos_delta: int = 50) -> tuple:
    """Para cada lugar con eventos entrena SIN ese lugar (casa nunca vista) y compara dos decisiones:
      - suma: el modelo clasifica la meseta posterior (lo que hace hoy el pipeline); acierta si da exactamente
        las cargas activas (umbral cfg.umbral).
      - diferencia: el modelo clasifica el ciclo de la diferencia, repetido `n_ciclos_delta` veces; como en un evento
        cambia una sola carga, se elige la clase más probable.
    Entran los eventos no parciales con al menos `min_ciclos` de cada lado (11 ciclos = 2200 muestras ≥ W).
    `modelos` = {nombre: Config}. Devuelve (detalle por evento y semilla, nodos de cada modelo)."""
    usables = [e for e in evs if not e.parcial and e.ciclos_min >= min_ciclos]
    filas, nodos = [], {}
    for lugar in sorted({e.captura.lugar for e in usables}):
        regs = [r for r in ds.registros if r.lugar != lugar]
        ds_l = Dataset(regs, sorted({c for r in regs for c in r.clases}), ds.manifest_filas, ds.manifest_hash)
        del_lugar = [e for e in usables if e.captura.lugar == lugar]
        for nombre, cfg in modelos.items():
            fs = N_CICLO * cfg.f_red
            for s in range(semillas):
                modelo, _, clases = entrenar_final(ds_l, cfg.clonar(seed=cfg.seed + s), verbose=False)
                nodos.setdefault(nombre, costo_modelo(modelo)["nodos_totales"])

                def proba(senales):
                    Y = np.zeros((len(senales), len(clases)), np.uint8)
                    G = np.array([f"{k:05d}" for k in range(len(senales))])
                    F, Yw, Gw, _ = armar_conjunto(senales, Y, G, cfg, fs)
                    Pg, _, _ = agregar_por_grupo(predecir_proba(modelo, F, len(clases)), Yw, Gw)
                    return Pg            # grupos únicos ordenados = orden de `senales`

                P_suma = proba([e.captura.Ci[e.despues].ravel().astype(np.float32) for e in del_lugar])
                P_dif = proba([np.tile(e.ciclo_delta, n_ciclos_delta).astype(np.float32) for e in del_lugar])
                for e, ps, pdif in zip(del_lugar, P_suma, P_dif):
                    real = _clases_agrupadas(e.estado_despues, cfg)
                    carga = cfg.agrupar.get(e.clase, e.clase)
                    pred_suma = {clases[j] for j in np.where(ps >= cfg.umbral)[0]}
                    j = int(np.argmax(pdif))
                    filas.append({"lugar": lugar, "modelo": nombre, "semilla": s, "captura": e.captura.nombre[9:],
                                  "t_s": round(e.t, 2), "tipo": e.tipo, "carga": carga, "vista": carga in clases,
                                  "suma_real": "+".join(sorted(real)), "suma_pred": "+".join(sorted(pred_suma)) or "-",
                                  "suma_ok": pred_suma == real, "dif_pred": clases[j], "dif_p": round(float(pdif[j]), 2),
                                  "dif_ok": clases[j] == carga})
    return pd.DataFrame(filas), nodos


def resumen_clasificacion(det: pd.DataFrame) -> pd.DataFrame:
    """Aciertos por modelo (promedio entre semillas): la suma exacta y la diferencia, en total y por carga."""
    d = det[det["vista"]]
    n_sem = d["semilla"].nunique()
    out = d.groupby("modelo").agg(eventos=("suma_ok", lambda x: len(x) // n_sem),
                                  suma_exacta=("suma_ok", "mean"), diferencia=("dif_ok", "mean"))
    por_carga = d.pivot_table(index="modelo", columns="carga", values="dif_ok", aggfunc="mean")
    por_carga.columns = [f"dif_{c}" for c in por_carga.columns]
    return out.join(por_carga).round(2)


def top1_individuales(rep) -> tuple:
    """Proxy en todo el dataset: en la validación por lugar, ¿la clase más probable de cada individual de la casa nueva
    es la correcta? Es lo que vería la placa en un evento limpio, porque la diferencia es la carga sola.
    Solo cuenta individuales de una clase (no `nada`) que el modelo de ese fold vio.
    Devuelve (acierto global, acierto por clase)."""
    tot, por = [], {}
    for r in rep.resultados:
        d = r.bloques["ind"]
        Pg, Yg, _ = agregar_por_grupo(d["P"], d["Y"], d["grupos"])
        Mg, _, _ = agregar_por_grupo(np.asarray(d["M"], float), d["Y"], d["grupos"])
        for p, y, m in zip(Pg, Yg, Mg):
            if y.sum() != 1:
                continue
            j = int(np.argmax(y))
            if m[j] < 0.5:
                continue
            ok = int(np.argmax(p)) == j
            tot.append(ok)
            por.setdefault(r.clases[j], []).append(ok)
    return float(np.mean(tot)), pd.Series({c: float(np.mean(v)) for c, v in sorted(por.items())})
