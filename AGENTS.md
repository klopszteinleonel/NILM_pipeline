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
  - `train.py` — `run_experiment(ds, cfg, leaderboard, etiqueta)`: CV por
    grupos con mix dentro del fold. El test son SOLO capturas reales, en tres
    bloques: `ind` (individuales del fold), `comb` (combinadas reales, fuera
    de los folds, promedio de los modelos) y `todas` (ind+comb = métrica del
    producto). `mix` solo si `mix_test=True`. Aplica `cfg.agrupar`; `_u_` =
    umbrales por clase aprendidos en ind OOF (`Resultado.umbrales`);
    `entrenar_final`; `costo_modelo`; `robustez_snr`; leaderboard CSV.
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
individuales(test)  ─┬► segmentar ─► preprocesar_ventanas ─► features ─► eval `ind`
combinadas reales   ─┴►                                      (todos los folds) ─► eval `comb`; `todas` = ind+comb
```
El filtro (`preprocesar_captura`) va sobre la captura completa antes de segmentar.

## Otros archivos
- `nilm_pipeline/plots.py` — gráficos de sanity check y resultados para el notebook.
- `notebooks/nilm_colab.ipynb` — notebook delgado (se genera/edita a mano;
  la celda 0 clona los repos en Colab o usa rutas relativas en local). Ejecutable localmente con el venv
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

## Captura de combinadas reales y sin carga
- `clienteTCP/capturar.py` acepta comas desde 2026-09-20:
  `--categoria "aire,tele"` (archivo `<ts>_aire+tele.json`, la coma se guarda
  en el JSON) y `--categoria nada` para ruido de fondo.
- Mover las combinadas a `capturas_limpio/combinadas/` y las "nada" a
  cualquier carpeta (ej. `capturas_limpio/nada/`).
