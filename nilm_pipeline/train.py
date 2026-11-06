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
from sklearn.base import clone
from sklearn.ensemble import (ExtraTreesClassifier, HistGradientBoostingClassifier,
                              RandomForestClassifier)
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import LeaveOneGroupOut, StratifiedGroupKFold
from sklearn.multiclass import OneVsRestClassifier
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from .augment import aumentar_ventanas, copias_por_ventana, perturbar_capturas
from .config import Config
from .data import TIPO_COMBINADA, Dataset
from .evaluate import (agregar_por_grupo, importancias, metricas_multilabel, umbrales_optimos,
                       umbrales_optimos_exact)
from .features import extraer_features, mascara_armonica, nombres_features
from .mix import sintetizar_combinadas
from .preprocess import preprocesar_captura, preprocesar_ventanas
from .segment import segmentar_dataset

_ARBOLES = ("rf", "et")


# ----------------------------------------------------------------- modelo --

class OvRFranjas:
    """Un bosque binario por clase, cada uno sobre su propia franja de frecuencias."""

    def __init__(self, base, franjas: dict, clases: list, nombres: list, f_red: float):
        self.base, self.franjas, self.clases, self.f_red = base, franjas, clases, f_red
        self.freqs = np.array([float(n[4:-2]) for n in nombres])

    def _mascara(self, clase: str) -> np.ndarray:
        fmin, fmax, tipo = self.franjas.get(clase, (0.0, np.inf, "todo"))
        m = (self.freqs >= fmin) & (self.freqs <= fmax) & mascara_armonica(self.freqs, self.f_red, tipo)
        return m | (np.abs(self.freqs - self.f_red) <= 1.5 * (self.freqs[1] - self.freqs[0]))

    def fit(self, X: np.ndarray, Y: np.ndarray):
        self.mascaras_, self.estimators_, self.constante_ = [], [], []
        for j, c in enumerate(self.clases):
            m = self._mascara(c)
            self.mascaras_.append(m)
            constante = len(np.unique(Y[:, j])) < 2
            self.constante_.append(float(Y[0, j]) if constante else None)
            self.estimators_.append(None if constante else clone(self.base).fit(X[:, m], Y[:, j]))
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        P = np.zeros((len(X), len(self.clases)), dtype=np.float32)
        for j, (e, m, k) in enumerate(zip(self.estimators_, self.mascaras_, self.constante_)):
            P[:, j] = k if e is None else e.predict_proba(X[:, m])[:, 1]
        return P


class OvRCadena:
    """Un bosque binario por clase, en cadena: cada uno recibe también la salida de los anteriores.
    Al entrenar ve las etiquetas reales de las clases previas; al predecir, sus probabilidades."""

    def __init__(self, base, nombres: list, orden: str):
        self.base, self.nombres, self.orden = base, nombres, orden

    def fit(self, X: np.ndarray, Y: np.ndarray):
        h1, solas = _h1_de_features(X, self.nombres), Y.sum(axis=1) == 1
        corriente = [h1[solas & (Y[:, j] == 1)].mean() if (solas & (Y[:, j] == 1)).any() else 0.0 for j in range(Y.shape[1])]
        self.orden_ = np.argsort(corriente)[::-1 if self.orden == "grandes" else 1]
        self.estimators_, self.constante_ = [], []
        Xc = X
        for j in self.orden_:
            constante = len(np.unique(Y[:, j])) < 2
            self.constante_.append(float(Y[0, j]) if constante else None)
            self.estimators_.append(None if constante else clone(self.base).fit(Xc, Y[:, j]))
            Xc = np.hstack([Xc, Y[:, [j]].astype(np.float32)])
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        P = np.zeros((len(X), len(self.orden_)), dtype=np.float32)
        Xc = X
        for j, e, k in zip(self.orden_, self.estimators_, self.constante_):
            P[:, j] = k if e is None else e.predict_proba(Xc)[:, 1]
            Xc = np.hstack([Xc, P[:, [j]]])
        return P


def construir_modelo(cfg: Config, seed: int | None = None, clases: list | None = None, nombres: list | None = None):
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
    if cfg.franjas:
        return OvRFranjas(base, cfg.franjas, clases, nombres, cfg.f_red)
    if cfg.multi_salida == "cadena":
        return OvRCadena(base, nombres, cfg.cadena_orden)
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
        elif e is not None:
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
    senales = perturbar_capturas(senales, cfg, rng, fs)
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
               rng: np.random.Generator, total: int, prefijo: str, fs: float, rafagas: np.ndarray | None = None,
               lugares: np.ndarray | None = None):
    """Mixes sintéticos desde un subconjunto de capturas, o None si no hay
    al menos 2 clases con individuales."""
    Yi = np.asarray(Y)
    individuales = Yi[Yi.sum(axis=1) == 1]
    if total <= 0 or len(np.unique(individuales.argmax(axis=1))) < 2:
        return None
    return sintetizar_combinadas(senales, Y, grupos, clases, cfg, rng, total, prefijo, fs, rafagas, lugares)


def _perturbar_test(senales: list, cfg: Config, fs: float) -> list:
    """Solo robustez: simula otra tensión de red (escala) u otra frecuencia de red en el test."""
    if cfg.test_escala == 1.0 and not cfg.test_f0_hz:
        return senales
    out = []
    for x in senales:
        x = np.asarray(x, dtype=np.float64)
        if cfg.test_f0_hz:
            x = np.interp(np.arange(len(x)) * (1 + cfg.test_f0_hz / cfg.f_red), np.arange(len(x)), x)
        out.append((x * cfg.test_escala).astype(np.float32))
    return out


def _limitar_por_rafaga(idx: np.ndarray, rafagas: np.ndarray, maximo: int, rng: np.random.Generator) -> np.ndarray:
    """Se queda con hasta `maximo` capturas al azar de cada ráfaga."""
    if maximo <= 0:
        return idx
    salida = []
    for r in np.unique(rafagas[idx]):
        g = idx[rafagas[idx] == r]
        salida.append(g if len(g) <= maximo else rng.choice(g, maximo, replace=False))
    return np.sort(np.concatenate(salida))


def _h1_de_features(F: np.ndarray, nombres: list) -> np.ndarray:
    """Amplitud de la fundamental por ventana, a partir del vector de features."""
    if "h1_A" in nombres:
        return F[:, nombres.index("h1_A")]
    j = [i for i, n in enumerate(nombres) if n.startswith("fft_") and 40.0 <= float(n[4:-2]) <= 60.0]
    return F[:, j].max(axis=1)


def _minimos_h1(F: np.ndarray, Yw: np.ndarray, nombres: list, cfg: Config) -> np.ndarray | None:
    """Corriente mínima creíble por clase: factor × P5 de H1 entre sus ventanas individuales de train."""
    if not cfg.filtro_corriente:
        return None
    h1, solas = _h1_de_features(F, nombres), Yw.sum(axis=1) == 1
    return np.array([cfg.filtro_corriente * np.percentile(h1[solas & (Yw[:, j] == 1)], 5) if (solas & (Yw[:, j] == 1)).any() else 0.0
                     for j in range(Yw.shape[1])])


def _proba(modelo, F: np.ndarray, n_clases: int, minimos: np.ndarray | None, nombres: list) -> np.ndarray:
    P = predecir_proba(modelo, F, n_clases)
    if minimos is not None:
        P = P * (_h1_de_features(F, nombres)[:, None] >= minimos)
    return P


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
    sen_r, Y_r, G_r = _perturbar_test(reales.senales(), cfg, fs), reales.y(), reales.grupos()
    R, Lug = base.rafagas(), base.lugares()

    bloques = {b: {"P": [], "Y": [], "grupos": [], "M": []} for b in ("ind", "mix")}
    F_comb = None
    nombres, imps, costo = nombres_features(cfg, fs), [], {}

    folds = dividir(base, reales, cfg)
    for k, (tr, te, te_c) in enumerate(folds):
        rng = np.random.default_rng(cfg.seed + k)
        tr = _limitar_por_rafaga(tr, R, cfg.max_por_rafaga, rng)
        sen_tr, Y_tr, G_tr = [sen[i] for i in tr], Y[tr], G[tr]
        vistas = Y_tr.sum(axis=0) > 0

        # --- train: individuales + mezclas (+ augment)
        m = mezclas_de(sen_tr, Y_tr, G_tr, clases, cfg, rng, cfg.mix_total if cfg.mix_habilitado else 0,
                       f"mixtr{k}", fs, R[tr], Lug[tr])
        if m is not None:
            sen_tr, Y_tr, G_tr = sen_tr + m.senales, np.vstack([Y_tr, m.Y]), np.concatenate([G_tr, m.grupos])
        F_tr, Yw_tr, _, nombres = armar_conjunto_train(sen_tr, Y_tr, G_tr, cfg, fs, rng)
        modelo = construir_modelo(cfg, cfg.seed + k, clases, nombres)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            modelo.fit(F_tr, Yw_tr)
        minimos = _minimos_h1(F_tr, Yw_tr, nombres, cfg)
        costo = costo_modelo(modelo)
        imps.append(importancias(modelo, nombres))

        # --- test: individuales
        n_te = 0
        if len(te):
            sen_te, Y_te, G_te = _perturbar_test([sen[i] for i in te], cfg, fs), Y[te], G[te]
            F_te, Yw_te, Gw_te, _ = armar_conjunto(sen_te, Y_te, G_te, cfg, fs)
            n_te = len(F_te)
            for kk, v in zip(("P", "Y", "grupos", "M"), (_proba(modelo, F_te, C, minimos, nombres), Yw_te, Gw_te,
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
            P_sum[sel] += _proba(modelo, F_comb[sel], C, minimos, nombres); n_ev[sel] += 1; M_c[sel] &= vistas

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


def _conteos_por_captura(rep: Repetido, bloque: str, clases_destino: list | None, mapa: dict | None):
    """TP, FP, FN y acierto exacto por captura, promediados entre semillas (clases vistas).
    `mapa` proyecta las predicciones a otro conjunto de clases: clase -> grupo (P = máximo, Y = OR)
    o clase -> None para sacarla. Así dos experimentos con clases distintas se comparan en lo mismo."""
    filas = {}
    for r in rep.resultados:
        d = r.bloques[bloque]
        P, Y, G = agregar_por_grupo(d["P"], d["Y"], d["grupos"])
        M, _, _ = agregar_por_grupo(d["M"].astype(np.float32), d["Y"], d["grupos"])
        M = M > 0.5
        clases = r.clases
        if clases_destino is not None and list(clases_destino) != list(clases):
            mapa = mapa or {}
            P2, Y2, M2 = [np.zeros((len(P), len(clases_destino))) for _ in range(3)]
            for j, c in enumerate(clases):
                g = mapa.get(c, c)
                if g is None:
                    continue
                i = list(clases_destino).index(g)
                P2[:, i], Y2[:, i], M2[:, i] = np.maximum(P2[:, i], P[:, j]), np.maximum(Y2[:, i], Y[:, j]), np.maximum(M2[:, i], M[:, j])
            P, Y, M = P2, Y2.astype(np.uint8), M2 > 0.5
        Y2, P2, keep = _solo_vistas(Y, P, M.astype(np.float32))
        pred = P2 >= r.cfg.umbral
        tp, fp, fn = (pred & (Y2 == 1)).sum(1), (pred & (Y2 == 0)).sum(1), (~pred & (Y2 == 1)).sum(1)
        for i in np.where(keep)[0]:
            filas.setdefault(G[i], []).append((tp[i], fp[i], fn[i], fp[i] + fn[i] == 0))
    return {g: np.mean(v, axis=0) for g, v in filas.items()}


def comparar_bootstrap(ds: Dataset, a: Repetido, b: Repetido, bloque: str = "todas", n: int = 4000, seed: int = 0,
                       mapa_a: dict | None = None, mapa_b: dict | None = None) -> dict:
    """Diferencia b−a con intervalo del 95 %, remuestreando RÁFAGAS del test dentro de cada lugar.

    El desvío entre semillas solo mide el azar del entrenamiento. Este intervalo mide además cuánto
    depende el resultado de qué ocasiones cayeron en el test: es el error que importa con tan pocas.
    No incluye la variación entre casas, porque hay solo 4 lugares. Para clases distintas, usar mapa_*."""
    destino = b.resultados[0].clases if mapa_a else (a.resultados[0].clases if mapa_b else None)
    ca, cb = _conteos_por_captura(a, bloque, destino, mapa_a), _conteos_por_captura(b, bloque, destino, mapa_b)
    regs = {r.grupo: r for r in ds.registros}
    grupos = sorted(set(ca) & set(cb) & set(regs))
    raf = np.array([regs[g].rafaga for g in grupos])
    A, B = np.array([ca[g] for g in grupos]), np.array([cb[g] for g in grupos])
    unicas, inv = np.unique(raf, return_inverse=True)
    lugar = np.array([r.split("_")[0] for r in unicas])
    SA, SB = np.zeros((len(unicas), 4)), np.zeros((len(unicas), 4))
    np.add.at(SA, inv, A); np.add.at(SB, inv, B)
    n_cap = np.bincount(inv).astype(float)
    f1 = lambda S: 2 * S[..., 0] / np.maximum(2 * S[..., 0] + S[..., 1] + S[..., 2], 1e-9)
    rng = np.random.default_rng(seed)
    pesos = np.zeros((n, len(unicas)))
    for L in np.unique(lugar):
        i = np.where(lugar == L)[0]
        pesos[:, i] = rng.multinomial(len(i), np.full(len(i), 1 / len(i)), size=n)
    d_f1 = f1(pesos @ SB) - f1(pesos @ SA)
    d_ex = (pesos @ SB[:, 3] - pesos @ SA[:, 3]) / (pesos @ n_cap)
    total = lambda S: (f1(S.sum(0)), S[:, 3].sum() / n_cap.sum())
    (fa, ea), (fb, eb) = total(SA), total(SB)
    return {"capturas": len(grupos), "rafagas": len(unicas), "f1_a": float(fa), "f1_b": float(fb), "dif_f1": float(fb - fa),
            "ic95_f1": tuple(np.percentile(d_f1, [2.5, 97.5]).round(3).tolist()), "p_mejora_f1": float((d_f1 > 0).mean()),
            "exact_a": float(ea), "exact_b": float(eb), "dif_exact": float(eb - ea),
            "ic95_exact": tuple(np.percentile(d_ex, [2.5, 97.5]).round(3).tolist()), "p_mejora_exact": float((d_ex > 0).mean())}


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
    idx = _limitar_por_rafaga(np.arange(len(sen)), tr.rafagas(), cfg.max_por_rafaga, rng)
    sen, Y, G = [sen[i] for i in idx], Y[idx], G[idx]
    m = mezclas_de(sen, Y, G, clases, cfg, rng, cfg.mix_total if cfg.mix_habilitado else 0, "mixcong", fs, tr.rafagas()[idx], tr.lugares()[idx])
    if m is not None:
        sen, Y, G = sen + m.senales, np.vstack([Y, m.Y]), np.concatenate([G, m.grupos])
    F, Yw, _, nombres = armar_conjunto_train(sen, Y, G, cfg, fs, rng)
    modelo = construir_modelo(cfg, clases=clases, nombres=nombres)
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
    idx = _limitar_por_rafaga(np.arange(len(sen)), base.rafagas(), cfg.max_por_rafaga, rng)
    sen, Y, G = [sen[i] for i in idx], Y[idx], G[idx]
    m = mezclas_de(sen, Y, G, ds.clases, cfg, rng, cfg.mix_total if cfg.mix_habilitado else 0, "mixfin", fs, base.rafagas()[idx], base.lugares()[idx])
    if m is not None:
        sen, Y, G = sen + m.senales, np.vstack([Y, m.Y]), np.concatenate([G, m.grupos])
    F, Yw, _, nombres = armar_conjunto_train(sen, Y, G, cfg, fs, rng)
    modelo = construir_modelo(cfg, clases=ds.clases, nombres=nombres)
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
