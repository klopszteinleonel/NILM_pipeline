import json, sys

celdas = []
def md(s): celdas.append({"cell_type": "markdown", "metadata": {}, "source": s.strip("\n")})
def code(s): celdas.append({"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": s.strip("\n")})

md("""
# Pipeline NILM — experimentación en Colab

Notebook delgado: toda la lógica vive en el paquete `nilm_pipeline/` (repo `NILM_pipeline`).
Cada experimento es una instancia de `Config`; los resultados se acumulan en `outputs/leaderboard.csv`.

**Fuentes (GitHub, repos privados)**:
- código: `github.com/klopszteinleonel/NILM_pipeline`
- datos:  `github.com/klopszteinleonel/NILM_local_dataset` (una carpeta por clase, `combinadas/`, `nada/`)

**Antes de correr en Colab**: crear un *secret* llamado `GITHUB_TOKEN` (ícono de la llave, barra lateral)
con un token de GitHub con permiso de lectura sobre los dos repos, y habilitarlo para este notebook.
La celda 0 clona ambos repos en `/content/` (o hace `git pull` si ya están) e instala dependencias.
`outputs/` en Colab es efímero: para conservar el leaderboard entre sesiones poné `PERSISTIR_EN_DRIVE = True`.

Principio rector: todo lo que hace el pipeline tiene que poder correr en el ESP32-S3 (ver `AGENTS.md`).
""")

code("""
# 0. Setup: en Colab clona los repos de GitHub; en local usa rutas relativas
import os, sys, subprocess

REPO_PIPELINE = "klopszteinleonel/NILM_pipeline"
REPO_DATOS    = "klopszteinleonel/NILM_local_dataset"
PERSISTIR_EN_DRIVE = False     # True: outputs/ en MyDrive/NILM/outputs (sobrevive a la sesión)

def clonar_o_actualizar(repo, destino, token):
    url = f"https://{'x-access-token:' + token + '@' if token else ''}github.com/{repo}.git"
    if os.path.isdir(os.path.join(destino, ".git")):
        cmd = ["git", "-C", destino, "pull", "--ff-only", "-q"]
    else:
        cmd = ["git", "clone", "-q", "--depth", "1", url, destino]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        detalle = r.stderr.replace(token, "<token>") if token else r.stderr
        raise RuntimeError(f"git falló para {repo}: {detalle.strip()} -- "
                           "Si dice 'not found': el token no tiene acceso a ese repo "
                           "(Repository access -> incluir los dos repos, Contents: Read-only).")
    print(f"{repo} -> {destino}")

try:
    from google.colab import userdata
    try:
        TOKEN = userdata.get("GITHUB_TOKEN")
    except Exception:
        TOKEN = ""          # sin secret: solo funciona si los repos son públicos
        print("[AVISO] No hay secret GITHUB_TOKEN; se intenta clonar sin token")
    RUTA_PIPELINE = "/content/NILM_pipeline"
    RUTA_DATOS    = "/content/NILM_local_dataset"
    clonar_o_actualizar(REPO_PIPELINE, RUTA_PIPELINE, TOKEN)
    clonar_o_actualizar(REPO_DATOS, RUTA_DATOS, TOKEN)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r",
                    f"{RUTA_PIPELINE}/requirements-colab.txt"], check=True)
    if PERSISTIR_EN_DRIVE:
        from google.colab import drive
        drive.mount("/content/drive")
        RUTA_SALIDAS = "/content/drive/MyDrive/NILM/outputs"
    else:
        RUTA_SALIDAS = os.path.join(RUTA_PIPELINE, "outputs")
    EN_COLAB = True
except ImportError:
    RUTA_PIPELINE = os.path.abspath(os.path.join(os.getcwd(), ".."))
    RUTA_DATOS    = os.path.abspath(os.path.join(RUTA_PIPELINE, "..", "clienteTCP", "capturas_limpio"))
    RUTA_SALIDAS  = os.path.join(RUTA_PIPELINE, "outputs")
    EN_COLAB = False
os.makedirs(RUTA_SALIDAS, exist_ok=True)
sys.path.insert(0, RUTA_PIPELINE)

import numpy as np, pandas as pd, matplotlib.pyplot as plt
import nilm_pipeline
from nilm_pipeline import *
from nilm_pipeline import plots
print(f"nilm_pipeline {nilm_pipeline.__version__} | colab={EN_COLAB}\\ndatos:   {RUTA_DATOS}\\nsalidas: {RUTA_SALIDAS}")
""")

md("""
## 1. Ingesta
Escanea la carpeta de capturas, arma el manifest (con hash del dataset) y una cache `.npz`.
Para agregar datos: copiar los JSON a la carpeta que corresponda y volver a correr esta celda.
""")
code("""
ds = cargar_dataset(RUTA_DATOS)
print(ds.resumen())
guardar_manifest(ds.manifest_filas, os.path.join(RUTA_SALIDAS, "manifest.csv"))
guardar_cache(ds, os.path.join(RUTA_SALIDAS, "cache_dataset.npz"))
fs = ds.fs()
sen, Y, G = ds.senales(), ds.y(), ds.grupos()
""")

md("""
## 2. Configuración base
`Config` es la única fuente de verdad. Base: **FFT cruda** (solo los 614 bins directos de la FFT de W=2048 hasta 3 kHz),
sin augment, 100 mezclas sintéticas por fold con fundamentales exactamente en fase (solo para entrenar), OvR de RandomForest 20×8.
El test son siempre capturas reales; la validación por default es leave-one-lugar-out (casa nunca vista).
`AGRUPAR` fusiona clases físicamente indistinguibles (resistivas puras de ~6 A: misma amplitud, THD ≈ 0.02);
dejalo vacío (`{}`) para evaluarlas por separado. `airecalor` se fusiona en `aire` (mismo equipo) y `parasito`
se excluye (`EXCLUIR`): el standby se va a estimar como consumo base, no como clase.
""")
code("""
AGRUPAR = {"calefon": "resistiva", "horno": "resistiva", "pava": "resistiva", "plancha": "resistiva",
           "airecalor": "aire"}          # aire y airecalor = el mismo equipo en dos modos
EXCLUIR = ("parasito",)                  # standby: se medirá como consumo base, no se clasifica
CFG = Config(agrupar=AGRUPAR, excluir_clases=EXCLUIR)
print(CFG.nombre())
pd.Series({k: v for k, v in CFG.to_dict().items() if k != "bandas_hz"}).to_frame("valor").T
""")

md("""
## 3. Sanity checks visuales
Señal, detalle y espectro (con las armónicas de la f0 estimada) de una captura por clase; una mezcla sintética; augment.
""")
code("""
for clase in ("aire", "tele", "calefon"):
    r = next((r for r in ds.registros if r.clases == (clase,)), None)
    if r is not None:
        plots.plot_captura(r.datos, fs, CFG, titulo=f"{clase} — {r.ruta}")
plt.show()
""")
code("""
rng = np.random.default_rng(CFG.seed)
m = sintetizar_combinadas(sen, Y, G, ds.clases, CFG, rng, total=3, fs=fs)
i = 0
origen = [j for j in range(len(G)) if G[j] in m.origenes[i]]
print("mezcla 0 =", [c for c, b in zip(ds.clases, m.Y[i]) if b], "| fuentes:", m.origenes[i])
plots.plot_mix([sen[j] for j in origen], [G[j].split('/')[-1][:24] for j in origen], m.senales[i], fs)
V = segmentar(sen[0], CFG.W, CFG.solape)
Vp, esc = preprocesar_ventanas(V, CFG)
plots.plot_augment(Vp[0], CFG, fs)
plt.show()
""")

md("""
## 4. Features
Vector `rawfft` de una ventana por clase (espectro directo) y, como referencia, la mediana por clase de las features `compact`.
La amplitud absoluta (`rms_A`, `h1_A`) es lo único que separa a las resistivas puras entre sí.
""")
code("""
X, Yw, Gw, idx = segmentar_dataset(sen, Y, G, CFG)
Vp, esc = preprocesar_ventanas(X, CFG)
F, nombres = extraer_features(Vp, esc, CFG, fs)
print(f"rawfft: {len(F)} ventanas x {len(nombres)} features ({nombres[0]} … {nombres[-1]})")
fig, ax = plt.subplots(figsize=(12, 3.5))
freqs = np.array([float(n[4:-2]) for n in nombres])
for clase in ("aire", "tele", "lavarropa", "calefon"):
    i = next((k for k in range(len(Yw)) if Yw[k].sum() == 1 and ds.clases[Yw[k].argmax()] == clase), None)
    if i is not None:
        ax.semilogy(freqs, F[i] + 1e-4, lw=0.8, label=f"{clase} (rms {np.sqrt(np.mean(Vp[i]**2)):.2f} A)")
ax.set(xlabel="f [Hz]", ylabel="|I| [A]", title="vector rawfft de una ventana por clase"); ax.legend(fontsize=8)
plt.show()

Fc, nc = extraer_features(Vp, esc, CFG.clonar(estrategia="compact"), fs)
df_f = pd.DataFrame(Fc, columns=nc)
df_f["clase"] = [",".join(c for c, b in zip(ds.clases, y) if b) or "nada" for y in Yw]
df_f.groupby("clase")[["rms_A", "h1_A", "thd", "h3_rel", "par_impar", "centroide_hz", "cresta"]].median().round(3)
""")

md("""
## 5. Baseline — validación que refleja el objetivo
El medidor tiene que funcionar en **casas que nunca vio**, así que la validación principal es **leave-one-lugar-out** (`cv="lolo"`):
se entrena sin un lugar y se evalúan las individuales **y** las combinadas de ese lugar. Todo el test son capturas reales.

- Bloques: `ind` (individuales), `comb` (combinadas reales), `todas` (ind + comb).
- Niveles: por ventana; por captura (media de probabilidades, umbral 0.5); **clases vistas** = sin contar aparatos que solo
  existen en la casa de test (mide "reconocer unidades nuevas de clases conocidas"; es la fila a mirar mientras haya clases de un solo lugar).
- Cada experimento se repite con varias semillas (`run_repetido`): con 59 combinadas el desvío entre semillas es del orden de
  0.02–0.06, así que **una diferencia menor que el desvío no es evidencia**. Para comparar dos configs usá `comparar(a, b)` (pareado).
- Referencia dentro de las casas conocidas: `cv="sgkf5"` agrupado por **ráfaga** (capturas casi idénticas nunca se reparten
  entre train y test). El CV por captura que usábamos antes era optimista (ind exact 0.90 vs 0.55 por ráfaga).
- Los umbrales por clase (`+umbr`) empeoran bajo validación honesta: se usa umbral fijo 0.5.
""")
code("""
LEADERBOARD = os.path.join(RUTA_SALIDAS, "leaderboard.csv")
N_SEMILLAS = 5
rep = run_repetido(ds, CFG, n_semillas=N_SEMILLAS, leaderboard=LEADERBOARD, etiqueta="base")
res = rep.resultados[0]
print(f"LOLO, {N_SEMILLAS} semillas (media ± desvío):")
for k in ("ind_s_f1_micro", "comb_s_f1_micro", "comb_s_exact_match", "todas_s_f1_micro", "todas_c_f1_micro"):
    if k in rep.media:
        print(f"  {k:22s} {rep.media[k]:.3f} ± {rep.desvio[k]:.3f}")
print("\\nDetalle de la semilla 0:\\n" + reporte(res.metricas, res.clases))
if "comb" in res.bloques:
    print("\\nCombinadas reales por combinación (casa no vista, umbral 0.5):")
    print(reporte_combinaciones(res.bloques["comb"], res.clases, 0.5))
ref = run_repetido(ds, CFG.clonar(cv="sgkf5", cv_grupo="rafaga"), n_semillas=3, etiqueta="base_sgkf_rafaga")
print("\\nReferencia dentro de casas conocidas (sgkf5 por ráfaga, 3 semillas):",
      {k: f"{ref.media[k]:.3f} ± {ref.desvio[k]:.3f}" for k in ("ind_s_f1_micro", "comb_c_f1_micro", "comb_c_exact_match")})
""")

md("""
## 6. Barridos
Cada variante es un `CFG.clonar(...)` corrido con `N_SEMILLAS_BARRIDO` semillas; el leaderboard guarda media y desvío.
Métrica principal: `todas_s_f1_micro` (LOLO, clases vistas). Otros modelos (ExtraTrees, HGB, kNN, SVM, regresión logística,
RF multi-output) ya se probaron y no superan al RF OvR 20×8; ver `AGENTS.md`.
""")
code("""
N_SEMILLAS_BARRIDO = 3
variantes = {
    # --- mezclas sintéticas
    "jitter_2hz":     CFG.clonar(mix_jitter_fase=14.4),   # ±2 Hz sobre un ciclo de 50 Hz ≡ ±14.4°
    "fase_aleatoria": CFG.clonar(mix_fase="aleatoria"),
    "sin_mix":        CFG.clonar(mix_habilitado=False),
    "mix200":         CFG.clonar(mix_total=200),
    # --- clases, features, ventana
    "sin_agrupar":    CFG.clonar(agrupar={}),
    "compact":        CFG.clonar(estrategia="compact"),
    "fmax2000":       CFG.clonar(rawfft_fmax=2000.0),
    "fmax4000":       CFG.clonar(rawfft_fmax=4000.0),
    "W4096":          CFG.clonar(W=4096),
    # --- filtros digitales (causales = implementables en el ESP32)
    "hp20":           CFG.clonar(filtro="hp", filtro_fc=(20.0,), causal=True),
    "notch50_q30":    CFG.clonar(filtro="notch", filtro_fc=(50.0,), notch_q=30.0, causal=True),
    # --- data augmentation
    "aug_blanco":     CFG.clonar(aug_n=2, ruido_tipo="blanco", snr_db=(20.0, 50.0)),
    "aug_rosa":       CFG.clonar(aug_n=2, ruido_tipo="rosa", snr_db=(20.0, 50.0)),
    "aug_impulsivo":  CFG.clonar(aug_n=2, ruido_tipo="impulsivo", snr_db=(20.0, 50.0)),
    "aug_hum":        CFG.clonar(aug_n=2, hum50=True, hum_rel=0.05, snr_db=(60.0, 60.0)),
    "aug_jitter_red": CFG.clonar(aug_n=2, jitter_red=True, jitter_hz=0.5, snr_db=(60.0, 60.0)),
    "aug_escala10":   CFG.clonar(aug_n=2, escala=(0.9, 1.1), snr_db=(60.0, 60.0)),
    # --- tamaño del RF
    "rf50x12":        CFG.clonar(n_estimators=50, max_depth=12),
    # --- combinación candidata a nueva base (lo mejor del barrido anterior)
    "faseal+escala10": CFG.clonar(mix_fase="aleatoria", aug_n=2, escala=(0.9, 1.1), snr_db=(60.0, 60.0)),
}
reps = {"base": rep}
print(f"{'variante':15s} {'todas_s':>13s} {'comb_s':>13s} {'comb_s exact':>13s} {'ind_s':>13s} {'nodos':>7s}")
for nombre, c in variantes.items():
    r = run_repetido(ds, c, n_semillas=N_SEMILLAS_BARRIDO, leaderboard=LEADERBOARD, etiqueta=nombre)
    reps[nombre] = r
    f = lambda k: f"{r.media.get(k, float('nan')):.3f}±{r.desvio.get(k, float('nan')):.3f}"
    print(f"{nombre:15s} {f('todas_s_f1_micro'):>13s} {f('comb_s_f1_micro'):>13s} {f('comb_s_exact_match'):>13s} "
          f"{f('ind_s_f1_micro'):>13s} {r.media['nodos_totales']:7.0f}")
""")
code("""
lb = cargar_leaderboard(LEADERBOARD, ordenar_por="todas_s_f1_micro")
cols = ["etiqueta", "nombre", "todas_s_f1_micro", "todas_s_f1_micro_std", "comb_s_f1_micro", "comb_s_exact_match",
        "ind_s_f1_micro", "nodos_totales", "n_semillas"]
display(lb[[c for c in cols if c in lb.columns]].head(20))
plots.plot_leaderboard(lb, y="todas_s_f1_micro", y2="comb_s_f1_micro")
plt.show()
# comparación pareada de la mejor variante contra la base (mismas semillas)
mejor_var = next(e for e in lb["etiqueta"] if e in reps and e != "base")
print(f"\\ncomparar(base, {mejor_var}) con {N_SEMILLAS_BARRIDO} semillas pareadas:")
base_n = run_repetido(ds, CFG, n_semillas=N_SEMILLAS_BARRIDO) if N_SEMILLAS != N_SEMILLAS_BARRIDO else rep
print(comparar(base_n, reps[mejor_var], ("todas_s_f1_micro", "comb_s_f1_micro", "comb_s_exact_match", "ind_s_f1_micro")))
""")

md("""
## 7. Evaluación del elegido
Elegí `MEJOR` a mano: solo cambiá la base si `comparar` muestra una mejora mayor al desvío y en todas (o casi todas) las semillas.
Si no, quedate con la base. Las matrices por clase son del LOLO (casa no vista), con umbral 0.5.
""")
code("""
MEJOR = CFG            # o: Config.from_json(lb.iloc[0]["config"])
print("MEJOR:", MEJOR.nombre())
res_m = run_experiment(ds, MEJOR, verbose=False)
print(reporte(res_m.metricas, res_m.clases))
""")
code("""
b = res_m.bloques["todas"]
Pg, Yg, _ = agregar_por_grupo(b["P"], b["Y"], b["grupos"])
if "comb" in res_m.bloques:
    print("Combinadas reales por combinación (casa no vista, umbral 0.5):")
    print(reporte_combinaciones(res_m.bloques["comb"], res_m.clases, 0.5))
plots.plot_matrices_por_clase(Yg, Pg, 0.5, res_m.clases)
plots.plot_importancias(res_m.importancias)
plt.show()
""")

md("""
## 8. Export al ESP32-S3 y test de paridad
Entrena con todas las individuales (+ mezclas + augment), genera `nilm_modelo.h`, `nilm_config.h`, `pipeline_config.json` y `modelo.joblib`, y compila un programa C en el host que debe reproducir `predict_proba` de sklearn (diferencia < 1e-5).
""")
code("""
modelo, nombres_f, clases = entrenar_final(ds, MEJOR)
CARPETA_EXPORT = os.path.join(RUTA_SALIDAS, f"export_{MEJOR.nombre()}")
info = exportar(modelo, MEJOR, fs, nombres_f, clases, CARPETA_EXPORT)   # umbral 0.5
print(info)
F_all, _, _, _ = armar_conjunto(sen, Y, G, MEJOR, fs)
paridad = test_paridad(CARPETA_EXPORT, F_all, predecir_proba(modelo, F_all, len(clases)))
print(paridad)
assert paridad["paridad_ok"], "el C exportado NO reproduce a sklearn: no flashear"
rob = robustez_snr(modelo, ds, MEJOR)
print("robustez SNR:", {k: round(v, 3) for k, v in rob.items()})
plots.plot_robustez(rob)
plt.show()
print("archivos en", CARPETA_EXPORT, ":", sorted(os.listdir(CARPETA_EXPORT)))
""")

md("""
## 9. Test congelado (una sola vez, al final)
Reservá desde el principio uno o más lugares/sesiones en `Config.test_congelado`: `run_experiment` los ignora durante
todos los barridos y solo esta celda los usa. Si se mira este resultado para elegir configuraciones, deja de ser un test.
Con los datos actuales (3 lugares) conviene esperar a tener una casa nueva y congelarla entera.
""")
code("""
CONGELADO = ()          # ej. ("casa_nueva",) — nombres de lugar o de sesión
if CONGELADO:
    print(evaluar_test_congelado(ds, MEJOR.clonar(test_congelado=CONGELADO)))
""")

nb = {"cells": celdas, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
      "language_info": {"name": "python"}, "colab": {"provenance": []}}, "nbformat": 4, "nbformat_minor": 5}
salida = sys.argv[1]
with open(salida, "w", encoding="utf-8") as f:
    json.dump(nb, f, indent=1, ensure_ascii=False)
print("escrito", salida, len(celdas), "celdas")
