# Pipeline NILM — guía para agentes

## Proyecto
Medidor de consumo no invasivo (NILM) monofásico sobre ESP32-S3 + MCP3204.
Desagregación por clasificación de firmas eléctricas de corriente (FFT + ML).
Flujo: entrenamiento/experimentación en Google Colab → export a C para el borde.

## Estructura
- `nilm_pipeline/` — paquete Python con las etapas del pipeline:
  - `config.py` — dataclass `Config`: ÚNICA fuente de verdad de parámetros
    (valida en `__post_init__`: W potencia de 2, nombres válidos, etc.).
  - `data.py` — manifest + loader multi-label + cache .npz.
  - `preprocess.py` — filtro IIR (sos) sobre la captura; DC/normalización por
    ventana (devuelve el factor de escala para recuperar amplitudes absolutas).
  - `segment.py` — ventanas W×solape; devuelve también `idx_captura`.
  - `mix.py` — combinadas sintéticas (2–3 cargas) con sesgo grande+chica y
    fondo "sin carga"; `Mezclas.origenes` para auditar anti-leakage.
  - `augment.py` — sobre ventanas de train: ruido SNR, escala ±5 %, shift,
    hum, jitter de f_red; balanceo por clase con tope; hereda grupo/idx.
  - `features.py` — `compact` (42 feats: tiempo, armónicas con f0 estimado
    en log-parabólica y tolerancia ±1 bin, estadísticos, bandas) y `rawfft`
    (410 bins + rms/pico). `extraer_features(V, escala, cfg, fs)`.
  - `evaluate.py` — métricas multi-label por ventana y por captura
    (`agregar_por_grupo`), umbrales por clase, importancias, `reporte`.
  - `train.py` — `run_experiment(ds, cfg, leaderboard)`: CV por grupos con
    mix dentro del fold y 3 bloques (ind/mix/real); `entrenar_final`;
    `costo_modelo`; `robustez_snr`; leaderboard CSV (config JSON + métricas).
  - `export.py` — `exportar(modelo, cfg, fs, nombres, clases, carpeta)` genera
    `nilm_modelo.h` (bosques como arrays de nodos de 12 bytes, un bosque por
    clase, `nilm_predict_proba`/`nilm_predict`), `nilm_config.h` (#defines,
    LUT de ventana, biquads, umbrales, nombres), `pipeline_config.json`,
    `modelo.joblib`. `test_paridad(carpeta, F, P_ref)` compila `paridad.c`
    con gcc y compara con sklearn (paridad exacta, dif < 1e-6).
    Generador C propio, NO emlearn: emlearn `inline` hace voto por árbol
    (0/1) en vez de promediar fracciones de hoja → probabilidades distintas
    a sklearn; `loadable` cuantiza a int16. Umbrales de nodo redondeados hacia
    abajo a float32 para preservar `f32 <= thr64`.

## Flujo por fold (para train.py)
```
individuales(train) ─► mix.py (mix_total) ─┐
                                           ├► segmentar ─► preprocesar_ventanas ─► augment ─► features ─► fit
individuales(train) ────────────────────────┘
individuales(test) ─► mix.py (mix_test_total) ─┐
individuales(test) ─────────────────────────────┼► segmentar ─► preprocesar_ventanas ─► features ─► eval (3 bloques)
combinadas reales ──────────────────────────────┘
```
El filtro (`preprocesar_captura`) va sobre la captura completa antes de segmentar.

## Otros archivos
- `nilm_pipeline/plots.py` — gráficos de sanity check y resultados para el notebook.
- `notebooks/nilm_colab.ipynb` — notebook delgado (se genera/edita a mano;
  la celda 0 detecta Colab+Drive vs local). Ejecutable localmente con el venv
  desde `notebooks/` (rutas relativas).
- `README.md` — uso en Colab con Drive montado y descripción del export.
- `outputs/` — artefactos locales (manifest.csv, leaderboard.csv, caches). No versionar.
- `requirements-colab.txt` — deps para Colab.

## Datos
- Carpeta de capturas: `../clienteTCP/capturas_limpio/` (fuera de este directorio).
- Formato JSON del nodo: `{categoria, lugar, canal, magnitud, unidad, fs=10000,
  duracion, muestras, formato, k, bytes, datos[]}` (1 s típica = 10000 muestras).
  `canal=0/magnitud="corriente"/unidad="A"` (CH0) o `canal=1/"tension"/"V"` (CH1).
  Capturas viejas no tienen esos 3 campos → se asumen corriente.
- Desde 2026-09-20 el firmware muestrea CH1 (tensión): comando `"canal":"i"|"v"|"iv"`;
  con `iv` ambos canales se leen en el mismo tick del timer (sincronizados,
  ~20 µs entre sí) y llegan como dos objetos JSON separados por `\n`.
  `capturar.py --canal iv` guarda `<ts>_<cat>.json` (I) y `<ts>_<cat>_v.json` (V);
  `graficar.py --par` superpone el canal compañero. `data.py` omite archivos
  con `magnitud != "corriente"` (el pipeline usa solo corriente por ahora;
  V+I sincronizadas habilitan potencia/fase como extensión futura).
- Red 50 Hz. Nyquist 5 kHz.
- Convención multi-label: `categoria="aire,tele"` (separador coma) para
  combinadas reales, guardadas en subcarpeta `combinadas/`.
- `categoria="nada"` = captura sin carga (ruido de fondo) → etiqueta vector
  cero, tipo `sin_carga`. NO es una clase. Pendiente: el usuario capturará ~10.
- Fuente de verdad de la clase = campo `categoria` del JSON (no la carpeta;
  ej: carpeta `hidromasaje/` pero clase `hidro`).
- Hay capturas de 1 s (10000) y de 2 s (20000 muestras).
- Cada captura es su propio grupo (anti-leakage en CV).

## Hechos de las señales (medidos 2026-09-19)
- Resistivas puras (calefon 6.4 A, plancha 6.15 A, horno 2.8 A, secadorpelo
  2.4 A) tienen THD ≈ 0.02: se distinguen SOLO por amplitud absoluta.
  Limitación conocida: dos resistivas de potencia similar son indistinguibles
  en régimen permanente (extensión futura: transitorios de encendido).
- Cargas chicas (heladera/tele/luces ~0.45 A) quedan enterradas en mezclas con
  aire/calefon: es el caso difícil y el mix lo fuerza (sesgo grande+chica).
- Un HP Butterworth orden 4 con fc=40 Hz atenúa la fundamental 7.5 % → usar
  fc ≤ 20 Hz.
- Con f0 estimado + tolerancia ±1 bin, un jitter de red de ±0.5 Hz mueve
  H_k/H1 ~1 % en promedio; con bin fijo, 4.4 % promedio y hasta 63 % en las
  armónicas altas. `rect` y `flattop` son solo experimentos (fuga / lóbulo ancho).
- `rawfft` entre distintos W tiene la misma dimensión pero no los mismos
  valores (corr ~0.87 entre W vecinos): un modelo = un W.

## Decisiones de diseño (plan aprobado + revisión 2026-09-19)
- PRINCIPIO RECTOR: el pipeline se diseña en base a lo que puede correr el
  ESP32-S3. W y n_fft SOLO potencias de 2 (esp-dsp radix-2 float32). Sin
  `filtfilt` en el camino final (si un filtro gana, se exporta como biquads
  causales). Sin `StandardScaler` para árboles.
- Orden de operaciones (paridad borde): filtro causal sobre la captura →
  segmentar → DC y normalización por ventana.
- Salida multi-label (vector binario de clases activas).
- Features `compact`: las absolutas (RMS, |H1| en A, pico) SIEMPRE presentes;
  `normalizar` afecta solo la forma armónica (H_k/H1). f0 se estima (interp.
  parabólica) y H_k = máximo en ±`armonica_tol_bins` alrededor de k·f0.
- Escalado en augment y mix ±5 % (`(0.95, 1.05)`): más borra la diferencia
  entre resistivas. Sin `flip` (redundante con shift para magnitudes).
- Mix sintético 2–3 cargas a nivel captura, generado DENTRO del fold: desde
  train (`mix_total`) y también desde test (`mix_test_total`) para medir
  multi-label ya. Combinadas reales SOLO validación/test.
- Leaderboard con 3 bloques: individuales / mix-sintético / combinadas-reales,
  métricas por ventana y agregadas por captura (media de probas), y costo del
  modelo (n_features, n_arboles, nodos, prof_max, KB del .h).
- Modelo exportable = `OneVsRest(RandomForest)` (`multi_salida="ovr"`,
  default n_estimators=20, max_depth=8 desde el paso 6); RF multi-output
  nativo solo referencia.
- Validación: StratifiedGroupKFold(5) estratificado por la clase de la
  individual (y 1-D); LOGO definitivo. Combinadas reales fuera de los folds.
- Estrategias comparadas: `compact` vs `rawfft`.
- Export: un RF binario por clase como arrays C propios + test de paridad en
  host (gcc). Costo en flash ≈ 12 bytes/nodo: baseline 50×10 = 3.2 MB (NO
  entra cómodo), OvR 20×8 = 276 KB (OK). Elegir por leaderboard mirando
  `nodos_totales`.

## Resultados de referencia (61 capturas, SGKF5, 2026-09-19)
Config del paso 4 = compact, W=2048, hann, OvR-RF 50×10, mix 400, aug 4 balanceado
(el default actual es 20×8; ver barrido más abajo):
- individuales por captura: exact 0.74, F1-micro 0.82, F1-macro 0.63
- mix sintético por captura: exact 0.23, F1-micro 0.66 (el caso difícil)
- clases con 1 captura (hidro, secadorpelo) y luces (2) dan F1 0: no hay
  forma de aprenderlas con CV por grupo → falta capturar.
- costo: 550 árboles, 276 k nodos (grande para el ESP32-S3; barrer
  n_estimators/max_depth). OvR 20×8 → 23 k nodos con F1-micro 0.855.
- OvR supera al RF multi-output nativo (0.855 vs 0.75) → confirma decisión G.
- Umbrales por clase optimizados sobre OOF suben F1-macro 0.65 → 0.72.
- Tiempos: SGKF5 baseline ~60 s; LOGO con config chica ~35 s; `hgb` OvR es
  lento (~140 s), solo referencia.
- Con `cv="logo"` el bloque mix queda vacío (1 captura de test no alcanza).

Barrido del notebook (mismo dataset, `Config()` = RF 20×8, mix_c_f1_micro /
ind_c_f1_micro / nodos): rawfft 0.694/0.777/65k · rf50x10 0.663/0.824/276k ·
baseline 0.652/0.789/55k · blackman 0.649/0.783/54k · W4096 0.645/0.772/45k ·
hp20_causal 0.642/0.806/55k · sin_aug 0.636/0.830/26k · W1024 0.616/0.778/64k ·
aug8 0.613/0.778/60k · et 0.568/0.583/38k. Lecturas: rawfft ayuda en mezclas
pero cuesta 412 features; el augment ayuda a mezclas y perjudica individuales;
ExtraTrees rinde mal. Todo con 61 capturas: reevaluar cuando haya más datos.

Lección del test de paridad: atrapó un `uint8_t` para el índice de feature
(rawfft tiene 412) que daba 74 % de acuerdo. Ahora `uint16_t`.

## Entorno local
- `pipeline/.venv` (Python 3.14, numpy/scipy/sklearn 1.9/pandas/joblib/
  matplotlib). Usar `.venv/bin/python3` desde la raíz de `pipeline/`.
- NO tocar `Graficar/.venv` (es de otro subproyecto).
- `gcc` del sistema para el test de paridad (en Colab también hay gcc).
- `emlearn` quedó instalado en el venv pero no se usa (requiere `setuptools`
  por `distutils` en Python ≥ 3.12).

## Estado (2026-09-19)
Pasos 1–6 hechos: paquete completo, notebook Colab (Drive montado) ejecutado
localmente de punta a punta, README. Default de `Config` pasó a RF 20×8.
Pendientes del usuario: subir `pipeline/` y `capturas_limpio/` a
`MyDrive/NILM/`, capturar registros `categoria="nada"` y combinadas reales.

## Captura de combinadas reales y sin carga
- `clienteTCP/capturar.py` acepta comas desde 2026-09-20:
  `--categoria "aire,tele"` (archivo `<ts>_aire+tele.json`, la coma se guarda
  en el JSON) y `--categoria nada` para ruido de fondo.
- Mover las combinadas a `capturas_limpio/combinadas/` y las "nada" a
  cualquier carpeta (ej. `capturas_limpio/nada/`).
