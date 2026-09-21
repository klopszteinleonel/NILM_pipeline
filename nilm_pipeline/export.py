"""
Export del modelo y de la configuración del pipeline a C para el ESP32-S3,
más un test de paridad compilado en el host.

Por qué un generador propio y no emlearn: el `predict_proba` de emlearn
(método inline) hace voto mayoritario por árbol (cada árbol aporta 0 o 1),
mientras que sklearn promedia las fracciones de clase de cada hoja. Con
umbrales de decisión las dos cosas no coinciden. El método `loadable` además
cuantiza las features a int16. Acá se emite cada bosque como un array de
nodos compactos con la probabilidad en la hoja y comparación `<=` (la de
sklearn), de modo que la salida C es bit-a-bit la de `predict_proba`.

Archivos generados en `carpeta`:
  nilm_modelo.h          bosques (un array de nodos por clase) + nilm_predict_proba()
  nilm_config.h          #defines del pipeline, LUT de ventana, biquads, umbrales
  pipeline_config.json   lo mismo en JSON + nombres de features (para Python/docs)
  modelo.joblib          modelo sklearn + cfg + nombres + clases
  paridad.c / paridad    programa de paridad (si se corre test_paridad)

Modelo soportado: OneVsRestClassifier de RandomForest/ExtraTrees
(`multi_salida="ovr"`, modelo "rf"|"et"). Cada clase = un bosque binario.

Formato de nodo (12 bytes): {uint16 feat; uint16 izq; uint16 der; float val}.
  feat < 65535 -> nodo interno: si features[feat] <= val ir a izq, si no a der
  feat == 65535 -> hoja: val = probabilidad de "clase activa"
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import joblib
import numpy as np

from .config import Config
from .features import ventana_fft
from .preprocess import disenar_filtro

_HOJA = 65535


def _umbral_f32(thr: float) -> float:
    """Mayor float32 <= thr. sklearn evalúa `f32 <= thr64`; con este redondeo
    hacia abajo el conjunto de features que van a la izquierda es idéntico."""
    t32 = np.float32(thr)
    if float(t32) > thr:
        t32 = np.nextafter(t32, np.float32(-np.inf))
    return float(t32)


# ------------------------------------------------------------- árboles ----

def _nodos_arbol(tree) -> list:
    """Lista de (feat, izq, der, val) por nodo de un DecisionTree binario."""
    t = tree.tree_
    valores = t.value[:, 0, :]
    suma = valores.sum(axis=1, keepdims=True)
    proba1 = np.where(suma > 0, valores / np.where(suma > 0, suma, 1.0), 0.0)
    idx1 = list(tree.classes_).index(1) if 1 in tree.classes_ else None
    nodos = []
    for i in range(t.node_count):
        if t.children_left[i] == -1:
            p = float(proba1[i, idx1]) if idx1 is not None else 0.0
            nodos.append((_HOJA, 0, 0, p))
        else:
            nodos.append((int(t.feature[i]), int(t.children_left[i]),
                          int(t.children_right[i]), _umbral_f32(float(t.threshold[i]))))
    return nodos


def _bosque_c(nombre: str, estimador) -> tuple[str, int, int]:
    """Código C de un bosque binario. Devuelve (código, n_arboles, n_nodos)."""
    arboles = getattr(estimador, "estimators_", None)
    if arboles is None:                      # _ConstantPredictor (clase ausente en train)
        p = float(np.ravel(getattr(estimador, "y_", [0]))[0])
        return (f"static float nilm_proba_{nombre}(const float *f) {{ (void)f; return {p:.6f}f; }}\n", 0, 0)
    nodos, raices = [], []
    for a in arboles:
        raices.append(len(nodos))
        base = len(nodos)
        for feat, izq, der, val in _nodos_arbol(a):
            if feat == _HOJA:
                nodos.append((feat, 0, 0, val))
            else:
                nodos.append((feat, izq + base, der + base, val))
    if len(nodos) >= 65535:
        raise ValueError(f"bosque {nombre}: {len(nodos)} nodos supera uint16; reducir n_estimators/max_depth")
    lineas = [f"static const nilm_nodo_t nilm_nodos_{nombre}[{len(nodos)}] = {{"]
    for feat, izq, der, val in nodos:
        lineas.append(f"  {{{feat},{izq},{der},{val!r}f}},")
    lineas.append("};")
    lineas.append(f"static const uint16_t nilm_raices_{nombre}[{len(raices)}] = {{"
                  + ",".join(map(str, raices)) + "};")
    lineas.append(f"static float nilm_proba_{nombre}(const float *f) {{\n"
                  f"  return nilm_eval_bosque(nilm_nodos_{nombre}, nilm_raices_{nombre}, {len(raices)}, f);\n}}\n")
    return "\n".join(lineas), len(raices), len(nodos)


_PREAMBULO_MODELO = """// Generado por nilm_pipeline.export -- NO editar a mano
#pragma once
#include <stdint.h>
#include "nilm_config.h"

typedef struct { uint16_t feat; uint16_t izq; uint16_t der; float val; } nilm_nodo_t;

static inline float nilm_eval_arbol(const nilm_nodo_t *n, uint16_t i, const float *f) {
  while (n[i].feat != 65535) i = (f[n[i].feat] <= n[i].val) ? n[i].izq : n[i].der;
  return n[i].val;
}

static float nilm_eval_bosque(const nilm_nodo_t *n, const uint16_t *raices, int n_arboles, const float *f) {
  float s = 0.0f;
  for (int k = 0; k < n_arboles; k++) s += nilm_eval_arbol(n, raices[k], f);
  return s / (float)n_arboles;
}
"""


def _identificador(s: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in s)


def generar_modelo_h(modelo, clases: list) -> tuple[str, dict]:
    if not hasattr(modelo, "estimators_") or len(modelo.estimators_) != len(clases):
        raise ValueError("se espera OneVsRestClassifier con un estimador por clase (multi_salida='ovr')")
    partes, costo = [_PREAMBULO_MODELO], {"n_arboles": 0, "nodos_totales": 0}
    for c, est in zip(clases, modelo.estimators_):
        codigo, na, nn = _bosque_c(_identificador(c), est)
        partes.append(codigo)
        costo["n_arboles"] += na
        costo["nodos_totales"] += nn
    idents = [_identificador(c) for c in clases]
    partes.append("// Probabilidad de cada clase (orden NILM_CLASES). f: NILM_N_FEATURES floats.\n"
                  "static void nilm_predict_proba(const float *f, float *out) {")
    for j, ident in enumerate(idents):
        partes.append(f"  out[{j}] = nilm_proba_{ident}(f);")
    partes.append("}\n\n// Clases activas aplicando NILM_UMBRALES. Devuelve la cantidad de activas.\n"
                  "static int nilm_predict(const float *f, uint8_t *activas) {\n"
                  "  float p[NILM_N_CLASES]; int n = 0;\n"
                  "  nilm_predict_proba(f, p);\n"
                  "  for (int j = 0; j < NILM_N_CLASES; j++) { activas[j] = p[j] >= NILM_UMBRALES[j]; n += activas[j]; }\n"
                  "  return n;\n}\n")
    return "\n".join(partes), costo


# ------------------------------------------------------------- config -----

def _arr_c(nombre: str, valores, tipo: str = "float", por_linea: int = 8) -> str:
    vals = [f"{float(v)!r}f" if tipo == "float" else str(int(v)) for v in np.ravel(valores)]
    filas = [", ".join(vals[i:i + por_linea]) for i in range(0, len(vals), por_linea)]
    return f"static const {tipo} {nombre}[{len(vals)}] = {{\n  " + ",\n  ".join(filas) + "\n};\n"


def config_pipeline(cfg: Config, fs: float, nombres: list, clases: list, umbrales) -> dict:
    sos = disenar_filtro(cfg, fs)
    return {
        "fs": fs, "f_red": cfg.f_red, "W": cfg.W, "paso": cfg.paso, "n_fft": cfg.n_fft_efectivo,
        "ventana": cfg.ventana, "quitar_dc": cfg.quitar_dc, "normalizar": cfg.normalizar,
        "filtro": cfg.filtro, "filtro_fc": list(cfg.filtro_fc) if cfg.filtro else [],
        "filtro_causal_requerido": True,
        "sos": sos.tolist() if sos is not None else [],
        "estrategia": cfg.estrategia, "n_armonicas": cfg.n_armonicas,
        "f0_estimar": cfg.f0_estimar, "armonica_tol_bins": cfg.armonica_tol_bins,
        "normalizar_forma": cfg.normalizar_forma, "bandas_hz": list(cfg.bandas_hz),
        "rawfft_fmax": cfg.rawfft_fmax, "rawfft_bins": cfg.rawfft_bins,
        "clases": list(clases), "umbrales": [float(u) for u in np.broadcast_to(umbrales, (len(clases),))],
        "n_features": len(nombres), "features": list(nombres),
        "modelo": cfg.modelo, "multi_salida": cfg.multi_salida,
        "n_estimators": cfg.n_estimators, "max_depth": cfg.max_depth,
        "nota_causal": "si cfg.causal=False el filtro se evaluó con filtfilt (fase cero); "
                       "el borde solo puede aplicar sosfilt causal: verificar métricas con causal=True",
    }


def generar_config_h(pc: dict) -> str:
    L = ["// Generado por nilm_pipeline.export -- NO editar a mano", "#pragma once", "#include <stdint.h>", ""]
    d = lambda k, v: L.append(f"#define {k} {v}")
    d("NILM_FS", f"{pc['fs']!r}f"); d("NILM_F_RED", f"{pc['f_red']!r}f")
    d("NILM_W", pc["W"]); d("NILM_PASO", pc["paso"]); d("NILM_N_FFT", pc["n_fft"])
    d("NILM_QUITAR_DC", int(pc["quitar_dc"]))
    d("NILM_NORMALIZAR", {None: 0, "rms": 1, "maxabs": 2}[pc["normalizar"]])
    d("NILM_N_ARMONICAS", pc["n_armonicas"]); d("NILM_F0_ESTIMAR", int(pc["f0_estimar"]))
    d("NILM_ARMONICA_TOL_BINS", pc["armonica_tol_bins"]); d("NILM_NORMALIZAR_FORMA", int(pc["normalizar_forma"]))
    d("NILM_ESTRATEGIA_RAWFFT", int(pc["estrategia"] == "rawfft"))
    d("NILM_RAWFFT_FMAX", f"{pc['rawfft_fmax']!r}f"); d("NILM_RAWFFT_BINS", pc["rawfft_bins"])
    d("NILM_N_BANDAS", len(pc["bandas_hz"]) - 1)
    d("NILM_N_CLASES", len(pc["clases"])); d("NILM_N_FEATURES", pc["n_features"])
    d("NILM_N_SOS", len(pc["sos"]))
    L.append("")
    L.append("static const char *NILM_CLASES[NILM_N_CLASES] = {" + ", ".join(f'"{c}"' for c in pc["clases"]) + "};")
    L.append(_arr_c("NILM_UMBRALES", pc["umbrales"]))
    L.append(_arr_c("NILM_BANDAS_HZ", pc["bandas_hz"]))
    L.append(f"static const char *NILM_VENTANA_NOMBRE = \"{pc['ventana']}\";")
    L.append(_arr_c("NILM_VENTANA_LUT", ventana_fft(pc["ventana"], pc["W"])))
    if pc["sos"]:
        L.append("// Secciones de 2do orden [b0 b1 b2 a0 a1 a2] (aplicar en cascada, forma directa II)")
        L.append(_arr_c("NILM_SOS", np.asarray(pc["sos"]).ravel()))
    L.append("// Nombres de features (orden del vector de entrada)")
    L.append("static const char *NILM_FEATURES[NILM_N_FEATURES] = {" + ", ".join(f'"{n}"' for n in pc["features"]) + "};")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------- export -----

def exportar(modelo, cfg: Config, fs: float, nombres: list, clases: list, carpeta: str | Path,
             umbrales=None) -> dict:
    """Escribe nilm_modelo.h, nilm_config.h, pipeline_config.json y modelo.joblib.
    Devuelve dict con rutas, costo y tamaños en KB."""
    carpeta = Path(carpeta).resolve()
    carpeta.mkdir(parents=True, exist_ok=True)
    umbrales = cfg.umbral if umbrales is None else umbrales
    pc = config_pipeline(cfg, fs, nombres, clases, umbrales)
    modelo_h, costo = generar_modelo_h(modelo, clases)
    (carpeta / "nilm_modelo.h").write_text(modelo_h)
    (carpeta / "nilm_config.h").write_text(generar_config_h(pc))
    (carpeta / "pipeline_config.json").write_text(json.dumps(pc, indent=2, ensure_ascii=False))
    joblib.dump({"modelo": modelo, "cfg": cfg.to_dict(), "nombres": nombres, "clases": clases,
                 "fs": fs, "umbrales": pc["umbrales"]}, carpeta / "modelo.joblib")
    kb = lambda n: round((carpeta / n).stat().st_size / 1024, 1)
    return {"carpeta": str(carpeta), **costo,
            "kb_modelo_h": kb("nilm_modelo.h"), "kb_config_h": kb("nilm_config.h"),
            "kb_nodos_binario": round(costo["nodos_totales"] * 12 / 1024, 1)}


# ------------------------------------------------------------- paridad ----

_PARIDAD_C = r"""// Test de paridad host: lee vectores de features y escribe probabilidades.
#include <stdio.h>
#include <stdlib.h>
#include "nilm_modelo.h"

int main(int argc, char **argv) {
  if (argc < 3) { fprintf(stderr, "uso: paridad entrada.txt salida.txt\n"); return 2; }
  FILE *in = fopen(argv[1], "r"), *out = fopen(argv[2], "w");
  if (!in || !out) return 3;
  float f[NILM_N_FEATURES], p[NILM_N_CLASES]; uint8_t a[NILM_N_CLASES];
  int n; if (fscanf(in, "%d", &n) != 1) return 4;
  for (int i = 0; i < n; i++) {
    for (int j = 0; j < NILM_N_FEATURES; j++) if (fscanf(in, "%f", &f[j]) != 1) return 5;
    nilm_predict_proba(f, p); nilm_predict(f, a);
    for (int j = 0; j < NILM_N_CLASES; j++) fprintf(out, "%.9g %d ", p[j], a[j]);
    fprintf(out, "\n");
  }
  fclose(in); fclose(out); return 0;
}
"""


def test_paridad(carpeta: str | Path, F: np.ndarray, P_ref: np.ndarray, umbrales=None,
                 tol: float = 1e-5, cc: str = "gcc") -> dict:
    """Compila paridad.c contra los headers exportados y compara con sklearn.
    F (n, n_features) float32; P_ref (n, C) = predecir_proba(modelo, F)."""
    carpeta = Path(carpeta).resolve()
    F = np.asarray(F, dtype=np.float32)
    P_ref = np.asarray(P_ref, dtype=np.float32)
    pc = json.loads((carpeta / "pipeline_config.json").read_text())
    umbrales = np.asarray(pc["umbrales"] if umbrales is None else umbrales, dtype=np.float32)
    if F.shape[1] != pc["n_features"]:
        raise ValueError(f"F tiene {F.shape[1]} features, el export espera {pc['n_features']}")

    (carpeta / "paridad.c").write_text(_PARIDAD_C)
    entrada, salida, binario = carpeta / "paridad_in.txt", carpeta / "paridad_out.txt", carpeta / "paridad"
    with open(entrada, "w") as fh:
        fh.write(f"{len(F)}\n")
        for fila in F:
            fh.write(" ".join(f"{float(v)!r}" for v in fila) + "\n")
    t0 = time.time()
    comp = subprocess.run([cc, "-O2", "-std=c11", "-Wall", "-o", str(binario), str(carpeta / "paridad.c"), "-lm"],
                          capture_output=True, text=True, cwd=carpeta)
    if comp.returncode != 0:
        raise RuntimeError(f"gcc falló:\n{comp.stderr}")
    t_comp = time.time() - t0
    subprocess.run([str(binario), str(entrada), str(salida)], check=True, cwd=carpeta)

    datos = np.loadtxt(salida).reshape(len(F), -1)
    entrada.unlink()
    salida.unlink()
    P_c, A_c = datos[:, 0::2].astype(np.float32), datos[:, 1::2].astype(np.uint8)
    A_ref = (P_ref >= umbrales).astype(np.uint8)
    dif = np.abs(P_c - P_ref)
    return {
        "n": int(len(F)), "max_dif_proba": float(dif.max()), "media_dif_proba": float(dif.mean()),
        "acuerdo_decisiones": float((A_c == A_ref).mean()),
        "acuerdo_vectores": float((A_c == A_ref).all(axis=1).mean()),
        "paridad_ok": bool(dif.max() <= tol and (A_c == A_ref).all()),
        "segundos_compilacion": round(t_comp, 1),
    }
