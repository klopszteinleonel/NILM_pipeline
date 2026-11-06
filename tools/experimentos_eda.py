"""
Experimentos de la Parte B derivados del EDA, con validación por lugar (casa nunca vista).

Cada variante se compara contra la base con las mismas semillas. Para cada una se informa:
  - la diferencia media entre semillas y en cuántas gana (azar del entrenamiento);
  - un intervalo del 95 % remuestreando ráfagas del test (azar de qué ocasiones se midieron).

Uso, desde la raíz de pipeline/:
    python3 tools/experimentos_eda.py --salida outputs/experimentos_eda.csv
    python3 tools/experimentos_eda.py --grupos cobertura nivel --semillas 3
    python3 tools/experimentos_eda.py --grupos fft extras armonicas armonicas_extras      # contra todos los bins
    python3 tools/experimentos_eda.py --grupos siguiente                                  # contra armónicos ±2
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import matplotlib

matplotlib.use("Agg")
import pandas as pd

from nilm_pipeline import Config, cargar_dataset, comparar_bootstrap, run_repetido

AGRUPAR = {"calefon": "resistiva", "horno": "resistiva", "pava": "resistiva", "plancha": "resistiva",
           "airecalor": "aire"}
# Base contra la que se hizo la auditoría del 2026-11-04. La base actual (Config por defecto) es
# esta más mix_sesgo_grande_chica=0, mix_total=300, rawfft_f0=True, min_rafagas=2 y rawfft_armonicas_tol=2.
BASE = Config(agrupar=AGRUPAR, excluir_clases=("parasito",), mix_sesgo_grande_chica=0.5, mix_total=100, rawfft_f0=False,
              min_rafagas=1, rawfft_armonicas_tol=None)

# Tabla de franjas que salió del EDA. Se dedujo mirando TODO el dataset, incluido el
# lugar de test de cada fold, así que su resultado es algo optimista.
FRANJAS_EDA = {"resistiva": (0.0, 100.0, "todo"), "aire": (0.0, 400.0, "armonicas"),
               "lavarropa": (0.0, 3000.0, "pares"), "heladera": (0.0, 750.0, "armonicas"),
               "luces": (0.0, 2000.0, "impares"), "tele": (0.0, 2000.0, "impares"),
               "aireinverter": (0.0, 3000.0, "todo")}
FRANJAS_AMPLIAS = {"resistiva": (0.0, 1000.0, "armonicas"), "aire": (0.0, 1000.0, "armonicas"),
                   "lavarropa": (0.0, 3000.0, "armonicas"), "heladera": (0.0, 1000.0, "armonicas"),
                   "luces": (0.0, 3000.0, "armonicas"), "tele": (0.0, 3000.0, "armonicas"),
                   "aireinverter": (0.0, 3000.0, "todo")}
ELECTRONICA = {"tele": "electronica", "luces": "electronica"}

# (grupo, nombre) -> (config, proyección de las clases de la base para poder compararlas)
VARIANTES = {
    # Las mezclas no se parecen a lo real: 91 % con carga grande (66 % en las reales)
    ("cobertura", "sesgo0"): (BASE.clonar(mix_sesgo_grande_chica=0.0), None),
    ("cobertura", "mix200"): (BASE.clonar(mix_total=200), None),
    ("cobertura", "mix300"): (BASE.clonar(mix_total=300), None),
    ("cobertura", "sesgo0_mix300"): (BASE.clonar(mix_sesgo_grande_chica=0.0, mix_total=300), None),
    ("cobertura", "sesgo0_mix500"): (BASE.clonar(mix_sesgo_grande_chica=0.0, mix_total=500), None),
    # Las armónicas suman sin relación de fase; la alineación usa 50 Hz fijos; hay casi duplicados
    ("mezcla", "alinear_f0"): (BASE.clonar(mix_alinear_f0=True), None),
    ("mezcla", "armonicas_azar"): (BASE.clonar(mix_armonicas_azar=True), None),
    ("mezcla", "fase_aleatoria"): (BASE.clonar(mix_fase="aleatoria"), None),
    ("mezcla", "por_rafaga"): (BASE.clonar(mix_por_rafaga=True), None),
    ("mezcla", "max3_por_rafaga"): (BASE.clonar(max_por_rafaga=3), None),
    # Cada equipo tiene su potencia: el nivel no es una propiedad de la clase
    ("nivel", "mix_escala30"): (BASE.clonar(mix_escala=(0.7, 1.3)), None),
    ("nivel", "mix_escala50"): (BASE.clonar(mix_escala=(0.5, 1.5)), None),
    ("nivel", "train_escala1.6"): (BASE.clonar(train_escala=(0.6, 1.6)), None),
    ("nivel", "train_escala2.5"): (BASE.clonar(train_escala=(0.4, 2.5)), None),
    ("nivel", "train1.6_mix30"): (BASE.clonar(train_escala=(0.6, 1.6), mix_escala=(0.7, 1.3)), None),
    # Las armónicas de las resistivas son la red de cada casa
    ("red", "red1.5"): (BASE.clonar(train_red_pct=1.5), None),
    ("red", "red3"): (BASE.clonar(train_red_pct=3.0), None),
    # Franjas del tutor y del EDA
    ("franjas", "solo_armonicas"): (BASE.clonar(rawfft_mascara="armonicas"), None),
    ("franjas", "solo_impares"): (BASE.clonar(rawfft_mascara="impares"), None),
    ("franjas", "fmax1000"): (BASE.clonar(rawfft_fmax=1000.0), None),
    ("franjas", "fmax1500"): (BASE.clonar(rawfft_fmax=1500.0), None),
    ("franjas", "fmax2000"): (BASE.clonar(rawfft_fmax=2000.0), None),
    ("franjas", "por_clase_eda"): (BASE.clonar(franjas=FRANJAS_EDA), None),
    ("franjas", "por_clase_amplias"): (BASE.clonar(franjas=FRANJAS_AMPLIAS), None),
    # La red se mueve y la forma es más transferible que el nivel
    ("features", "eje_f0"): (BASE.clonar(rawfft_f0=True), None),
    ("features", "forma_h1"): (BASE.clonar(rawfft_norm="h1"), None),
    # Pocas ocasiones independientes: árboles menos capaces de memorizar
    ("modelo", "hoja5"): (BASE.clonar(hiperparams={"min_samples_leaf": 5}), None),
    ("modelo", "hoja20"): (BASE.clonar(hiperparams={"min_samples_leaf": 20}), None),
    ("modelo", "rf50x10"): (BASE.clonar(n_estimators=50, max_depth=10), None),
    # La corriente medida acota qué cargas pueden estar prendidas
    ("post", "filtro_corriente0.5"): (BASE.clonar(filtro_corriente=0.5), None),
    ("post", "filtro_corriente0.25"): (BASE.clonar(filtro_corriente=0.25), None),
    # Familias eléctricas y clases con una sola ráfaga
    ("clases", "tele+luces"): (BASE.clonar(agrupar={**AGRUPAR, **ELECTRONICA}), ELECTRONICA),
    ("clases", "sin_tele"): (BASE.clonar(min_rafagas=2), {"tele": None}),
    # El augment rota cada ventana: ¿el salto que deja en el medio perjudica?
    ("augment", "escala1.4_con_shift"): (BASE.clonar(aug_n=2, escala=(0.7, 1.4), snr_db=(60.0, 60.0)), None),
    ("augment", "escala1.4_sin_shift"): (BASE.clonar(aug_n=2, escala=(0.7, 1.4), snr_db=(60.0, 60.0), shift=False), None),
}

# Combinaciones acumulativas de lo que funcionó por separado
_C1 = dict(mix_por_rafaga=True, hiperparams={"min_samples_leaf": 5})
_C2 = dict(_C1, mix_sesgo_grande_chica=0.0)
_C3 = dict(_C2, mix_total=300)
_C4 = dict(_C3, rawfft_mascara="armonicas")
_C5 = dict(_C4, rawfft_f0=True)
VARIANTES.update({
    ("combo", "1_rafaga_hoja5"): (BASE.clonar(**_C1), None),
    ("combo", "2_+sesgo0"): (BASE.clonar(**_C2), None),
    ("combo", "2b_+sesgo0_mix200"): (BASE.clonar(**_C2, mix_total=200), None),
    ("combo", "3_+mix300"): (BASE.clonar(**_C3), None),
    ("combo", "4_+armonicas"): (BASE.clonar(**_C4), None),
    ("combo", "5_+eje_f0"): (BASE.clonar(**_C5), None),
    ("combo", "6_+alinear_f0"): (BASE.clonar(**_C5, mix_alinear_f0=True), None),
    ("combo", "7_+red1.5"): (BASE.clonar(**_C5, mix_alinear_f0=True, train_red_pct=1.5), None),
    ("combo", "5b_+train_escala"): (BASE.clonar(**_C5, train_escala=(0.6, 1.6)), None),
    ("combo", "4b_hoja10"): (BASE.clonar(**{**_C4, "hiperparams": {"min_samples_leaf": 10}}), None),
    ("combo", "5c_sin_tele"): (BASE.clonar(**_C5, min_rafagas=2), {"tele": None}),
    ("combo", "5d_electronica"): (BASE.clonar(**_C5, agrupar={**AGRUPAR, **ELECTRONICA}), ELECTRONICA),
})

# Tamaño de FFT, features extra y solo armónicas, contra la base con todos los bins hasta 3 kHz
BASE_ACTUAL = Config(agrupar=AGRUPAR, excluir_clases=("parasito",), rawfft_armonicas_tol=None)
VARIANTES_ACTUAL = {
    ("fft", "W4096"): BASE_ACTUAL.clonar(W=4096),
    ("fft", "W8192"): BASE_ACTUAL.clonar(W=8192),
    ("fft", "W2048_nfft4096"): BASE_ACTUAL.clonar(n_fft=4096),
    ("fft", "W2048_nfft8192"): BASE_ACTUAL.clonar(n_fft=8192),
    ("extras", "extras"): BASE_ACTUAL.clonar(rawfft_extras=True),
}
for _W in (2048, 4096, 8192):
    for _tol in (0, 1, 2):
        VARIANTES_ACTUAL["armonicas", f"W{_W}_tol{_tol}"] = BASE_ACTUAL.clonar(W=_W, rawfft_armonicas_tol=_tol)
    VARIANTES_ACTUAL["armonicas_extras", f"W{_W}_tol1_extras"] = BASE_ACTUAL.clonar(W=_W, rawfft_armonicas_tol=1, rawfft_extras=True)

# Sobre la base de armónicos ±2 bins (Config por defecto)
BASE_ARMONICOS = Config(agrupar=AGRUPAR, excluir_clases=("parasito",))
VARIANTES_ARMONICOS = {
    ("siguiente", "arm_tol1"): BASE_ARMONICOS.clonar(rawfft_armonicas_tol=1),
    ("siguiente", "mismo_lugar"): BASE_ARMONICOS.clonar(mix_mismo_lugar=True),
    ("siguiente", "desfase15"): BASE_ARMONICOS.clonar(mix_jitter_fase=15.0),
    ("siguiente", "desfase30"): BASE_ARMONICOS.clonar(mix_jitter_fase=30.0),
    ("siguiente", "desfase45"): BASE_ARMONICOS.clonar(mix_jitter_fase=45.0),
    ("siguiente", "cadena_grandes"): BASE_ARMONICOS.clonar(multi_salida="cadena", cadena_orden="grandes"),
    ("siguiente", "cadena_chicas"): BASE_ARMONICOS.clonar(multi_salida="cadena", cadena_orden="chicas"),
    ("siguiente", "mismo_lugar+desfase30"): BASE_ARMONICOS.clonar(mix_mismo_lugar=True, mix_jitter_fase=30.0),
    ("siguiente", "mismo_lugar+cadena"): BASE_ARMONICOS.clonar(mix_mismo_lugar=True, multi_salida="cadena"),
    ("siguiente", "desfase30+cadena"): BASE_ARMONICOS.clonar(mix_jitter_fase=30.0, multi_salida="cadena"),
}


def fila(nombre, grupo, rep, base, ds, mapa):
    c = comparar_bootstrap(ds, base, rep, "comb", mapa_a=mapa)
    t = comparar_bootstrap(ds, base, rep, "todas", mapa_a=mapa)
    i = comparar_bootstrap(ds, base, rep, "ind", mapa_a=mapa)
    d = [r.metricas["todas_s_f1_micro"] - b.metricas["todas_s_f1_micro"] for r, b in zip(rep.resultados, base.resultados)] if not mapa else []
    return {"grupo": grupo, "variante": nombre, "todas_f1": t["f1_b"], "dif_todas": t["dif_f1"], "ic_todas": t["ic95_f1"], "p_todas": t["p_mejora_f1"],
            "ind_f1": i["f1_b"], "dif_ind": i["dif_f1"], "comb_f1": c["f1_b"], "dif_comb": c["dif_f1"], "ic_comb": c["ic95_f1"], "p_comb": c["p_mejora_f1"],
            "comb_exact": c["exact_b"], "dif_exact": c["dif_exact"], "ic_exact": c["ic95_exact"], "p_exact": c["p_mejora_exact"],
            "gana_semillas": sum(x > 0 for x in d) if d else None, "std_todas": rep.desvio["todas_s_f1_micro"],
            "nodos": rep.media["nodos_totales"], "n_features": rep.media["n_features"], "segundos": rep.media["segundos"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datos", default=os.path.join(os.path.dirname(__file__), "..", "..", "clienteTCP", "capturas_limpio"))
    ap.add_argument("--salida", default="outputs/experimentos_eda.csv")
    ap.add_argument("--semillas", type=int, default=5)
    ap.add_argument("--grupos", nargs="*", default=None, help="sin esto corre la auditoría contra la base anterior")
    a = ap.parse_args()

    ds = cargar_dataset(a.datos)
    print(f"dataset {ds.manifest_hash[:10]}: {len(ds.registros)} capturas; {a.semillas} semillas", flush=True)
    t0 = time.time()
    filas = []
    base_actual = None
    for (grupo, nombre), cfg in list(VARIANTES_ACTUAL.items()) + list(VARIANTES_ARMONICOS.items()):
        if not a.grupos or grupo not in a.grupos:
            continue
        if base_actual is None:
            base_actual = run_repetido(ds, BASE_ARMONICOS if grupo == "siguiente" else BASE_ACTUAL, n_semillas=a.semillas)
            print(f"base actual: todas {base_actual.media['todas_s_f1_micro']:.3f}  ind {base_actual.media['ind_s_f1_micro']:.3f}  "
                  f"comb {base_actual.media['comb_s_f1_micro']:.3f}  exact {base_actual.media['comb_s_exact_match']:.3f}  "
                  f"nodos {base_actual.media['nodos_totales']:.0f}", flush=True)
        rep = run_repetido(ds, cfg, n_semillas=a.semillas)
        f = fila(nombre, grupo, rep, base_actual, ds, None)
        filas.append(f)
        print(f"{grupo:10s} {nombre:22s} todas {f['todas_f1']:.3f} {f['dif_todas']:+.3f} {f['ic_todas']} | comb {f['comb_f1']:.3f} {f['dif_comb']:+.3f} {f['ic_comb']} | "
              f"exact {f['comb_exact']:.3f} {f['dif_exact']:+.3f} {f['ic_exact']} | ind {f['ind_f1']:.3f} {f['dif_ind']:+.3f} | gana {f['gana_semillas']} | "
              f"feats {f['n_features']:.0f} nodos {f['nodos']:.0f} | {time.time() - t0:.0f} s", flush=True)
        os.makedirs(os.path.dirname(os.path.abspath(a.salida)), exist_ok=True)
        pd.DataFrame(filas).to_csv(a.salida, index=False)
    base = None
    for (grupo, nombre), (cfg, mapa) in VARIANTES.items():
        if a.grupos and grupo not in a.grupos:
            continue
        if base is None:
            base = run_repetido(ds, BASE, n_semillas=a.semillas)
            print(f"base: todas {base.media['todas_s_f1_micro']:.3f}±{base.desvio['todas_s_f1_micro']:.3f}  "
                  f"ind {base.media['ind_s_f1_micro']:.3f}  comb {base.media['comb_s_f1_micro']:.3f}  "
                  f"exact {base.media['comb_s_exact_match']:.3f}  nodos {base.media['nodos_totales']:.0f}", flush=True)
        rep = run_repetido(ds, cfg, n_semillas=a.semillas)
        f = fila(nombre, grupo, rep, base, ds, mapa)
        filas.append(f)
        print(f"{grupo:10s} {nombre:22s} todas {f['dif_todas']:+.3f} {f['ic_todas']} | comb {f['dif_comb']:+.3f} {f['ic_comb']} | "
              f"exact {f['dif_exact']:+.3f} {f['ic_exact']} | ind {f['dif_ind']:+.3f} | gana {f['gana_semillas']} | "
              f"nodos {f['nodos']:.0f} | {time.time() - t0:.0f} s", flush=True)
        os.makedirs(os.path.dirname(os.path.abspath(a.salida)), exist_ok=True)
        pd.DataFrame(filas).to_csv(a.salida, index=False)
    print("fin", flush=True)


if __name__ == "__main__":
    main()
