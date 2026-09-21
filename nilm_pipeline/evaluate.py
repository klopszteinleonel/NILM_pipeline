"""
Métricas y análisis para salida multi-label.

- Por ventana: exact-match, hamming, F1 micro/macro, precision/recall macro,
  F1 por clase.
- Por captura (decisión H): se promedian las probabilidades de todas las
  ventanas de un mismo grupo (captura o mezcla) y se umbraliza. Es lo que
  haría el firmware con suavizado temporal.
- Umbrales por clase, importancias de features, curva de robustez a SNR.
"""
from __future__ import annotations

import numpy as np
from sklearn.metrics import (f1_score, hamming_loss, multilabel_confusion_matrix,
                             precision_score, recall_score)


def binarizar(P: np.ndarray, umbral: float | np.ndarray) -> np.ndarray:
    return (np.asarray(P) >= np.asarray(umbral)).astype(np.uint8)


def metricas_multilabel(Y: np.ndarray, P: np.ndarray, umbral: float | np.ndarray,
                        clases: list | None = None, prefijo: str = "") -> dict:
    """Y (n, C) multi-hot, P (n, C) probabilidades. Devuelve dict plano."""
    Y = np.asarray(Y, dtype=np.uint8)
    Yp = binarizar(P, umbral)
    if len(Y) == 0:
        return {}
    m = {
        "n": int(len(Y)),
        "exact_match": float((Y == Yp).all(axis=1).mean()),
        "hamming": float(hamming_loss(Y, Yp)),
        "f1_micro": float(f1_score(Y, Yp, average="micro", zero_division=0)),
        "f1_macro": float(f1_score(Y, Yp, average="macro", zero_division=0)),
        "precision_macro": float(precision_score(Y, Yp, average="macro", zero_division=0)),
        "recall_macro": float(recall_score(Y, Yp, average="macro", zero_division=0)),
    }
    if clases is not None:
        f1c = f1_score(Y, Yp, average=None, zero_division=0)
        for c, f in zip(clases, f1c):
            m[f"f1_{c}"] = float(f)
    return {prefijo + k: v for k, v in m.items()}


def agregar_por_grupo(P: np.ndarray, Y: np.ndarray, grupos: np.ndarray,
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Promedia P sobre las ventanas de cada grupo. Devuelve (P_g, Y_g, grupos_unicos)."""
    grupos = np.asarray(grupos)
    unicos, inv = np.unique(grupos, return_inverse=True)
    Pg = np.zeros((len(unicos), P.shape[1]), dtype=np.float64)
    np.add.at(Pg, inv, P)
    Pg /= np.bincount(inv)[:, None]
    Yg = np.asarray(Y)[np.unique(inv, return_index=True)[1]]
    return Pg.astype(np.float32), Yg.astype(np.uint8), unicos


def matrices_por_clase(Y: np.ndarray, P: np.ndarray, umbral) -> np.ndarray:
    """(C, 2, 2) con [[TN, FP], [FN, TP]] por clase."""
    return multilabel_confusion_matrix(np.asarray(Y, dtype=np.uint8), binarizar(P, umbral))


def umbrales_optimos(Y: np.ndarray, P: np.ndarray, grilla=None) -> np.ndarray:
    """Umbral por clase que maximiza F1 sobre predicciones out-of-fold (C,).
    Piso 0.3: umbrales más bajos (clases con 1–2 capturas) generan falsos
    positivos en mezclas; medido sobre combinadas reales, 0.3 rinde mejor que 0.1."""
    Y = np.asarray(Y, dtype=np.uint8)
    grilla = np.arange(0.3, 0.91, 0.05) if grilla is None else np.asarray(grilla)
    out = np.full(Y.shape[1], 0.5)
    for j in range(Y.shape[1]):
        if Y[:, j].sum() == 0:
            continue
        f1s = [f1_score(Y[:, j], (P[:, j] >= u).astype(int), zero_division=0) for u in grilla]
        out[j] = grilla[int(np.argmax(f1s))]
    return out


def importancias(modelo, nombres: list) -> dict:
    """Importancia media (MDI) por feature; soporta OvR de árboles y RF nativo."""
    ests = getattr(modelo, "estimators_", None)
    if ests is None:
        ests = [modelo]
    imps = [getattr(e, "feature_importances_", None) for e in ests]
    imps = [i for i in imps if i is not None]
    if not imps:
        return {}
    media = np.mean(imps, axis=0)
    return dict(sorted(zip(nombres, media.tolist()), key=lambda kv: -kv[1]))


def reporte(metricas: dict, clases: list, bloques=("ind", "mix", "real")) -> str:
    lineas = []
    cab = ("bloque", "n", "exact", "hamming", "f1_micro", "f1_macro", "prec", "rec")
    lineas.append(f"{cab[0]:14s}" + "".join(f"{c:>9s}" for c in cab[1:]))
    etiqueta_nivel = {"v": "ventana", "c": "captura", "u": "captura+umbr"}
    for b in bloques:
        for nivel in ("v", "c", "u"):
            p = f"{b}_{nivel}_"
            if p + "n" not in metricas:
                continue
            fila = (f"{b}/{etiqueta_nivel[nivel]}",
                    str(metricas[p + "n"]),
                    *(f"{metricas[p + k]:.3f}" for k in
                      ("exact_match", "hamming", "f1_micro", "f1_macro", "precision_macro", "recall_macro")))
            lineas.append(f"{fila[0]:14s}" + "".join(f"{v:>9s}" for v in fila[1:]))
    if any(f"ind_c_f1_{c}" in metricas for c in clases):
        lineas.append("F1 por clase (individuales, por captura): " + ", ".join(
            f"{c}={metricas.get(f'ind_c_f1_{c}', float('nan')):.2f}" for c in clases))
    costo = {k: metricas[k] for k in ("n_features", "n_arboles", "nodos_totales", "prof_max") if k in metricas}
    if costo:
        lineas.append("costo modelo: " + ", ".join(f"{k}={v}" for k, v in costo.items()))
    return "\n".join(lineas)


def reporte_combinaciones(bloque: dict, clases: list, umbral, max_filas: int = 40) -> str:
    """Por cada combinación real de cargas: cuántas capturas, qué se predijo
    (con `umbral`, escalar o por clase) y la probabilidad media por clase."""
    from collections import Counter
    Pg, Yg, _ = agregar_por_grupo(bloque["P"], bloque["Y"], bloque["grupos"])
    Yp = binarizar(Pg, umbral)
    etiqueta = lambda fila: ",".join(c for c, b in zip(clases, fila) if b) or "(nada)"
    verdad = np.array([etiqueta(y) for y in Yg])
    lineas = [f"{'combinación real':26s} {'n':>3s}  predicciones (umbral aplicado)"]
    for combo in sorted(set(verdad), key=lambda c: (-int((verdad == c).sum()), c))[:max_filas]:
        idx = np.where(verdad == combo)[0]
        pred = Counter(etiqueta(Yp[i]) for i in idx)
        pm = Pg[idx].mean(axis=0)
        activas = set(combo.split(","))
        detalle = ", ".join(f"{c}={pm[j]:.2f}" for j, c in enumerate(clases)
                            if c in activas or pm[j] >= 0.3)
        exact = sum(1 for i in idx if etiqueta(Yp[i]) == combo)
        lineas.append(f"{combo:26s} {len(idx):3d}  exactas={exact}/{len(idx)}  "
                      + "; ".join(f"{k}×{v}" for k, v in pred.most_common(4)))
        lineas.append(f"{'':26s}      proba media: {detalle}")
    return "\n".join(lineas)
