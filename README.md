# Pipeline NILM — clasificación/desagregación de cargas (Colab → ESP32-S3)

Pipeline de entrenamiento y experimentación para el medidor NILM monofásico
(ESP32-S3 + MCP3204, 10 kHz, red 50 Hz). Clasifica firmas de corriente con
FFT cruda (bins de 0–2 kHz) + Random Forest, con salida multi-label (qué cargas
están activas), y
exporta el modelo a C con paridad exacta para correr en el borde.

## Uso en Google Colab (repos de GitHub)

Código y datos viven en dos repos privados:

- `github.com/klopszteinleonel/NILM_pipeline` — este repo.
- `github.com/klopszteinleonel/NILM_local_dataset` — capturas JSON, una carpeta
  por clase, más `combinadas/` y `nada/`.

1. Crear un token de GitHub *fine-grained* (Settings → Developer settings →
   Personal access tokens → Fine-grained): acceso solo a `NILM_pipeline` y
   `NILM_local_dataset`, permiso **Contents: Read-only** (Metadata se marca
   solo), nada más. En Colab guardarlo como *secret* `GITHUB_TOKEN` (ícono de
   la llave en la barra lateral) y activar "Acceso del cuaderno".
2. Abrir `notebooks/nilm_colab.ipynb` (desde GitHub: *Archivo → Abrir → GitHub*)
   y ejecutar las celdas en orden. La celda 0 clona los dos repos en `/content/`
   (o hace `git pull` si ya están), instala `requirements-colab.txt` e importa el paquete.
3. Los resultados quedan en `/content/NILM_pipeline/outputs/` (`manifest.csv`,
   `cache_dataset.npz`, `leaderboard.csv`, `export_<experimento>/`). Esa carpeta
   es efímera: para conservar el leaderboard entre sesiones poné
   `PERSISTIR_EN_DRIVE = True` en la celda 0 (usa `MyDrive/NILM/outputs`).

Para agregar datos: subir los JSON al repo del dataset y volver a correr la
celda 0 (hace `pull`) y la de ingesta.

## Validación

El objetivo es funcionar en casas que el modelo nunca vio, así que la
validación por default es **leave-one-lugar-out**: se entrena sin un lugar y se
evalúan sus individuales y sus combinadas. Las capturas se agrupan en
**sesiones** y **ráfagas** (capturas de la misma carga con segundos de
diferencia) a partir del nombre de archivo; una ráfaga nunca se reparte entre
train y test. Cada experimento se corre con varias semillas (`run_repetido`) y
dos configuraciones se comparan de forma pareada (`comparar`). Lugares o
sesiones en `Config.test_congelado` quedan fuera de todos los barridos y se
evalúan una sola vez al final (`evaluar_test_congelado`).

## Flujo del notebook

| Sección | Qué hace |
|---|---|
| 1 Ingesta | `cargar_dataset` → manifest con hash + cache |
| 2 Config | `Config()`: única fuente de verdad de todos los parámetros |
| 3 Sanity | señal/espectro por clase, una mezcla sintética, augment |
| 4 Features | tabla de features `compact` por clase |
| 5 Baseline | `run_repetido` con LOLO (5 semillas) + referencia sgkf5 por ráfaga; bloques ind / comb / todas |
| 6 Barridos | variantes `CFG.clonar(...)` con 3 semillas c/u → `leaderboard.csv` (media ± desvío) + `comparar` |
| 7 Elegido | reporte LOLO, detalle por combinación, matrices por clase, importancias |
| 8 Export | `entrenar_final` → `exportar` → `test_paridad` (gcc) → robustez SNR |
| 9 Test congelado | `evaluar_test_congelado` una sola vez sobre lo reservado |

## Datos

- Formato JSON del nodo: `{categoria, lugar, fs, duracion, muestras, formato, k, bytes, datos[]}`.
- Individuales: `capturas_limpio/<clase>/*.json` con `categoria="aire"`.
- Combinadas reales: `capturas_limpio/combinadas/*.json` con `categoria="aire,tele"`
  (solo se usan para validación/test).
- Sin carga: `categoria="nada"` → etiqueta vector cero (ejemplos negativos y fondo).
- La clase sale del campo `categoria`, no de la carpeta. Agregar datos = copiar
  archivos y volver a correr la celda de ingesta.

## Salida para el firmware (`outputs/export_*/`)

- `nilm_modelo.h` — un bosque binario por clase como arrays de nodos
  (12 bytes/nodo) + `nilm_predict_proba(f, out)` y `nilm_predict(f, activas)`.
- `nilm_config.h` — `#define`s (fs, W, paso, n_fft…), LUT de la ventana,
  biquads del filtro (si hay), umbrales por clase, nombres de clases y features.
- `pipeline_config.json` — lo mismo en JSON.
- `modelo.joblib` — modelo sklearn + config para reproducir en Python.

El firmware debe calcular el mismo vector de features (mismo W, ventana,
normalización del espectro `2·|X|/Σw`, f0 estimada por interpolación
parabólica en log-magnitud, armónicas como máximo en ±tol bins) y pasarlo a
`nilm_predict`. `test_paridad` garantiza que, dadas las mismas features, el C
devuelve exactamente lo mismo que sklearn.

## Uso local

```bash
cd pipeline
.venv/bin/python3 -c "from nilm_pipeline import *; ..."
```
El venv local (`.venv/`) tiene numpy, scipy, scikit-learn, pandas, joblib y
matplotlib. `gcc` es necesario para `test_paridad`. Ver `AGENTS.md` para las
decisiones de diseño, hechos medidos sobre las señales y resultados de referencia.
