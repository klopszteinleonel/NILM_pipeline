"""
Síntesis de señales combinadas (varias cargas a la vez) a partir de capturas
individuales. Las corrientes de cargas en paralelo se suman (superposición
sobre la misma red), así que la mezcla es una suma en el dominio del tiempo
con escala aleatoria (±5 %) por carga.

Fase (`mix_fase`): cada captura individual arranca en una fase de red
arbitraria, pero en la realidad todas las cargas cuelgan de la misma tensión.
Con "alineada" se rota cada carga para que su fundamental de f_red quede en
fase 0 (opcionalmente ± `mix_jitter_fase` grados): las amplitudes resultantes
caen cerca de la suma lineal, como en las combinadas reales medidas
(aire+horno real 8.3–8.6 A vs 8.27 lineal / 6.2 cuadrática). Con "aleatoria"
el shift es uniforme y la amplitud cae en cualquier punto entre la suma
cuadrática y la lineal.

El fondo (ruido, cargas parásitas) ya está implícito en cada captura
individual; no se suma nada extra.

Anti-leakage: esta función NO sabe de splits. El llamador le pasa solo las
capturas de train (o solo las de test) del fold, y las mezclas resultantes
heredan un grupo propio ("mix_0001"). `Mezclas.origenes` guarda los grupos
fuente de cada mezcla para poder auditarlo.

Sesgo grande+chica: en NILM el caso difícil es una carga chica (heladera, tele
~0.45 A) enterrada en una grande (aire, calefon 2–6 A). Una fracción
`mix_sesgo_grande_chica` de las mezclas fuerza ese escenario.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import Config


@dataclass
class Mezclas:
    senales: list                       # list[np.ndarray float32]
    Y: np.ndarray                       # (n, n_clases) uint8
    grupos: np.ndarray                  # (n,) str
    origenes: list = field(default_factory=list)  # list[tuple[str, ...]] grupos fuente


def rms_por_clase(senales: list, Y: np.ndarray, clases: list) -> dict[str, float]:
    """RMS mediano de las capturas individuales de cada clase (solo clases presentes)."""
    Y = np.asarray(Y)
    out = {}
    for j, c in enumerate(clases):
        idx = np.where((Y.sum(axis=1) == 1) & (Y[:, j] == 1))[0]
        if len(idx):
            out[c] = float(np.median([np.sqrt(np.mean(senales[i] ** 2)) for i in idx]))
    return out


def fase_fundamental(x: np.ndarray, fs: float, f_red: float) -> float:
    """Fase [rad] de la componente de f_red al inicio de la señal (DFT en f_red)."""
    n = np.arange(len(x))
    return float(np.angle(np.sum(x * np.exp(-2j * np.pi * f_red * n / fs))))


def alinear_fase(x: np.ndarray, fs: float, f_red: float, fase_objetivo: float = 0.0) -> np.ndarray:
    """Rota circularmente x para que su fundamental de f_red tenga la fase pedida."""
    d = (fase_objetivo - fase_fundamental(x, fs, f_red)) * fs / (2 * np.pi * f_red)
    return np.roll(x, -int(round(d)))


def combinar(senales_sel: list, rng: np.random.Generator, mix_escala: tuple, fs: float,
             f_red: float = 50.0, modo: str = "alineada", jitter_grados: float = 0.0,
             shift: bool = True) -> np.ndarray:
    """Suma en tiempo de varias señales con escala aleatoria por carga.
    modo="alineada": fundamentales en fase 0 ± jitter; "aleatoria": shift uniforme.
    Se recortan a la longitud mínima común."""
    n = min(len(s) for s in senales_sel)
    out = np.zeros(n, dtype=np.float32)
    for s in senales_sel:
        s = np.asarray(s[:n], dtype=np.float32)
        if shift:
            if modo == "alineada":
                objetivo = np.deg2rad(rng.uniform(-jitter_grados, jitter_grados)) if jitter_grados > 0 else 0.0
                s = alinear_fase(s, fs, f_red, objetivo)
            else:
                s = np.roll(s, int(rng.integers(0, n)))
        out += s * np.float32(rng.uniform(*mix_escala))
    return out


def _tercios(rms: dict) -> tuple[list, list]:
    """Clases 'grandes' (tercio superior por RMS) y 'chicas' (tercio inferior)."""
    if len(rms) < 2:
        return [], []
    orden = sorted(rms, key=rms.get)
    k = max(1, len(orden) // 3)
    return orden[-k:], orden[:k]


def sintetizar_combinadas(senales: list, Y: np.ndarray, grupos: np.ndarray, clases: list,
                          cfg: Config, rng: np.random.Generator, total: int,
                          prefijo: str = "mix", fs: float = 10000.0) -> Mezclas:
    """Genera `total` mezclas de K ∈ mix_k cargas desde capturas individuales.

    - Solo usa capturas con exactamente una clase activa como fuente de cargas
      (las "sin carga" no participan: el fondo ya está en cada captura).
    - Etiqueta = OR de los multi-hot; grupo = f"{prefijo}_{i:04d}".
    """
    Y = np.asarray(Y, dtype=np.uint8)
    grupos = np.asarray(grupos)
    activas = Y.sum(axis=1)
    por_clase = {c: np.where((activas == 1) & (Y[:, j] == 1))[0]
                 for j, c in enumerate(clases)}
    por_clase = {c: idx for c, idx in por_clase.items() if len(idx)}
    disponibles = list(por_clase)
    if len(disponibles) < 2:
        raise ValueError(f"se necesitan >= 2 clases con capturas individuales, hay {len(disponibles)}")

    grandes, chicas = _tercios(rms_por_clase(senales, Y, clases))
    k_min, k_max = cfg.mix_k

    out_senales, out_y, out_grupos, out_orig = [], [], [], []
    for i in range(total):
        K = int(rng.integers(k_min, k_max + 1))
        if not cfg.mix_misma_clase:
            K = min(K, len(disponibles))

        elegidas = []
        if grandes and chicas and rng.random() < cfg.mix_sesgo_grande_chica:
            g = str(rng.choice(grandes))
            c = str(rng.choice([x for x in chicas if x != g] or chicas))
            elegidas = [g, c] if g != c else [g]
        resto = [x for x in disponibles if cfg.mix_misma_clase or x not in elegidas]
        faltan = K - len(elegidas)
        if faltan > 0:
            extra = rng.choice(resto, size=faltan, replace=cfg.mix_misma_clase)
            elegidas += [str(x) for x in np.atleast_1d(extra)]

        idx = [int(rng.choice(por_clase[c])) for c in elegidas]

        out_senales.append(combinar([senales[j] for j in idx], rng, cfg.mix_escala, fs,
                                    cfg.f_red, cfg.mix_fase, cfg.mix_jitter_fase))
        out_y.append(np.bitwise_or.reduce(Y[idx], axis=0))
        out_grupos.append(f"{prefijo}_{i:04d}")
        out_orig.append(tuple(str(grupos[j]) for j in idx))

    return Mezclas(out_senales, np.stack(out_y).astype(np.uint8),
                   np.array(out_grupos), out_orig)
