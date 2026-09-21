"""
Entrenamiento y validación cruzada por grupos con mezcla dentro del fold.

Flujo por fold (ver AGENTS.md):
    train: individuales(train) + mixes(train) -> filtro -> ventanas -> DC/norm
           -> augment -> features -> fit
    test:  tres bloques evaluados con el mismo modelo
             ind  = individuales(test) (+ "sin carga")
             mix  = mixes sintéticos generados SOLO desde individuales(test)
             real = combinadas reales (fuera de los folds; se promedian las
                    probabilidades de los modelos de todos los folds)

Anti-leakage: el split es por captura (grupo) y se hace ANTES de mezclar y
aumentar. La CV se estratifica por la clase de la individual (y 1-D).

Con cv="logo" el bloque `mix` queda vacío (una sola captura de test no
alcanza para mezclar): LOGO mide individuales; para multi-label usar sgkf5
o combinadas reales.

Modelo exportable = OneVsRest(RandomForest): un bosque binario por clase
(emlearn no exporta multi-output). `multi_salida="nativo"` (RF/ET
multi-output de sklearn) queda como referencia.
"""
from __future__ import annotations

import json
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sklearn.ensemble import (ExtraTreesClassifier, HistGradientBoostingClassifier,
                              RandomForestClassifier)
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import LeaveOneGroupOut, StratifiedGroupKFold
from sklearn.multiclass import OneVsRestClassifier
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from .augment import aumentar_ventanas, copias_por_ventana
from .config import Config
from .data import TIPO_COMBINADA, Dataset
from .evaluate import agregar_por_grupo, importancias, metricas_multilabel
from .features import extraer_features, nombres_features
from .mix import sintetizar_combinadas
from .preprocess import preprocesar_captura, preprocesar_ventanas
from .segment import segmentar_dataset

_ARBOLES = ("rf", "et")


# ----------------------------------------------------------------- modelo --

def construir_modelo(cfg: Config, seed: int | None = None):
    seed = cfg.seed if seed is None else seed
    hp = dict(cfg.hiperparams)
    if cfg.modelo == "rf":
        base = RandomForestClassifier(n_estimators=cfg.n_estimators, max_depth=cfg.max_depth,
                                      class_weight=cfg.class_weight, random_state=seed,
                                      n_jobs=-1, **hp)
    elif cfg.modelo == "et":
        base = ExtraTreesClassifier(n_estimators=cfg.n_estimators, max_depth=cfg.max_depth,
                                    class_weight=cfg.class_weight, random_state=seed,
                                    n_jobs=-1, **hp)
    elif cfg.modelo == "hgb":
        base = HistGradientBoostingClassifier(max_iter=cfg.n_estimators, max_depth=cfg.max_depth,
                                              class_weight=cfg.class_weight, random_state=seed, **hp)
    elif cfg.modelo == "svm":
        base = make_pipeline(StandardScaler(), SVC(probability=True, class_weight=cfg.class_weight,
                                                   random_state=seed, **hp))
    elif cfg.modelo == "knn":
        base = make_pipeline(StandardScaler(), KNeighborsClassifier(**(hp or {"n_neighbors": 5})))
    elif cfg.modelo == "logreg":
        base = make_pipeline(StandardScaler(), LogisticRegression(class_weight=cfg.class_weight,
                                                                  max_iter=2000, random_state=seed, **hp))
    else:
        raise ValueError(cfg.modelo)

    if cfg.multi_salida == "nativo":
        if cfg.modelo in _ARBOLES:
            return base
        warnings.warn(f"{cfg.modelo} no soporta multi-output nativo; se usa OvR")
    return OneVsRestClassifier(base, n_jobs=1)


def predecir_proba(modelo, F: np.ndarray, n_clases: int) -> np.ndarray:
    """Probabilidad de 'activa' por clase, (n, C), para OvR o multi-output nativo."""
    P = modelo.predict_proba(F)
    if isinstance(P, list):                       # RF/ET multi-output nativo
        cols = []
        for j, pj in enumerate(P):
            clases_j = modelo.classes_[j]
            cols.append(pj[:, list(clases_j).index(1)] if 1 in clases_j else np.zeros(len(F)))
        return np.column_stack(cols).astype(np.float32)
    P = np.asarray(P, dtype=np.float32)
    if P.shape[1] != n_clases:
        raise ValueError(f"predict_proba devolvió {P.shape}, se esperaban {n_clases} clases")
    return P


def costo_modelo(modelo) -> dict:
    """n_arboles, nodos_totales y prof_max de un OvR o RF/ET nativo (0 si no hay árboles)."""
    arboles = []
    pendientes = [modelo]
    while pendientes:
        e = pendientes.pop()
        if hasattr(e, "tree_"):
            arboles.append(e)
        else:
            pendientes += list(getattr(e, "estimators_", []))
    return {
        "n_arboles": len(arboles),
        "nodos_totales": int(sum(a.tree_.node_count for a in arboles)),
        "prof_max": int(max((a.tree_.max_depth for a in arboles), default=0)),
    }


# --------------------------------------------------------------- conjuntos --

def armar_conjunto(senales: list, Y: np.ndarray, grupos: np.ndarray, cfg: Config, fs: float,
                   ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list]:
    """Conjunto de evaluación: capturas -> filtro -> ventanas -> DC/norm -> features.
    Devuelve (F, Yw, Gw, nombres)."""
    senales = [preprocesar_captura(x, fs, cfg) for x in senales]
    X, Yw, Gw, _ = segmentar_dataset(senales, Y, grupos, cfg)
    nombres = nombres_features(cfg)
    if len(X) == 0:
        return (np.empty((0, len(nombres)), np.float32),
                np.empty((0, np.asarray(Y).shape[1]), np.uint8), Gw, nombres)
    V, escala = preprocesar_ventanas(X, cfg)
    F, nombres = extraer_features(V, escala, cfg, fs)
    return F, Yw, Gw, nombres


def armar_conjunto_train(senales: list, Y: np.ndarray, grupos: np.ndarray, cfg: Config, fs: float,
                         rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray, list]:
    """Como armar_conjunto pero con augment. Las copias aumentadas heredan la
    escala (factor de normalización) de su ventana original."""
    senales = [preprocesar_captura(x, fs, cfg) for x in senales]
    X, Yw, Gw, idx = segmentar_dataset(senales, Y, grupos, cfg)
    V, escala = preprocesar_ventanas(X, cfg)
    if cfg.aug_n > 0:
        origen = np.repeat(np.arange(len(V)), copias_por_ventana(Yw, cfg))
        V, Yw, Gw, idx = aumentar_ventanas(V, Yw, Gw, idx, cfg, rng, fs)
        escala = np.concatenate([escala, escala[origen]])
    F, nombres = extraer_features(V, escala, cfg, fs)
    return F, Yw, Gw, nombres


def mezclas_de(senales: list, Y: np.ndarray, grupos: np.ndarray, clases: list, cfg: Config,
               rng: np.random.Generator, total: int, prefijo: str):
    """Mixes sintéticos desde un subconjunto de capturas, o None si no hay
    al menos 2 clases con individuales."""
    Yi = np.asarray(Y)
    individuales = Yi[Yi.sum(axis=1) == 1]
    if total <= 0 or len(np.unique(individuales.argmax(axis=1))) < 2:
        return None
    return sintetizar_combinadas(senales, Y, grupos, clases, cfg, rng, total, prefijo)


# -------------------------------------------------------------------- CV ----

def _estratos(Y: np.ndarray) -> np.ndarray:
    """Etiqueta 1-D para estratificar: clase de la individual, C para 'sin carga'."""
    Y = np.asarray(Y)
    return np.where(Y.sum(axis=1) > 0, Y.argmax(axis=1), Y.shape[1])


def dividir(Y: np.ndarray, grupos: np.ndarray, cfg: Config):
    """Generador de (train_idx, test_idx) sobre individuales (+ sin carga)."""
    y = _estratos(Y)
    if cfg.cv == "logo":
        return LeaveOneGroupOut().split(np.zeros(len(y)), y, grupos)
    sgkf = StratifiedGroupKFold(n_splits=cfg.n_splits, shuffle=True, random_state=cfg.seed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return list(sgkf.split(np.zeros(len(y)), y, grupos))


@dataclass
class Resultado:
    cfg: Config
    clases: list
    nombres_features: list
    metricas: dict
    bloques: dict = field(default_factory=dict)   # nombre -> dict(P, Y, grupos)
    importancias: dict = field(default_factory=dict)
    segundos: float = 0.0


def _bloque_vacio() -> dict:
    return {"P": [], "Y": [], "grupos": []}


def _acumular(bloque: dict, P, Y, G):
    bloque["P"].append(P); bloque["Y"].append(Y); bloque["grupos"].append(G)


def _cerrar(bloque: dict) -> dict:
    if not bloque["P"]:
        return {"P": np.empty((0, 0)), "Y": np.empty((0, 0), np.uint8), "grupos": np.empty(0)}
    return {k: np.concatenate(v) for k, v in bloque.items()}


def run_experiment(ds: Dataset, cfg: Config, leaderboard: str | Path | None = None,
                   verbose: bool = True) -> Resultado:
    """Validación cruzada completa según cfg. Devuelve métricas por bloque
    (ind/mix/real) a nivel ventana (_v_) y captura (_c_), y costo del modelo."""
    t0 = time.time()
    fs = ds.fs()
    clases = ds.clases
    C = len(clases)
    reales = ds.filtrar(TIPO_COMBINADA)
    base = Dataset([r for r in ds.registros if r.tipo != TIPO_COMBINADA], clases)
    sen, Y, G = base.senales(), base.y(), base.grupos()
    sen_r, Y_r, G_r = reales.senales(), reales.y(), reales.grupos()

    bloques = {b: _bloque_vacio() for b in ("ind", "mix")}
    P_real_acum, F_real = None, None
    nombres, imps, costo = nombres_features(cfg), [], {}

    folds = list(dividir(Y, G, cfg))
    for k, (tr, te) in enumerate(folds):
        rng = np.random.default_rng(cfg.seed + k)
        sen_tr, Y_tr, G_tr = [sen[i] for i in tr], Y[tr], G[tr]
        sen_te, Y_te, G_te = [sen[i] for i in te], Y[te], G[te]

        # --- train: individuales + mixes, con augment
        m = mezclas_de(sen_tr, Y_tr, G_tr, clases, cfg, rng, cfg.mix_total if cfg.mix_habilitado else 0,
                       f"mixtr{k}")
        if m is not None:
            sen_tr, Y_tr, G_tr = sen_tr + m.senales, np.vstack([Y_tr, m.Y]), np.concatenate([G_tr, m.grupos])
        F_tr, Yw_tr, _, nombres = armar_conjunto_train(sen_tr, Y_tr, G_tr, cfg, fs, rng)
        modelo = construir_modelo(cfg, cfg.seed + k)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            modelo.fit(F_tr, Yw_tr)
        costo = costo_modelo(modelo)
        imps.append(importancias(modelo, nombres))

        # --- test: individuales
        F_te, Yw_te, Gw_te, _ = armar_conjunto(sen_te, Y_te, G_te, cfg, fs)
        _acumular(bloques["ind"], predecir_proba(modelo, F_te, C), Yw_te, Gw_te)

        # --- test: mixes sintéticos SOLO desde individuales de test
        if cfg.mix_habilitado and cfg.mix_test:
            mt = mezclas_de(sen_te, Y_te, G_te, clases, cfg, rng, cfg.mix_test_total, f"mixte{k}")
            if mt is not None:
                F_mt, Yw_mt, Gw_mt, _ = armar_conjunto(mt.senales, mt.Y, mt.grupos, cfg, fs)
                _acumular(bloques["mix"], predecir_proba(modelo, F_mt, C), Yw_mt, Gw_mt)

        # --- combinadas reales: promedio de los modelos de todos los folds
        if len(sen_r):
            if F_real is None:
                F_real, Yw_r, Gw_r, _ = armar_conjunto(sen_r, Y_r, G_r, cfg, fs)
                P_real_acum = np.zeros((len(F_real), C), np.float64)
            P_real_acum += predecir_proba(modelo, F_real, C)

        if verbose:
            print(f"  fold {k + 1}/{len(folds)}: train {len(F_tr)} ventanas, "
                  f"test ind {len(F_te)} | {time.time() - t0:.0f} s")

    bl = {b: _cerrar(v) for b, v in bloques.items()}
    if F_real is not None:
        bl["real"] = {"P": (P_real_acum / len(folds)).astype(np.float32), "Y": Yw_r, "grupos": Gw_r}

    met = {}
    for b, d in bl.items():
        if len(d["Y"]) == 0:
            continue
        met.update(metricas_multilabel(d["Y"], d["P"], cfg.umbral, clases, f"{b}_v_"))
        Pg, Yg, _ = agregar_por_grupo(d["P"], d["Y"], d["grupos"])
        met.update(metricas_multilabel(Yg, Pg, cfg.umbral, clases, f"{b}_c_"))
    met.update(costo)
    met["n_features"] = len(nombres)
    met["n_folds"] = len(folds)
    met["segundos"] = round(time.time() - t0, 1)

    imp_media = {}
    if imps and imps[0]:
        for n in nombres:
            imp_media[n] = float(np.mean([i.get(n, 0.0) for i in imps]))
        imp_media = dict(sorted(imp_media.items(), key=lambda kv: -kv[1]))

    res = Resultado(cfg, clases, nombres, met, bl, imp_media, met["segundos"])
    if leaderboard is not None:
        registrar(res, leaderboard, ds.manifest_hash)
    return res


def entrenar_final(ds: Dataset, cfg: Config, verbose: bool = True):
    """Entrena con TODAS las individuales (+ mixes + augment) para exportar.
    Devuelve (modelo, nombres_features, clases)."""
    fs = ds.fs()
    base = Dataset([r for r in ds.registros if r.tipo != TIPO_COMBINADA], ds.clases)
    sen, Y, G = base.senales(), base.y(), base.grupos()
    rng = np.random.default_rng(cfg.seed)
    m = mezclas_de(sen, Y, G, ds.clases, cfg, rng, cfg.mix_total if cfg.mix_habilitado else 0, "mixfin")
    if m is not None:
        sen, Y, G = sen + m.senales, np.vstack([Y, m.Y]), np.concatenate([G, m.grupos])
    F, Yw, _, nombres = armar_conjunto_train(sen, Y, G, cfg, fs, rng)
    modelo = construir_modelo(cfg)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        modelo.fit(F, Yw)
    if verbose:
        print(f"modelo final: {len(F)} ventanas, {len(nombres)} features, {costo_modelo(modelo)}")
    return modelo, nombres, ds.clases


# ------------------------------------------------------------ leaderboard --

def registrar(res: Resultado, ruta_csv: str | Path, manifest_hash: str = "") -> None:
    """Agrega una fila (config + métricas) al leaderboard CSV."""
    import pandas as pd
    fila = {"nombre": res.cfg.nombre(), "fecha": time.strftime("%Y-%m-%d %H:%M:%S"),
            "manifest": manifest_hash[:10], **res.metricas, "config": res.cfg.to_json()}
    ruta = Path(ruta_csv)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    nuevo = pd.DataFrame([fila])
    if ruta.exists():
        nuevo = pd.concat([pd.read_csv(ruta), nuevo], ignore_index=True)
    nuevo.to_csv(ruta, index=False)


def cargar_leaderboard(ruta_csv: str | Path, ordenar_por: str = "mix_c_f1_macro"):
    import pandas as pd
    df = pd.read_csv(ruta_csv)
    if ordenar_por in df.columns:
        df = df.sort_values(ordenar_por, ascending=False)
    return df


# ------------------------------------------------------------- robustez ----

def robustez_snr(modelo, ds: Dataset, cfg: Config, snrs=(40, 30, 20, 15, 10, 5),
                 seed: int = 0) -> dict:
    """F1 micro (por captura) sobre las individuales con ruido a distintos SNR.
    Nota: usa las mismas capturas con las que puede haberse entrenado `modelo`;
    sirve para medir degradación relativa, no rendimiento absoluto."""
    fs = ds.fs()
    base = Dataset([r for r in ds.registros if r.tipo != TIPO_COMBINADA], ds.clases)
    sen, Y, G = base.senales(), base.y(), base.grupos()
    out = {}
    for snr in snrs:
        c = cfg.clonar(aug_n=1, aug_balance=False, snr_db=(float(snr), float(snr)),
                       shift=False, escala=(1.0, 1.0), hum50=False, jitter_red=False)
        F, Yw, Gw, _ = armar_conjunto_train(sen, Y, G, c, fs, np.random.default_rng(seed))
        n0 = len(F) // 2
        F, Yw, Gw = F[n0:], Yw[n0:], Gw[n0:]          # solo las copias con ruido
        Pg, Yg, _ = agregar_por_grupo(predecir_proba(modelo, F, len(ds.clases)), Yw, Gw)
        out[snr] = metricas_multilabel(Yg, Pg, cfg.umbral)["f1_micro"]
    return out
