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
    """Y (n, C) multi-hot, P (n, C) probabilidades. Devuelve dict plano.
    f1_micro cuenta todos los bits juntos; f1/precision/recall_macro promedian
    por clase con peso igual, solo sobre las clases presentes en Y."""
    Y = np.asarray(Y, dtype=np.uint8)
    Yp = binarizar(P, umbral)
    if len(Y) == 0:
        return {}
    # Las métricas macro se promedian SOLO sobre las clases presentes en el bloque
    # (con soporte > 0): una clase ausente daría F1 = 0 por definición y
    # hundiría el promedio sin que el modelo haya fallado nada.
    presentes = np.where(Y.sum(axis=0) > 0)[0]
    kw = dict(average="macro", zero_division=0, labels=presentes if len(presentes) else None)
    m = {
        "n": int(len(Y)),
        "exact_match": float((Y == Yp).all(axis=1).mean()),
        "hamming": float(hamming_loss(Y, Yp)),
        "f1_micro": float(f1_score(Y, Yp, average="micro", zero_division=0)),
        "f1_macro": float(f1_score(Y, Yp, **kw)),
        "precision_macro": float(precision_score(Y, Yp, **kw)),
        "recall_macro": float(recall_score(Y, Yp, **kw)),
        "n_clases_presentes": int(len(presentes)),
    }
    if clases is not None:      # F1 por clase solo para clases presentes en el bloque
        f1c = f1_score(Y, Yp, average=None, zero_division=0)
        for j, (c, f) in enumerate(zip(clases, f1c)):
            if Y[:, j].sum() > 0:
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
        if max(f1s) > 0:            # sin ningún acierto el umbral no tiene información: queda 0.5
            out[j] = grilla[int(np.argmax(f1s))]
    return out


def umbrales_optimos_exact(Y: np.ndarray, P: np.ndarray, grilla=None, max_iter: int = 10) -> np.ndarray:
    """Umbrales por clase que maximizan el EXACT MATCH (todos los bits bien),
    por descenso coordenado: arranca de los umbrales óptimos por F1 y mueve
    una clase por vez sobre la grilla, aceptando solo mejoras estrictas.
    El exact es una métrica escalonada (1/n) con muchos empates: por eso el
    punto de partida y la aceptación estricta, para no vagar entre empates."""
    Y = np.asarray(Y, dtype=np.uint8)
    P = np.asarray(P)
    grilla = np.arange(0.3, 0.91, 0.05) if grilla is None else np.asarray(grilla)
    u = umbrales_optimos(Y, P, grilla)
    exact = lambda umb: float(((P >= umb).astype(np.uint8) == Y).all(axis=1).mean())
    mejor = exact(u)
    for _ in range(max_iter):
        mejoro = False
        for j in range(Y.shape[1]):
            if Y[:, j].sum() == 0:
                continue
            cand = u.copy()
            for g in grilla:
                cand[j] = g
                v = exact(cand)
                if v > mejor + 1e-12:
                    mejor, u[j], mejoro = v, g, True
        if not mejoro:
            break
    return u


def importancias(modelo, nombres: list) -> dict:
    """Importancia media (MDI) por feature; soporta OvR de árboles y RF nativo."""
    ests = getattr(modelo, "estimators_", None)
    if ests is None:
        ests = [modelo]
    imps = [getattr(e, "feature_importances_", None) for e in ests]
    imps = [i for i in imps if i is not None]
    if not imps or len({len(i) for i in imps}) > 1:     # franjas: cada bosque ve otros bins
        return {}
    media = np.mean(imps, axis=0)
    return dict(sorted(zip(nombres, media.tolist()), key=lambda kv: -kv[1]))


def reporte(metricas: dict, clases: list,
            bloques=(("ind", "REALES individuales"), ("comb", "REALES combinadas"),
                     ("todas", "REALES todas (ind+comb)"), ("mix", "sintéticas (mix)"))) -> str:
    """Tabla por bloque de capturas reales: por ventana, por captura (media de
    probabilidades de sus ventanas, umbral 0.5), por captura con umbrales por
    clase aprendidos en las individuales out-of-fold (+umbr; no se muestra para
    ind/todas porque ahí sería optimista) y sobre clases vistas (en lolo: sin
    contar aparatos que solo existen en la casa de test)."""
    lineas = []
    cab = ("n", "exact", "hamming", "f1_micro", "f1_macro", "prec", "rec")
    etiqueta_nivel = {"v": "por ventana", "c": "por captura", "u": "por captura +umbr",
                      "s": "clases vistas"}
    for b, titulo in bloques:
        if f"{b}_c_n" not in metricas:
            continue
        lineas.append(f"{titulo:24s}" + "".join(f"{c:>9s}" for c in cab))
        for nivel in ("v", "c", "u", "s"):
            p = f"{b}_{nivel}_"
            if p + "n" not in metricas:
                continue
            vals = (str(int(metricas[p + "n"])),
                    *(f"{metricas[p + k]:.3f}" for k in
                      ("exact_match", "hamming", "f1_micro", "f1_macro", "precision_macro", "recall_macro")))
            lineas.append(f"  {etiqueta_nivel[nivel]:22s}" + "".join(f"{v:>9s}" for v in vals))
    for b, titulo in (("ind", "individuales"), ("comb", "combinadas")):
        if any(f"{b}_c_f1_{c}" in metricas for c in clases):
            lineas.append(f"F1 por clase, {titulo} reales (por captura): " + ", ".join(
                f"{c}={metricas[f'{b}_c_f1_{c}']:.2f}" for c in clases if f"{b}_c_f1_{c}" in metricas))
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
