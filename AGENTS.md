# Pipeline NILM — guía para agentes

## Proyecto
Medidor de consumo no invasivo (NILM) monofásico sobre ESP32-S3 + MCP3204.
Desagregación por clasificación de firmas eléctricas de corriente (FFT + ML).
Flujo: entrenamiento/experimentación en Google Colab → export a C para el borde.

## Estructura
- `nilm_pipeline/` — paquete Python con las etapas del pipeline:
  - `config.py` — dataclass `Config`: ÚNICA fuente de verdad de parámetros
    (valida en `__post_init__`: W potencia de 2, nombres válidos, etc.).
  - `data.py` — manifest + loader multi-label + cache .npz. `asignar_sesiones`
    deriva `sesion` (lugar_fecha_n, pausas > 20 min) y `rafaga` (misma etiqueta
    consecutiva < 90 s = casi duplicados) del nombre de archivo; van al manifest
    y al cache. `resumen()` muestra ráfagas y lugares por clase.
  - `preprocess.py` — filtro IIR (sos) sobre la captura; DC/normalización por
    ventana (devuelve el factor de escala para recuperar amplitudes absolutas).
  - `segment.py` — ventanas W×solape; devuelve también `idx_captura`.
  - `mix.py` — combinadas sintéticas (2–3 cargas, SOLO para entrenar) con
    sesgo grande+chica y **fundamentales exactamente en fase** (jitter 0 por
    default; variante ±14.4° ≡ ±2 Hz sobre un ciclo). Sin fondo extra (ya está
    implícito en cada captura). `Mezclas.origenes` para auditar anti-leakage.
  - `augment.py` — sobre ventanas de train: ruido SNR, escala ±5 %, shift,
    hum, jitter de f_red; balanceo por clase con tope; hereda grupo/idx.
  - `features.py` — `rawfft` (BASE desde 2026-09-21: SOLO los bins directos de
    la FFT sin DC hasta `rawfft_fmax`=2 kHz → 409 feats con W=2048; sin
    interpolar, es lo que calcula el borde) y `compact` (42 feats: tiempo,
    armónicas con f0 estimado, estadísticos, bandas).
    `extraer_features(V, escala, cfg, fs)`, `nombres_features(cfg, fs)`.
  - `evaluate.py` — métricas multi-label por ventana y por captura
    (`agregar_por_grupo`), umbrales por clase (piso 0.3), importancias,
    `reporte`, `reporte_combinaciones` (qué se predice por combinación real).
  - `train.py` — `run_experiment(ds, cfg, leaderboard, etiqueta)`. Validación
    `cfg.cv`: **"lolo" (default) = leave-one-lugar-out**: entrena sin un lugar y
    evalúa sus individuales Y sus combinadas (casa nunca vista); "sgkf5"/"logo"
    agrupados por `cfg.cv_grupo` ("rafaga" default; "captura" reproduce los
    números viejos, optimistas). Bloques reales `ind` / `comb` / `todas`.
    Niveles `_v_` ventana, `_c_` captura (umbral 0.5), `_s_` clases vistas (sin
    contar aparatos que solo existen en la casa de test), `_u_` umbrales por
    clase (solo comb/mix; empeoran bajo validación honesta → no usar).
    `run_repetido(ds, cfg, n_semillas)` → media ± desvío (leaderboard guarda
    `_std` y `n_semillas`); `comparar(a, b)` pareado por semilla;
    `evaluar_test_congelado` sobre `cfg.test_congelado` (lugares/sesiones que
    run_experiment ignora). `preparar_dataset` aplica agrupar, min_capturas,
    min_rafagas y el congelado.
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
  - `eda.py` — Parte A del notebook (EDA, no entrena). `calcular_espectros(ds)`
    + funciones `plot_*` y tablas. Cada ráfaga pesa igual dentro de su clase.

## Flujo por fold (para train.py)
```
individuales(train) ─► mix.py (mix_total) ─┐
                                           ├► segmentar ─► preprocesar_ventanas ─► augment ─► features ─► fit
individuales(train) ────────────────────────┘
individuales(test)  ─┬► segmentar ─► preprocesar_ventanas ─► features ─► eval `ind`
combinadas reales   ─┴►                                      (todos los folds) ─► eval `comb`; `todas` = ind+comb
```
El filtro (`preprocesar_captura`) va sobre la captura completa antes de segmentar.

## Otros archivos
- `nilm_pipeline/plots.py` — gráficos de sanity check y resultados para el notebook.
- `notebooks/nilm_colab.ipynb` — notebook delgado, GENERADO por
  `tools/construir_notebook.py` (editar el constructor y regenerar:
  `.venv/bin/python3 tools/construir_notebook.py notebooks/nilm_colab.ipynb`).
  La celda 0 clona los repos en Colab o usa rutas relativas en local. Ejecutable localmente con el venv
  desde `notebooks/` (rutas relativas).
- `README.md` — uso en Colab desde GitHub y descripción del export.
- `outputs/` — artefactos locales (manifest.csv, leaderboard.csv, caches). No versionar.
- `requirements-colab.txt` — deps para Colab.

## Repos (GitHub, privados, desde 2026-09-20)
- Código: `github.com/klopszteinleonel/NILM_pipeline` (= este directorio).
- Datos: `github.com/klopszteinleonel/NILM_local_dataset` (= clon local en
  `../clienteTCP/capturas_limpio/`, una carpeta por clase + `combinadas/` + `nada/`).
- Colab clona ambos con un secret `GITHUB_TOKEN`; ya no se usa Drive (opcional
  solo para persistir `outputs/`).

## Datos
- Carpeta de capturas local: `../clienteTCP/capturas_limpio/` (clon del repo del dataset).
- Estado 2026-09-20: 209 capturas, 14 clases (nuevas: lavarropa 17, parasito 11,
  pava 7), 59 combinadas reales, 10 sin carga. hidro y secadorpelo siguen con 1.
- Formato JSON del nodo: `{categoria, lugar, canal, magnitud, unidad, fs=10000,
  duracion, muestras, formato, k, bytes, datos[]}` (1 s típica = 10000 muestras).
  `canal=0/magnitud="corriente"/unidad="A"` (CH0) o `canal=1/"tension"/"V"` (CH1).
  Capturas viejas no tienen esos 3 campos → se asumen corriente.
  Desde 2026-11-04 `capturar.py --equipo --estado` agrega esos campos a I y V (texto; en combinadas, separados
  por coma en el orden de `categoria`, `-` si no aplica). Las agrega el cliente, no el nodo; `data.py` todavía no los usa.
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
  cero, tipo `sin_carga`. NO es una clase.
- `Config.agrupar` (clase → grupo) fusiona clases en carga (`Dataset.agrupar_clases`).
  Default del notebook: calefon/horno/pava/plancha → "resistiva".
- `Config.min_capturas=3`: clases con menos capturas individuales se excluyen
  (sus capturas y las combinadas que las contienen) vía
  `Dataset.excluir_clases_raras`. Hoy excluye hidro y secadorpelo (1 c/u).
  Ambos pasos los aplica `train.preparar_dataset` en run_experiment/entrenar_final.
- Fuente de verdad de la clase = campo `categoria` del JSON (no la carpeta;
  ej: carpeta `hidromasaje/` pero clase `hidro`).
- Hay capturas de 1 s (10000) y de 2 s (20000 muestras).
- Cada captura es su propio grupo (anti-leakage en CV).

## Hechos de las señales (medidos 2026-09-19/21)
- Dataset 2026-09-20: calefon 4.7/6.4 A, horno 2.8/5.6 A, aire 2.5/6.0 A,
  pava 6.07, plancha 6.05 → **calefon/horno/pava/plancha son indistinguibles**
  (misma amplitud, THD 0.02); en combinadas reales el modelo las confunde
  sistemáticamente. 11 de 19 capturas de heladera están a 0.08–0.09 A (compresor
  apagado) = igual que `parasito` → heladera es invisible en mezclas.
  El usuario decidió NO reetiquetar ni recapturar con tensión por ahora.
- Combinadas reales: RMS cae cerca de la suma lineal de las individuales
  (aire+horno 8.3–8.6 A vs 8.27 lineal / 6.2 cuadrática) → cargas en fase.
  Mezclar con fase aleatoria generaba amplitudes irreales; `mix_fase="alineada"`
  subió real F1-micro 0.69 → 0.74.
- Umbrales por clase: derivarlos de individuales OOF con piso 0.3 mejora `real`
  (exact 0.46 → 0.54); derivarlos de `mix` empeora todo (falsos positivos).
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
- Mix sintético 2–3 cargas a nivel captura, generado DENTRO del fold solo
  desde train (`mix_total=100`), fase exactamente alineada, sin fondo extra.
  Decisión del usuario 2026-09-21: las mezclas sintéticas NO se usan como
  test (`mix_test=False`); el test son las capturas reales.
- Leaderboard con bloques reales ind / comb / todas (+ mix opcional), métricas
  por ventana, por captura (media de probas) y por captura con umbrales por
  clase, y costo del modelo (n_features, n_arboles, nodos, prof_max).
  Ordenar por `todas_c_f1_micro`.
- Modelo exportable = `OneVsRest(RandomForest)` (`multi_salida="ovr"`,
  default n_estimators=20, max_depth=8 desde el paso 6); RF multi-output
  nativo solo referencia.
- Validación: StratifiedGroupKFold(5) estratificado por la clase de la
  individual (y 1-D); LOGO definitivo. Combinadas reales fuera de los folds.
- Estrategia base `rawfft` W=2048 hasta 2 kHz, SOLO bins de la FFT (decisión
  del usuario 2026-09-21); `compact` queda como variante. Augment desactivado por default (`aug_n=0`):
  en 61 y en 209 capturas empeoró individuales y mezclas.
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

## Estado (2026-09-21, noche)
Pasos 1–6 hechos; repos en GitHub; dataset 209 capturas (207 tras excluir
hidro/secadorpelo). Base = `Config(agrupar=RES)`: rawfft SOLO bins hasta
**3 kHz** (614 feats), W=2048, mix 100 solo train con fase exactamente
alineada (sin jitter), sin augment, resistivas agrupadas, min_capturas=3,
RF OvR 20×8. Test = SOLO reales. Métricas macro sobre clases presentes.

Referencia local (SGKF5, por captura, F1-micro / exact, F1-macro):
- individuales reales 0.93 / 0.90, macro 0.92
- combinadas reales 0.87 / 0.51, macro 0.71 (0.90 / 0.63 / 0.75 con umbrales)
- todas 0.91 / 0.79, macro 0.88 (0.94 / 0.86 / 0.91 con umbrales)
- 10.5 k nodos ≈ 136 KB en C; paridad OK; LOGO ind F1-micro 0.92; ~6 s/exp.
- Combinadas: aire+resistiva 15/18, aire+lavarropa 9/10, aire+luces 7–8/10,
  aire+lavarropa+resistiva 6/6; cero en todo lo que tiene heladera (0.09 A)
  y en lavarropa+resistiva (0/3).

Barrido sobre esa base (todas / comb F1-micro, comb exact, nodos):
jitter_2hz 0.919/0.895/0.59/12k · fase_aleatoria 0.913/0.898/0.63/12k ·
fmax5000 0.909/0.861/0.49 · hp20 0.905/0.871/0.51 · base 0.905/0.871/0.51/10k ·
fmax4000 0.902/0.856/0.49 · rf40x8 0.901/0.866/0.51/20k · rf20x12 0.899/0.846/
0.46/16k · W4096 0.885/0.842/0.47/7k · fmax2000 0.876/0.830/0.46 · aug4 0.872 ·
mix400 0.871/0.898/0.63/21k (ind baja a 0.85) · compact 0.823 · sin_agrupar
0.802/0.613/0.07 · sin_mix 0.803/0.570/0.02 (ind 0.956).
Lecturas: fmax 3 kHz > 2 kHz claramente; 4–5 kHz no suma. Con 3 kHz las
variantes de fase (jitter ±14.4°, aleatoria) y mix400 suben combinadas
(0.87 → 0.90, exact 0.51 → 0.6) pero bajan individuales: hay más variedad
de mezclas en train; las diferencias son de 2–5 capturas de 59 (ruido).
Las mezclas sintéticas siguen siendo imprescindibles (sin_mix → exact 0.02).

Modelos (misma base, todas / comb F1-micro / comb exact / nodos):
rf50x12 0.911/0.861/0.49/38k · hgb 0.907/0.872/0.53/— (40 min, inviable) ·
rf20xinf 0.905/0.851/0.47/18k · base rf20x8 0.905/0.871/0.51/10k · et50x12
0.904/0.830/0.46/87k · rf100x10 0.904/0.861/0.49/67k · rf40x8 0.901/0.866 ·
knn 0.822/0.680/0.10 · rf_nativo 0.814/0.757/0.20 · svm 0.784 · logreg 0.762.
umbral_exact = base en todo salvo +umbr (0.936 vs 0.940): no aporta.
Lecturas: ningún modelo más grande supera a rf20x8 en combinadas; los RF
grandes suben individuales (0.95) pero no mezclas; kNN/SVM/logreg quedan lejos;
el RF multi-output nativo es claramente peor que OvR. El cuello de botella no
es la capacidad del modelo sino la información (heladera 0.09 A, luces bajo aire).

Filtros y augment (misma base; todas / comb F1-micro / comb exact):
hp20 0.905/0.871/0.51 (= base) · notch50 (Q30, Q10, filtfilt) 0.855–0.859/
0.755–0.766/0.22 → quitar la fundamental HUNDE combinadas: la amplitud de la
fundamental es la información principal (resistivas, aire 2.5 vs 6 A); las
armónicas solas no alcanzan. Augment (aug_n=2, ×2.5–3 nodos): escala10
0.890/0.866/0.51 · hum 0.890/0.846/0.46 · blanco/blanco10/rosa/impulsivo
0.869–0.875/0.82–0.83/0.39–0.44 · jitter_red 0.865/0.805 · todo 0.854/0.781/0.36.
Ningún augment supera a la base; ruido de cualquier color perjudica; el
augment solo tiene sentido para robustez a ruido futuro, no para F1.

## Revisión 2026-09-22: hechos medidos (ver informe en la conversación)
- Dataset = 3 sesiones (S00 12-sep casa/local; S01 y S02 20-sep depaleo). Las 148
  individuales son 28 ráfagas (misma clase, < 90 s): el tamaño efectivo es ~28, no
  148. aireinverter, airecalor, tele, parasito y nada tienen UNA sola ráfaga.
- CV agrupada por captura (actual) = ind exact 0.905; agrupada por ráfaga = 0.536;
  leave-one-burst-out solo en clases con ≥2 ráfagas = exact 0.675 / F1 0.81
  (luces 0.12: lámparas distintas en cada lugar). El CV actual es muy optimista.
- Combinadas: todas grabadas en las mismas sesiones que sus individuales. Entrenando
  solo con otra sesión (S00) → combinadas sin lavarropa exact 0.35 / F1 0.66
  (vs 0.45 / 0.83 con la misma sesión en train).
- Ruido entre semillas (misma config): comb exact 0.534 ± 0.064 (0.44–0.63),
  comb F1 ±0.023. Casi todas las diferencias de los barridos están en el ruido.
- Piso de ruido del front-end ≈ 83 mA RMS de banda ancha (~10 LSB, ~33× el ruido de
  cuantización); nada/parasito/heladera-S02 tienen todas ese RMS. Fundamental:
  nada 5 mA, parasito 24 mA, heladera S02 14 mA (reposo; las 12 combinadas de S02
  con heladera tienen la etiqueta inobservable: re-etiquetarlas sube comb exact
  0.51 → 0.69).
- Fondo de escala ±17.5 A pico (≈12.4 A RMS senoidal). Las 3 aire+calefon+horno
  están saturadas (14–16 % de muestras en el riel) y el modelo las lee como
  lavarropa. data.py no detecta saturación.
- Red real en los datos: 49.91–50.08 Hz. Con f0 ±0.2 Hz la base cae de exact 0.45
  a 0.37–0.39 (comb, modelo con todas las individuales); rawfft con eje normalizado
  a f0 estimada se mantiene en 0.46–0.50 sin costo nominal.
- Muestreo coherente (12.8 kHz, W=2048 = 8 ciclos exactos) NO sirve: igual en
  nominal y mucho peor ante deriva de f0 (rect: exact 0.23 → 0.04).
- Paridad de features: FFT float32 no cambia decisiones; Hann simétrica (N−1) vs
  periódica cambia 0.01 % (riesgo menor; usar la LUT exportada).
- Bugs: Config.from_dict falla con filas viejas del leaderboard (campos quitados);
  el constructor del notebook vive fuera del repo; robustez_snr es in-sample;
  todas_u usa umbrales ajustados sobre ind (parte de todas) → optimista.

## Definiciones del usuario (2026-09-22) — condicionan todo el diseño
- OBJETIVO: el medidor debe funcionar en CASAS QUE NUNCA VIO (modelo genérico).
  La métrica que manda es la validación entre lugares (leave-one-lugar-out), no
  el CV dentro de una sesión.
- aire y airecalor = el MISMO equipo en dos modos (mutuamente excluyentes);
  aireinverter = otro equipo.
- parasito = cargas enchufadas "apagadas" con consumo chico sostenido (ej. aire
  en standby). Fundamental medida 24 mA (~5 W), cerca del piso de ruido.
- Heladera de S02: el usuario cree que no estaba en reposo (14 mA de fundamental
  ≈ 3 W; la heladera de "local" con compresor andando mide 0.28–0.51 A).
- Hardware: TC 15 A/1 V → satura a ~12.4 A RMS; objetivo ~20 A RMS. Con el mismo
  front-end el fondo de escala escala como 12.4·(rango/15): 20 A/1 V → 16.5 A RMS,
  30 A/1 V → 24.7 A RMS (cuantización 4.9 mA RMS, muy debajo del piso de ruido
  de 83 mA → cambiar de sensor casi no cuesta resolución efectiva).
- Anti-alias: corriente = Butterworth 4° orden fc 2.2 kHz (2× MCP602); tensión =
  LM358 + RC 1er orden fc 480 Hz. Desfase I−V calculado (filtros + ~20 µs entre
  lecturas CH0/CH1): 2.9° a 50 Hz, 8° a 150 Hz, 12° a 250 Hz, 16° a 500 Hz (sin
  contar el desfase propio del TC) → P/Q necesitan calibración de fase (carga
  resistiva pura = referencia de FP 1). Bins > 2.2 kHz están atenuados
  (−11 dB a 3 kHz). Para alinear mezclas por fase de tensión NO hace falta
  calibrar: el desfase es común a todas las capturas del mismo hardware.

## Validación honesta (implementada 2026-09-22) — referencia actual
Base `Config(agrupar=RES)`, 5 semillas:
- LOLO (casa nunca vista), clases vistas: ind F1 0.715±0.009, comb F1
  0.691±0.041, comb exact 0.363±0.023, todas F1 0.703±0.017.
- sgkf5 por ráfaga: ind (clases vistas) exact 0.675 / F1 0.78; comb exact 0.49
  / F1 0.85. sgkf5 por captura reproduce los números viejos (0.90 / 0.51).
- Umbrales por clase bajo validación honesta EMPEORAN comb (por ráfaga exact
  0.49 → 0.41; LOLO 0.25 → 0.10): la mejora vieja era efecto de los casi
  duplicados. Decisión: umbral fijo 0.5; export sin umbrales por clase.
- Primera comparación pareada: fmax3000 > fmax2000 en 5/5 semillas (todas_s
  +0.046±0.021). Los barridos anteriores deben re-evaluarse con este esquema.
- Barrido LOLO 2026-09-22 (3 semillas; todas_s / comb_s F1 / comb_s exact):
  fase_aleatoria 0.736/0.766/0.45 · aug_impulsivo 0.731/0.754/0.51 ·
  aug_escala10 0.729/0.759/0.49 · aug_rosa 0.727/0.760/0.49 · fmax4000 0.717 ·
  aug_hum 0.716 · aug_blanco 0.711/0.752/0.50 · base 0.703/0.691/0.36 · mix200
  0.702 · rf50x12 0.701 · hp20 0.699 · jitter_2hz 0.683 · aug_jitter_red 0.681 ·
  sin_mix 0.673/0.573/0.11 · W4096 0.673 · fmax2000 0.660 · notch50 0.654/0.527/
  0.10 · compact 0.601 · sin_agrupar 0.500.
  CAMBIO DE CONCLUSIÓN: con casa nunca vista, el augment (ruido de cualquier
  tipo, escala ±10 %) y la fase aleatoria MEJORAN las combinadas (+0.06–0.08 F1,
  exact 0.36 → ~0.5, con desvíos chicos). Bajo el CV por captura parecían
  perjudicar porque ese CV premiaba memorizar casi duplicados. Ojo: comb en LOLO
  es un solo fold (depaleo, entrenado solo con casa+local).

## Dataset con depamica (2026-09-22, 317 capturas, 4 lugares, 124 combinadas)
Base del notebook: agrupar resistivas + airecalor→aire, excluir_clases=("parasito",).
LOLO 5 semillas: ind_s F1 0.737±0.028; comb F1 0.654±0.035, exact 0.263±0.045;
todas_s 0.686±0.030. sgkf5 por ráfaga: ind 0.780, comb F1 0.783, exact 0.41.
Barrido LOLO (3 semillas, todas_s): aug_hum 0.702, jitter_2hz 0.700, aug_blanco
0.694, mix200 0.689, faseal+escala10 0.686, base 0.686, rf50x12 0.686, ... fmax2000
0.668, hp20 0.667, W4096 0.650, compact 0.647, sin_mix 0.645 (comb exact 0.03),
notch50 0.640, sin_agrupar 0.507. Con 2 casas de combinadas, las ventajas de
augment / fase aleatoria del barrido anterior (1 sola casa) DESAPARECEN: todo
está dentro de ±1 desvío de la base. Robusto en ambos: agrupar resistivas,
usar mezclas, no usar notch, rawfft > compact, W2048 > W4096.
Fallas en casa nueva: luces F1 0.36/0.37, heladera en combinadas 0.14,
aireinverter (2 ráfagas, casa+depamica) arrastra falsos positivos de heladera,
lavarropa y tele; tele (1 sola ráfaga, casa) aparece como falso positivo.
Cuello de botella: pocas unidades distintas por clase, no el modelo.

## Auditoría de la Parte B contra el EDA (2026-11-04, dataset dfe60d02ca, 297 capturas)
**Base actual (decisión del usuario, 2026-11-04): `Config(agrupar=..., excluir_clases=("parasito",))` = mix_sesgo_grande_chica=0,
mix_total=300, rawfft_f0=True, min_rafagas=2.** Regla del usuario: una clase con pocas muestras se descarta hasta tener más
(hoy hidro, secadorpelo y tele). Antes de sacar tele, LOLO 10 semillas: todas_s 0.732, ind 0.729, comb 0.734, comb exact 0.371, 15.2 k nodos.
`nilm_features.h` implementa espectro -> features en C con la corrección de f0; `test_paridad_features`: 100 % de decisiones iguales
partiendo del mismo espectro. Falta integrarlo al firmware (hoy el nodo solo captura) y verificar en la placa.
`estimar_f0` lee +0.074 Hz con W=2048 (constante: no afecta la corrección); la red real en los datos fue 49.91–50.11 Hz.

`tools/experimentos_eda.py` reproduce el barrido; `comparar_bootstrap` da el IC del 95 % remuestreando ráfagas de test
(`mapa_a` proyecta la base cuando cambian las clases). Las opciones nuevas de `Config` están apagadas: la base no cambió.
Base LOLO (10 semillas): todas_s 0.708, ind 0.743, comb 0.683, comb exact 0.277, 9.2 k nodos.
- `mix_por_rafaga` + `min_samples_leaf=5`: todas +0.028 (0.012, 0.045), 7.4 k nodos, exportable.
- `mix_sesgo_grande_chica=0` + `mix_total=300`: comb +0.063 (0.029, 0.097), comb exact +0.108, ind exact -0.054, 15.5 k nodos.
  Causa: las mezclas tenían 91 % con carga grande (66 % en las reales) y lavarropa+luces era 1 %.
- Las dos juntas rinden menos que cada una por separado: las mejoras no se suman.
- `rawfft_f0` (eje corregido con f0): con f0 ±0.5 Hz la base cae a F1 0.58/0.46; con sesgo0+mix300+f0 queda en 0.73/0.73.
  `exportar` todavía no lo implementa (tampoco `rawfft_norm`, `franjas`, `filtro_corriente`).
- No funcionaron: franjas angostas por clase (exact -0.14), solo impares, forma+H1, escala amplia en mezclas, filtro por corriente.
- tele+luces en clases proyectadas: +0.022 (no +0.044). La mezcla alineada ya reproduce la suma real de armónicas.
- ~50 variantes sobre los mismos 4 lugares: hay riesgo de selección; hace falta una casa nueva congelada para confirmar.

## Captura de combinadas reales y sin carga
- `clienteTCP/capturar.py` acepta comas desde 2026-09-20:
  `--categoria "aire,tele"` (archivo `<ts>_aire+tele.json`, la coma se guarda
  en el JSON) y `--categoria nada` para ruido de fondo.
- Mover las combinadas a `capturas_limpio/combinadas/` y las "nada" a
  cualquier carpeta (ej. `capturas_limpio/nada/`).
