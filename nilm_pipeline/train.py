"""
Entrenamiento y validación cruzada por grupos con mezcla dentro del fold.

Flujo por fold (ver AGENTS.md):
    train: individuales(train) + mixes(train) -> filtro -> ventanas -> DC/norm
           -> augment -> features -> fit
    test:  bloques de capturas REALES evaluados con el mismo modelo
             ind   = individuales reales (+ "sin carga") del fold de test
             comb  = combinadas reales
             todas = ind + comb
             mix   = (opcional, mix_test=True) mezclas sintéticas de test

Validación (cfg.cv):
    lolo  (default) leave-one-lugar-out: el modelo nunca vio la casa de test;
          evalúa las individuales y las combinadas de ESE lugar. Es la métrica
          del objetivo (funcionar en casas nuevas).
    sgkf5 StratifiedGroupKFold agrupado por cfg.cv_grupo (ráfaga por default:
          capturas casi idénticas nunca quedan repartidas entre train y test);
          las combinadas las evalúan todos los folds (promedio de probabilidades).
    logo  leave-one-group-out con el mismo agrupamiento.

run_repetido: varias semillas → media ± desvío; comparar: comparación pareada.
evaluar_test_congelado: evaluación única sobre cfg.test_congelado.

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
from .evaluate import (agregar_por_grupo, importancias, metricas_multilabel, umbrales_optimos,
                       umbrales_optimos_exact)
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
    nombres = nombres_features(cfg, fs)
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
               rng: np.random.Generator, total: int, prefijo: str, fs: float):
    """Mixes sintéticos desde un subconjunto de capturas, o None si no hay
    al menos 2 clases con individuales."""
    Yi = np.asarray(Y)
    individuales = Yi[Yi.sum(axis=1) == 1]
    if total <= 0 or len(np.unique(individuales.argmax(axis=1))) < 2:
        return None
    return sintetizar_combinadas(senales, Y, grupos, clases, cfg, rng, total, prefijo, fs)


def preparar_dataset(ds: Dataset, cfg: Config, verbose: bool = True,
                     incluir_congelado: bool = False) -> Dataset:
    """Aplica cfg.agrupar, saca cfg.excluir_clases, excluye clases con < cfg.min_capturas capturas o
    < cfg.min_rafagas ráfagas individuales, y (salvo incluir_congelado) aparta
    los lugares/sesiones de cfg.test_congelado."""
    ds = ds.agrupar_clases(cfg.agrupar)
    if cfg.test_congelado and not incluir_congelado:
        cong = set(cfg.test_congelado)
        regs = [r for r in ds.registros if r.lugar not in cong and r.sesion not in cong]
        ds = Dataset(regs, sorted({c for r in regs for c in r.clases}), ds.manifest_filas,
                     ds.manifest_hash, ds.problemas)
    if cfg.excluir_clases:
        fuera = set(cfg.excluir_clases)
        regs = [r for r in ds.registros if not set(r.clases) & fuera]
        ds = Dataset(regs, sorted({c for r in regs for c in r.clases}), ds.manifest_filas,
                     ds.manifest_hash, ds.problemas)
    ds, raras = ds.excluir_clases_raras(cfg.min_capturas, cfg.min_rafagas)
    if raras and verbose:
        print(f"  clases excluidas (< {cfg.min_capturas} capturas o < {cfg.min_rafagas} ráfagas): {raras}")
    return ds


# -------------------------------------------------------------------- CV ----

def _estratos(Y: np.ndarray) -> np.ndarray:
    """Etiqueta 1-D para estratificar: clase de la individual, C para 'sin carga'."""
    Y = np.asarray(Y)
    return np.where(Y.sum(axis=1) > 0, Y.argmax(axis=1), Y.shape[1])


def _grupos_cv(base: Dataset, cfg: Config) -> np.ndarray:
    return {"rafaga": base.rafagas, "sesion": base.sesiones, "captura": base.grupos}[cfg.cv_grupo]()


def dividir(base: Dataset, reales: Dataset, cfg: Config) -> list:
    """Folds como (train_idx, test_idx, test_comb_idx):
    train/test indexan las individuales (+ sin carga) de `base`; test_comb indexa
    las combinadas reales que evalúa el modelo de ese fold.
      lolo : un fold por lugar; se entrena sin ese lugar y se evalúan SUS
             individuales y SUS combinadas (casa nunca vista).
      sgkf5/logo: agrupado por cfg.cv_grupo; las combinadas las evalúan todos los
             folds y se promedian (estimación dentro de las casas conocidas)."""
    n, n_r = len(base.registros), len(reales.registros)
    if cfg.cv == "lolo":
        lug, lug_r = base.lugares(), reales.lugares()
        lugares = sorted(set(lug) | set(lug_r))
        if len(lugares) < 2:
            raise ValueError("cv='lolo' necesita capturas de al menos 2 lugares")
        folds = []
        for L in lugares:
            tr, te = np.where(lug != L)[0], np.where(lug == L)[0]
            te_c = np.where(lug_r == L)[0]
            if len(tr) and (len(te) or len(te_c)):
                folds.append((tr, te, te_c))
        return folds
    y, grupos, todas = _estratos(base.y()), _grupos_cv(base, cfg), np.arange(n_r)
    if cfg.cv == "logo":
        return [(tr, te, todas) for tr, te in LeaveOneGroupOut().split(np.zeros(n), y, grupos)]
    n_splits = min(cfg.n_splits, len(set(grupos)))
    sgkf = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=cfg.seed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return [(tr, te, todas) for tr, te in sgkf.split(np.zeros(n), y, grupos)]


@dataclass
class Resultado:
    cfg: Config
    clases: list
    nombres_features: list
    metricas: dict
    bloques: dict = field(default_factory=dict)   # nombre -> dict(P, Y, grupos, M)
    importancias: dict = field(default_factory=dict)
    segundos: float = 0.0
    umbrales: np.ndarray | None = None            # por clase, óptimos sobre ind OOF
    etiqueta: str = ""                            # nombre libre del experimento


def _solo_vistas(Yg: np.ndarray, Pg: np.ndarray, Mg: np.ndarray):
    """Anula las clases no vistas por el modelo de cada captura; `keep` descarta
    las capturas que solo tenían clases no vistas."""
    M = Mg > 0.5
    Y2, P2 = (Yg * M).astype(np.uint8), Pg * M
    keep = ~((Yg.sum(axis=1) > 0) & (Y2.sum(axis=1) == 0))
    return Y2, P2, keep


def _metricas_vistas(Yg: np.ndarray, Pg: np.ndarray, Mg: np.ndarray, umbral, clases: list,
                     prefijo: str) -> dict:
    """Métricas ignorando, captura por captura, las clases que el modelo de su
    fold nunca vio en entrenamiento (en lolo: aparatos que solo existen en la
    casa de test). Las capturas que solo tenían clases no vistas se descartan.
    Mide 'reconocer unidades nuevas de clases conocidas'."""
    # se calcula siempre (aunque todo esté visto coincide con _c_) para que las
    # columnas del leaderboard sean comparables entre experimentos y datasets
    Y2, P2, keep = _solo_vistas(Yg, Pg, Mg)
    m = metricas_multilabel(Y2[keep], P2[keep], umbral, clases, prefijo)
    m[prefijo + "descartadas"] = int((~keep).sum())
    return m


def run_experiment(ds: Dataset, cfg: Config, leaderboard: str | Path | None = None,
                   verbose: bool = True, etiqueta: str = "") -> Resultado:
    """Validación según cfg.cv. Bloques de capturas REALES:
      ind   = individuales (+ sin carga) de test
      comb  = combinadas reales (en lolo: solo las del lugar de test de cada fold)
      todas = ind + comb
    Niveles: _v_ por ventana, _c_ por captura (umbral 0.5), _u_ por captura con
    umbrales por clase aprendidos en ind OOF (solo comb/mix: en ind/todas serían
    optimistas), _s_ por captura sobre clases vistas (si alguna quedó sin ver)."""
    t0 = time.time()
    ds = preparar_dataset(ds, cfg, verbose)
    fs = ds.fs()
    clases = ds.clases
    C = len(clases)
    reales = ds.filtrar(TIPO_COMBINADA)
    base = Dataset([r for r in ds.registros if r.tipo != TIPO_COMBINADA], clases)
    sen, Y, G = base.senales(), base.y(), base.grupos()
    sen_r, Y_r, G_r = reales.senales(), reales.y(), reales.grupos()

    bloques = {b: {"P": [], "Y": [], "grupos": [], "M": []} for b in ("ind", "mix")}
    F_comb = None
    nombres, imps, costo = nombres_features(cfg, fs), [], {}

    folds = dividir(base, reales, cfg)
    for k, (tr, te, te_c) in enumerate(folds):
        rng = np.random.default_rng(cfg.seed + k)
        sen_tr, Y_tr, G_tr = [sen[i] for i in tr], Y[tr], G[tr]
        vistas = Y_tr.sum(axis=0) > 0

        # --- train: individuales + mezclas (+ augment)
        m = mezclas_de(sen_tr, Y_tr, G_tr, clases, cfg, rng, cfg.mix_total if cfg.mix_habilitado else 0,
                       f"mixtr{k}", fs)
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
        n_te = 0
        if len(te):
            sen_te, Y_te, G_te = [sen[i] for i in te], Y[te], G[te]
            F_te, Yw_te, Gw_te, _ = armar_conjunto(sen_te, Y_te, G_te, cfg, fs)
            n_te = len(F_te)
            for kk, v in zip(("P", "Y", "grupos", "M"), (predecir_proba(modelo, F_te, C), Yw_te, Gw_te,
                                                          np.repeat(vistas[None], len(F_te), axis=0))):
                bloques["ind"][kk].append(v)
            if cfg.mix_habilitado and cfg.mix_test:
                mt = mezclas_de(sen_te, Y_te, G_te, clases, cfg, rng, cfg.mix_test_total, f"mixte{k}", fs)
                if mt is not None:
                    F_mt, Yw_mt, Gw_mt, _ = armar_conjunto(mt.senales, mt.Y, mt.grupos, cfg, fs)
                    for kk, v in zip(("P", "Y", "grupos", "M"), (predecir_proba(modelo, F_mt, C), Yw_mt, Gw_mt,
                                                                  np.repeat(vistas[None], len(F_mt), axis=0))):
                        bloques["mix"][kk].append(v)

        # --- combinadas reales que evalúa este fold
        if len(te_c):
            if F_comb is None:
                F_comb, Yw_r, Gw_r, _ = armar_conjunto(sen_r, Y_r, G_r, cfg, fs)
                P_sum = np.zeros((len(F_comb), C)); n_ev = np.zeros(len(F_comb)); M_c = np.ones((len(F_comb), C), bool)
            sel = np.isin(Gw_r, G_r[te_c])
            P_sum[sel] += predecir_proba(modelo, F_comb[sel], C); n_ev[sel] += 1; M_c[sel] &= vistas

        if verbose:
            print(f"  fold {k + 1}/{len(folds)}: train {len(F_tr)} ventanas, test ind {n_te}, "
                  f"test comb {len(te_c)} capturas | {time.time() - t0:.0f} s")

    cerrar = lambda b: {kk: np.concatenate(v) for kk, v in b.items()}
    bl = {"ind": cerrar(bloques["ind"])}
    if F_comb is not None and (n_ev > 0).any():
        ev = n_ev > 0
        bl["comb"] = {"P": (P_sum[ev] / n_ev[ev][:, None]).astype(np.float32), "Y": Yw_r[ev],
                      "grupos": Gw_r[ev], "M": M_c[ev]}
        bl["todas"] = {kk: np.concatenate([bl["ind"][kk], bl["comb"][kk]]) for kk in bl["ind"]}
    else:
        bl["todas"] = bl["ind"]
    if bloques["mix"]["P"]:
        bl["mix"] = cerrar(bloques["mix"])

    met = {}
    # umbrales por clase: sobre las individuales OOF, solo con las clases que cada
    # modelo vio (una clase nunca vista no aporta información sobre su umbral)
    Pg_ind, Yg_ind, _ = agregar_por_grupo(bl["ind"]["P"], bl["ind"]["Y"], bl["ind"]["grupos"])
    Mg_ind, _, _ = agregar_por_grupo(bl["ind"]["M"].astype(np.float32), bl["ind"]["Y"], bl["ind"]["grupos"])
    Yu, Pu, ku = _solo_vistas(Yg_ind, Pg_ind, Mg_ind)
    umbrales = (umbrales_optimos_exact(Yu[ku], Pu[ku]) if cfg.umbral_criterio == "exact"
                else umbrales_optimos(Yu[ku], Pu[ku]))
    for b, d in bl.items():
        met.update(metricas_multilabel(d["Y"], d["P"], cfg.umbral, clases, f"{b}_v_"))
        Pg, Yg, _ = agregar_por_grupo(d["P"], d["Y"], d["grupos"])
        Mg, _, _ = agregar_por_grupo(d["M"].astype(np.float32), d["Y"], d["grupos"])
        met.update(metricas_multilabel(Yg, Pg, cfg.umbral, clases, f"{b}_c_"))
        met.update(_metricas_vistas(Yg, Pg, Mg, cfg.umbral, clases, f"{b}_s_"))
        if b in ("comb", "mix"):
            met.update(metricas_multilabel(Yg, Pg, umbrales, clases, f"{b}_u_"))
    met.update(costo)
    met["n_features"] = len(nombres)
    met["n_folds"] = len(folds)
    met["segundos"] = round(time.time() - t0, 1)

    imp_media = {}
    if imps and imps[0]:
        for n in nombres:
            imp_media[n] = float(np.mean([i.get(n, 0.0) for i in imps]))
        imp_media = dict(sorted(imp_media.items(), key=lambda kv: -kv[1]))

    res = Resultado(cfg, clases, nombres, met, bl, imp_media, met["segundos"], umbrales, etiqueta)
    if leaderboard is not None:
        registrar(res, leaderboard, ds.manifest_hash)
    return res


@dataclass
class Repetido:
    """Mismo experimento con varias semillas (folds sgkf, mezclas y árboles cambian)."""
    cfg: Config
    resultados: list
    media: dict
    desvio: dict
    etiqueta: str = ""

    @property
    def metricas(self) -> dict:
        return self.media


def run_repetido(ds: Dataset, cfg: Config, n_semillas: int = 5, leaderboard: str | Path | None = None,
                 verbose: bool = False, etiqueta: str = "") -> Repetido:
    """Corre run_experiment con semillas cfg.seed..cfg.seed+n-1 y resume media ± desvío.
    Con 59 combinadas, el desvío entre semillas de comb exact es ~0.06: una
    diferencia entre configuraciones menor a eso no es evidencia de nada."""
    res = [run_experiment(ds, cfg.clonar(seed=cfg.seed + i), verbose=verbose, etiqueta=etiqueta)
           for i in range(n_semillas)]
    claves = [k for k, v in res[0].metricas.items() if isinstance(v, (int, float))]
    claves = [k for k in claves if all(k in r.metricas for r in res)]
    media = {k: float(np.mean([r.metricas[k] for r in res])) for k in claves}
    desvio = {k: float(np.std([r.metricas[k] for r in res])) for k in claves}
    rep = Repetido(cfg, res, media, desvio, etiqueta)
    if leaderboard is not None:
        registrar(rep, leaderboard, ds.manifest_hash)
    return rep


def comparar(a: Repetido, b: Repetido, metricas=("todas_c_f1_micro", "comb_c_exact_match", "comb_c_f1_micro",
                                                  "ind_c_f1_micro")) -> str:
    """Comparación PAREADA semilla a semilla (mismas semillas → mismos folds y
    mezclas de partida): diferencia media b−a, su desvío y en cuántas semillas b gana."""
    lineas = [f"{'métrica':22s} {'a':>7s} {'b':>7s} {'b−a':>8s} {'±':>6s}  b gana"]
    for m in metricas:
        if m not in a.media or m not in b.media:
            continue
        d = np.array([rb.metricas[m] - ra.metricas[m] for ra, rb in zip(a.resultados, b.resultados)])
        lineas.append(f"{m:22s} {a.media[m]:7.3f} {b.media[m]:7.3f} {d.mean():+8.3f} {d.std():6.3f}  "
                      f"{int((d > 0).sum())}/{len(d)}")
    return "\n".join(lineas)


def evaluar_test_congelado(ds: Dataset, cfg: Config, umbrales=None, verbose: bool = True) -> dict:
    """Entrena con todo lo que NO está en cfg.test_congelado y evalúa UNA VEZ sobre
    lo congelado (individuales y combinadas). Usar solo al final, con la config
    ya elegida: si se usa para elegir, deja de ser un test."""
    if not cfg.test_congelado:
        raise ValueError("cfg.test_congelado está vacío")
    ds_all = preparar_dataset(ds, cfg, verbose=False, incluir_congelado=True)
    cong = set(cfg.test_congelado)
    en_test = lambda r: r.lugar in cong or r.sesion in cong
    fs, clases = ds_all.fs(), ds_all.clases
    tr = Dataset([r for r in ds_all.registros if not en_test(r) and r.tipo != TIPO_COMBINADA], clases)
    te = [r for r in ds_all.registros if en_test(r)]
    sen, Y, G = tr.senales(), tr.y(), tr.grupos()
    rng = np.random.default_rng(cfg.seed)
    m = mezclas_de(sen, Y, G, clases, cfg, rng, cfg.mix_total if cfg.mix_habilitado else 0, "mixcong", fs)
    if m is not None:
        sen, Y, G = sen + m.senales, np.vstack([Y, m.Y]), np.concatenate([G, m.grupos])
    F, Yw, _, _ = armar_conjunto_train(sen, Y, G, cfg, fs, rng)
    modelo = construir_modelo(cfg)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        modelo.fit(F, Yw)
    vistas = tr.y().sum(axis=0) > 0
    met = {}
    for b, regs in (("ind", [r for r in te if r.tipo != TIPO_COMBINADA]),
                    ("comb", [r for r in te if r.tipo == TIPO_COMBINADA]), ("todas", te)):
        if not regs:
            continue
        d = Dataset(regs, clases)
        Ft, Ywt, Gwt, _ = armar_conjunto(d.senales(), d.y(), d.grupos(), cfg, fs)
        Pg, Yg, _ = agregar_por_grupo(predecir_proba(modelo, Ft, len(clases)), Ywt, Gwt)
        met.update(metricas_multilabel(Yg, Pg, cfg.umbral, clases, f"{b}_c_"))
        met.update(_metricas_vistas(Yg, Pg, np.repeat(vistas[None].astype(float), len(Yg), 0),
                                    cfg.umbral, clases, f"{b}_s_"))
        if umbrales is not None:
            met.update(metricas_multilabel(Yg, Pg, umbrales, clases, f"{b}_u_"))
    if verbose:
        print(f"test congelado {sorted(cong)}: {len(te)} capturas, train {len(tr.registros)} individuales")
    return met


def entrenar_final(ds: Dataset, cfg: Config, verbose: bool = True):
    """Entrena con TODAS las individuales (+ mixes + augment) para exportar.
    Devuelve (modelo, nombres_features, clases)."""
    ds = preparar_dataset(ds, cfg, verbose=False)
    fs = ds.fs()
    base = Dataset([r for r in ds.registros if r.tipo != TIPO_COMBINADA], ds.clases)
    sen, Y, G = base.senales(), base.y(), base.grupos()
    rng = np.random.default_rng(cfg.seed)
    m = mezclas_de(sen, Y, G, ds.clases, cfg, rng, cfg.mix_total if cfg.mix_habilitado else 0, "mixfin", fs)
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

def registrar(res, ruta_csv: str | Path, manifest_hash: str = "") -> None:
    """Agrega una fila (config + métricas) al leaderboard CSV. Con un Repetido
    guarda la media, el desvío (`<métrica>_std`) y `n_semillas`."""
    import pandas as pd
    metricas = dict(res.metricas)
    if isinstance(res, Repetido):
        metricas.update({f"{k}_std": v for k, v in res.desvio.items()})
        metricas["n_semillas"] = len(res.resultados)
    fila = {"etiqueta": res.etiqueta, "nombre": res.cfg.nombre(), "fecha": time.strftime("%Y-%m-%d %H:%M:%S"),
            "manifest": manifest_hash[:10], **metricas, "config": res.cfg.to_json()}
    ruta = Path(ruta_csv)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    nuevo = pd.DataFrame([fila])
    if ruta.exists():
        nuevo = pd.concat([pd.read_csv(ruta), nuevo], ignore_index=True)
    nuevo.to_csv(ruta, index=False)


def cargar_leaderboard(ruta_csv: str | Path, ordenar_por: str = "todas_c_f1_micro"):
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
    ds = preparar_dataset(ds, cfg, verbose=False)
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
