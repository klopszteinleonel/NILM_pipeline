# Pipeline NILM — clasificación/desagregación de cargas (Colab → ESP32-S3)

Pipeline de entrenamiento y experimentación para el medidor NILM monofásico
(ESP32-S3 + MCP3204, 10 kHz, red 50 Hz). Clasifica firmas de corriente con
FFT + Random Forest, con salida multi-label (qué cargas están activas), y
exporta el modelo a C con paridad exacta para correr en el borde.

## Uso en Google Colab (Drive montado)

1. Subir a Drive esta estructura:
   ```
   MyDrive/NILM/
   ├── pipeline/            # esta carpeta completa (nilm_pipeline/, notebooks/, requirements-colab.txt)
   └── capturas_limpio/     # capturas JSON del nodo, una subcarpeta por clase
   ```
2. Abrir `pipeline/notebooks/nilm_colab.ipynb` en Colab y ejecutar las celdas en orden.
   La celda 0 monta Drive, instala `requirements-colab.txt` e importa el paquete.
3. Los resultados quedan en `pipeline/outputs/` (Drive): `manifest.csv`,
   `cache_dataset.npz`, `leaderboard.csv` y `export_<experimento>/` con los
   headers C.

Para cambiar las rutas, editar `RUTA_PIPELINE` / `RUTA_DATOS` en la celda 0.

## Flujo del notebook

| Sección | Qué hace |
|---|---|
| 1 Ingesta | `cargar_dataset` → manifest con hash + cache |
| 2 Config | `Config()`: única fuente de verdad de todos los parámetros |
| 3 Sanity | señal/espectro por clase, una mezcla sintética, augment |
| 4 Features | tabla de features `compact` por clase |
| 5 Baseline | `run_experiment` → CV por captura, 3 bloques (ind / mix / real) |
| 6 Barridos | diccionario de variantes `CFG.clonar(...)` → `leaderboard.csv` |
| 7 Mejor | LOGO, matrices por clase, umbrales óptimos, importancias |
| 8 Export | `entrenar_final` → `exportar` → `test_paridad` (gcc) → robustez SNR |

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
# NILM_pipeline
