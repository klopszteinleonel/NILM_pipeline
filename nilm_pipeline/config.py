"""
Configuración central del pipeline NILM.

`Config` es la UNICA fuente de verdad de todos los parametros experimentables.
Cada experimento = una instancia de Config. Las métricas se loguean junto con
la config serializada en leaderboard.csv.

Principio rector: todo lo que hace el pipeline tiene que poder correr en el
ESP32-S3 (FFT radix-2 float32 con esp-dsp, filtros IIR causales, RF binario
por clase). Por eso W y n_fft se validan como potencias de 2.

Uso:
    cfg = Config()                       # baseline
    cfg2 = cfg.clonar(W=4096, ventana="blackman")
    cfg3 = Config.from_dict(diccionario) # desde JSON (leaderboard)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace

# Campos que se serializan como lista en JSON y hay que reconvertir a tupla
_CAMBIOS_TUPLA = ("filtro_fc", "mix_k", "mix_escala", "snr_db", "escala", "bandas_hz", "test_congelado", "excluir_clases",
                  "train_escala")

ESTRATEGIAS = ("compact", "rawfft")
VENTANAS = ("hann", "hamming", "blackman", "flattop", "rect")
MODELOS = ("rf", "et", "hgb", "svm", "knn", "logreg")
FILTROS = (None, "hp", "lp", "bp", "notch")
NORMALIZACIONES = (None, "rms", "maxabs")
CVS = ("lolo", "sgkf5", "logo")
CV_GRUPOS = ("rafaga", "sesion", "captura")
MULTI_SALIDAS = ("ovr", "nativo", "cadena")


def _es_potencia_de_2(n: int) -> bool:
    return isinstance(n, int) and n > 0 and (n & (n - 1)) == 0


@dataclass
class Config:
    # ---------------- datos ----------------
    data_dir: str = ""              # carpeta raíz de capturas (capturas_limpio)
    agrupar: dict = field(default_factory=dict)  # clase -> grupo, ej. {"calefon": "resistiva",
                                    # "horno": "resistiva"}: clases físicamente indistinguibles
                                    # (misma amplitud, THD ~0) se evalúan y exportan como una
    excluir_clases: tuple = ()      # clases que se sacan del problema: se descartan sus
                                    # capturas y toda combinada que las contenga
    min_rafagas: int = 2            # ídem por ráfagas (ocasiones independientes). Decisión del usuario: una
                                    # clase con pocas muestras se descarta hasta tener más
    min_capturas: int = 3           # clases con menos capturas individuales se EXCLUYEN
                                    # (sus capturas y las combinadas que las contienen):
                                    # con 1–2 capturas no se pueden aprender ni evaluar
    min_capturas_aviso: int = 3     # avisa si una clase tiene menos capturas
    f_red: float = 50.0             # frecuencia de red [Hz]
    clase_nada: str = "nada"        # categoria reservada para "sin carga" → vector cero

    # ---------------- preproceso ----------------
    quitar_dc: bool = True          # se aplica por ventana
    filtro: str | None = None       # None | "hp" | "lp" | "bp" | "notch" (sobre la captura)
    filtro_fc: tuple = (20.0,)      # Hz: 1 valor (hp/lp/notch) o 2 (bp). Para hp
                                    # mantener fc << f_red: con fc=40 y orden 4 la
                                    # fundamental de 50 Hz pierde 7.5 % (15 % filtfilt)
    filtro_orden: int = 4
    notch_q: float = 30.0           # factor de calidad del notch: ancho = fc/Q (50/30 = 1.7 Hz)
    causal: bool = False            # True = sosfilt (implementable en el borde);
                                    # False = sosfiltfilt (offline, fase cero)
    normalizar: str | None = None   # None | "rms" | "maxabs" (por ventana; las
                                    # features absolutas se calculan antes)

    # ---------------- segmentación ----------------
    W: int = 2048                   # tamaño de ventana [muestras], potencia de 2
    solape: float = 0.5             # fracción de solape [0..1)

    # ---------------- mezcla sintética ----------------
    mix_habilitado: bool = True
    mix_k: tuple = (2, 3)           # (min, max) cargas simultáneas
    mix_total: int = 300            # combinadas sintéticas para train (por fold). Con 7 clases hay 56
                                    # combinaciones de 2-3 cargas: con 100 no llegan a 2 por combinación
    mix_test: bool = False          # mezclas sintéticas también como bloque de test
                                    # (apagado: las combinadas reales son el test)
    mix_test_total: int = 100       # combinadas sintéticas para test (si mix_test)
    mix_escala: tuple = (0.95, 1.05)  # escalado de amplitud por carga (±5 % ~ red)
    mix_misma_clase: bool = False   # permitir 2 cargas de la misma clase
    mix_sesgo_grande_chica: float = 0.0  # fracción de mixes que fuerzan grande+chica. Con 0.5 el 91 % de las
                                    # mezclas tenía una carga grande (66 % en las combinadas reales)
    mix_fase: str = "alineada"      # "alineada": fundamentales exactamente en fase, como
                                    # cargas reales colgadas de la misma red; "aleatoria"
    mix_jitter_fase: float = 0.0    # grados de desfase aleatorio (±) entre cargas; 0 en la
                                    # base. Referencia: ±2 Hz sobre un ciclo de 50 Hz ≡ ±14.4°
    mix_alinear_f0: bool = False    # estimar la fase con la f0 de cada captura y no con f_red fija
                                    # (con f_red el error de fase mediano es 12.5°, hasta 40°)
    mix_armonicas_azar: bool = False  # fase propia al azar en cada armónica de cada carga:
                                    # H1 sigue alineada; desde H2 suman sin relación, como en las reales
    mix_mismo_lugar: bool = False   # si las cargas elegidas existen juntas en algún lugar, mezclarlas de ese lugar: en una
                                    # casa real comparten la red. Si no, se mezclan de lugares distintos para no perder combinaciones
    mix_por_rafaga: bool = False    # elegir primero la ráfaga y después la captura: una ocasión con
                                    # muchas capturas no domina las mezclas
    max_por_rafaga: int = 0         # capturas por ráfaga en train (0 = todas): baja los casi duplicados
    train_escala: tuple | None = None  # factor de amplitud por captura de train (log-uniforme):
                                    # cada equipo tiene su potencia. Ej. (0.6, 1.6)
    train_red_pct: float = 0.0      # distorsión de red simulada en train: H3..H13 hasta este % de H1,
                                    # para no aprender la red de cada casa

    # ---------------- augmentación (solo train) ----------------
    aug_n: int = 0                  # copias aumentadas por ventana (0 = sin augment:
                                    # en dos datasets distintos el augment empeoró
                                    # individuales y mezclas; las mezclas ya aportan variación)
    aug_balance: bool = True        # nivelar clases (más copias a minorías)
    aug_max_factor: int = 10        # tope de copias = aug_n * aug_max_factor
    snr_db: tuple = (20.0, 50.0)    # rango de SNR del ruido agregado
    ruido_tipo: str = "blanco"      # "blanco" (gaussiano) | "rosa" (1/f) | "impulsivo" (picos)
    escala: tuple = (0.95, 1.05)    # rango de escalado de amplitud (±5 % ~ red)
    shift: bool = True              # desplazamiento temporal circular
    hum50: bool = False             # inyectar zumbido de f_red
    hum_rel: float = 0.02           # amplitud del zumbido relativa al RMS
    jitter_red: bool = False        # variar f_red ±jitter_hz (resample)
    jitter_hz: float = 0.5

    # ---------------- features ----------------
    estrategia: str = "rawfft"      # "rawfft" (base) | "compact"
    ventana: str = "hann"           # hann|hamming|blackman|flattop|rect
    n_fft: int | None = None        # None → = W (si > W, zero-padding); potencia de 2
    n_armonicas: int = 20           # armónicas de f_red en estrategia compact
    f0_estimar: bool = True         # estimar f0 real (interp. parabólica) antes de H_k
    armonica_tol_bins: int = 1      # H_k = máximo en ±tol bins alrededor de k·f0
    normalizar_forma: bool = True   # vector armónico como H_k/H1 (las absolutas quedan)
    bandas_hz: tuple = (0.0, 100.0, 300.0, 500.0, 1000.0, 2000.0, 3500.0, 5000.0)
                                    # bordes de bandas para energía fraccional
    rawfft_mascara: str = "todos"   # "todos" | "armonicas" | "impares": qué bins de rawfft se usan
    rawfft_armonicas_tol: int | None = 2  # N = solo los bins a ±N de cada armónica k·f_red, pares e impares, hasta rawfft_fmax:
                                    # son los únicos que recibe el modelo (299 con W=2048). None = usa rawfft_mascara
    rawfft_extras: bool = False     # agrega al vector escalares de tiempo y espectro (RMS, pico, cresta, THD, centroide, bandas...)
    rawfft_f0: bool = True          # corrige el eje de frecuencia con la f0 estimada de cada ventana:
                                    # H_k cae en el mismo bin aunque la red se corra (sin esto, F1 0.46 a -0.5 Hz)
    rawfft_norm: str | None = None  # None | "h1": bins relativos a H1 + H1 como feature (forma + nivel)
    franjas: dict = field(default_factory=dict)  # clase -> (fmin, fmax, "todo"|"armonicas"|"pares"|
                                    # "impares"): bins que ve el bosque de esa clase (H1 siempre)
    rawfft_fmax: float = 3000.0     # rawfft = SOLO los bins directos de la FFT (sin DC) hasta
                                    # esta frecuencia. Con W=2048 y fs=10 kHz: 614 bins de
                                    # 4.88 Hz. Es exactamente lo que calcula el borde.
                                    # (3 kHz > 2 kHz en combinadas reales, mismo costo)

    # ---------------- modelo ----------------
    modelo: str = "rf"              # rf|et|hgb|svm|knn|logreg
    multi_salida: str = "ovr"       # "ovr" = un clasificador binario por clase (exportable)
                                    # "nativo" = multi-output de sklearn (referencia)
                                    # "cadena" = cada bosque recibe también la salida de los anteriores
    cadena_orden: str = "grandes"   # "grandes" = primero las clases de mayor corriente | "chicas" = al revés
    n_estimators: int = 20          # 20x8 ≈ 276 KB de nodos en C y mejor F1 que 50x10
    max_depth: int | None = 8       # (3.2 MB) sobre 61 capturas; ver AGENTS.md
    hiperparams: dict = field(default_factory=dict)  # extras para el estimador
    class_weight: str | None = "balanced"
    umbral: float = 0.5             # umbral de decisión multi-label (filas "por captura")
    filtro_corriente: float = 0.0   # >0: una clase no puede estar activa si H1 medida < este factor ×
                                    # P5 de H1 de esa clase en train (la corriente no alcanza)
    umbral_criterio: str = "f1"     # cómo se eligen los umbrales por clase (+umbr):
                                    # "f1" = cada clase maximiza su F1 (independiente)
                                    # "exact" = búsqueda conjunta que maximiza el exact match

    # ---------------- evaluación ----------------
    cv: str = "lolo"                # "lolo" = leave-one-lugar-out: entrena sin un lugar y
                                    #          evalúa ahí (individuales Y combinadas) → mide
                                    #          lo que importa: funcionar en una casa nueva
                                    # "sgkf5" = StratifiedGroupKFold dentro del dataset
                                    # "logo"  = leave-one-group-out (grupo = cv_grupo)
    cv_grupo: str = "rafaga"        # unidad que nunca se reparte entre train y test en
                                    # sgkf5/logo: "rafaga" (recomendado), "sesion", "captura"
    n_splits: int = 5
    test_congelado: tuple = ()      # lugares o sesiones reservados: run_experiment los
                                    # ignora; solo los usa evaluar_test_congelado()
    seed: int = 42
    test_escala: float = 1.0        # solo robustez: escala de amplitud aplicada a las capturas de test
    test_f0_hz: float = 0.0         # solo robustez: corrimiento de la frecuencia de red en test

    def __post_init__(self):
        e = []
        if not _es_potencia_de_2(self.W) or self.W < 256:
            e.append(f"W={self.W} debe ser potencia de 2 y >= 256 (FFT radix-2 del ESP32-S3)")
        if self.n_fft is not None and (not _es_potencia_de_2(self.n_fft) or self.n_fft < self.W):
            e.append(f"n_fft={self.n_fft} debe ser None o potencia de 2 >= W")
        if not (0.0 <= self.solape < 1.0):
            e.append(f"solape={self.solape} debe estar en [0, 1)")
        if not (len(self.mix_k) == 2 and 2 <= self.mix_k[0] <= self.mix_k[1]):
            e.append(f"mix_k={self.mix_k} debe ser (min, max) con 2 <= min <= max")
        if not (0.0 <= self.mix_sesgo_grande_chica <= 1.0):
            e.append("mix_sesgo_grande_chica debe estar en [0, 1]")
        if self.min_capturas < 1:
            e.append("min_capturas debe ser >= 1")
        if self.mix_fase not in ("alineada", "aleatoria"):
            e.append("mix_fase debe ser 'alineada' o 'aleatoria'")
        if self.rawfft_fmax <= 0:
            e.append("rawfft_fmax debe ser > 0")
        if self.estrategia not in ESTRATEGIAS:
            e.append(f"estrategia={self.estrategia!r} no está en {ESTRATEGIAS}")
        if self.ventana not in VENTANAS:
            e.append(f"ventana={self.ventana!r} no está en {VENTANAS}")
        if self.modelo not in MODELOS:
            e.append(f"modelo={self.modelo!r} no está en {MODELOS}")
        if self.multi_salida not in MULTI_SALIDAS:
            e.append(f"multi_salida={self.multi_salida!r} no está en {MULTI_SALIDAS}")
        if self.normalizar not in NORMALIZACIONES:
            e.append(f"normalizar={self.normalizar!r} no está en {NORMALIZACIONES}")
        if self.cadena_orden not in ("grandes", "chicas"):
            e.append("cadena_orden debe ser 'grandes' o 'chicas'")
        if self.umbral_criterio not in ("f1", "exact"):
            e.append("umbral_criterio debe ser 'f1' o 'exact'")
        if self.cv_grupo not in CV_GRUPOS:
            e.append(f"cv_grupo={self.cv_grupo!r} no está en {CV_GRUPOS}")
        if self.min_rafagas < 1:
            e.append("min_rafagas debe ser >= 1")
        if self.cv not in CVS:
            e.append(f"cv={self.cv!r} no está en {CVS}")
        if self.filtro not in FILTROS:
            e.append(f"filtro={self.filtro!r} no está en {FILTROS}")
        elif self.filtro is not None:
            esperados = 2 if self.filtro == "bp" else 1
            if len(self.filtro_fc) != esperados:
                e.append(f"filtro={self.filtro} requiere {esperados} frecuencia(s) en filtro_fc")
        if self.armonica_tol_bins < 0:
            e.append("armonica_tol_bins debe ser >= 0")
        if self.n_armonicas < 1:
            e.append("n_armonicas debe ser >= 1")
        if len(self.bandas_hz) < 2 or any(b <= a for a, b in zip(self.bandas_hz, self.bandas_hz[1:])):
            e.append("bandas_hz debe ser una tupla creciente de >= 2 bordes")
        if self.aug_n < 0 or self.aug_max_factor < 1:
            e.append("aug_n >= 0 y aug_max_factor >= 1")
        if not (self.snr_db[0] <= self.snr_db[1]):
            e.append("snr_db debe ser (min, max)")
        if self.ruido_tipo not in ("blanco", "rosa", "impulsivo"):
            e.append("ruido_tipo debe ser blanco, rosa o impulsivo")
        if self.notch_q <= 0:
            e.append("notch_q debe ser > 0")
        if self.rawfft_mascara not in ("todos", "armonicas", "impares"):
            e.append("rawfft_mascara debe ser todos, armonicas o impares")
        if self.rawfft_armonicas_tol is not None and (self.rawfft_armonicas_tol < 0 or self.rawfft_mascara != "todos"):
            e.append("rawfft_armonicas_tol debe ser >= 0 y no se combina con rawfft_mascara")
        if self.rawfft_extras and (self.rawfft_norm or self.franjas or self.estrategia != "rawfft"):
            e.append("rawfft_extras requiere estrategia rawfft, sin rawfft_norm ni franjas")
        if self.rawfft_norm not in (None, "h1"):
            e.append("rawfft_norm debe ser None o 'h1'")
        if self.franjas and (self.rawfft_norm or self.estrategia != "rawfft" or self.multi_salida != "ovr"):
            e.append("franjas requiere estrategia rawfft, multi_salida ovr y rawfft_norm None")
        if self.train_escala is not None and not (0 < self.train_escala[0] <= self.train_escala[1]):
            e.append("train_escala debe ser (min, max) con 0 < min <= max")
        if e:
            raise ValueError("Config inválida:\n  - " + "\n  - ".join(e))

    # ---------------- utilidades ----------------
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), default=list, **kwargs)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        """Tolera configs de versiones anteriores: los campos que ya no existen se
        ignoran con un aviso y los que faltan toman su default."""
        import dataclasses, warnings
        validos = {f.name for f in dataclasses.fields(cls)}
        viejos = sorted(set(d) - validos)
        if viejos:
            warnings.warn(f"Config: se ignoran campos de una versión anterior: {viejos}")
        d = {k: v for k, v in d.items() if k in validos}
        for k in _CAMBIOS_TUPLA:
            if k in d and isinstance(d[k], list):
                d[k] = tuple(d[k])
        return cls(**d)

    @classmethod
    def from_json(cls, s: str) -> "Config":
        return cls.from_dict(json.loads(s))

    def clonar(self, **cambios) -> "Config":
        """Devuelve una copia con los campos modificados (revalida)."""
        return replace(self, **cambios)

    @property
    def n_fft_efectivo(self) -> int:
        return self.n_fft or self.W

    @property
    def paso(self) -> int:
        """Paso entre ventanas consecutivas [muestras]."""
        return max(1, int(round(self.W * (1.0 - self.solape))))

    def nombre(self) -> str:
        """Nombre corto del experimento (para leaderboard y logs)."""
        mix = f"_mix{self.mix_total}" if self.mix_habilitado else ""
        aug = (f"_aug{self.aug_n}{self.ruido_tipo[0]}{self.snr_db[0]:g}-{self.snr_db[1]:g}"
               + ("h" if self.hum50 else "") + ("j" if self.jitter_red else "")) if self.aug_n > 0 else ""
        fil = f"_f{self.filtro}{self.filtro_fc[0]:g}" if self.filtro else ""
        agr = (f"_agr{len(set(self.agrupar.values()))}" if self.agrupar else "") + (
            f"_sin{len(self.excluir_clases)}" if self.excluir_clases else "")
        est = f"{self.estrategia}{self.rawfft_fmax:.0f}" if self.estrategia == "rawfft" else self.estrategia
        arb = f"{self.n_estimators}x{self.max_depth if self.max_depth is not None else 'inf'}"
        fase = "_fasealeat" if self.mix_fase == "aleatoria" else (
            f"_jit{self.mix_jitter_fase:g}" if self.mix_jitter_fase > 0 else "")
        ms = {"nativo": "_nativo", "cadena": f"_cadena{self.cadena_orden}"}.get(self.multi_salida, "")
        uc = "_uexact" if self.umbral_criterio == "exact" else ""
        cv = self.cv if self.cv == "lolo" else f"{self.cv}-{self.cv_grupo}"
        ext = "".join(s for s, v in (("_mascara" + self.rawfft_mascara, self.rawfft_mascara != "todos"), ("_f0", self.rawfft_f0),
                                     (f"_arm{self.rawfft_armonicas_tol}", self.rawfft_armonicas_tol is not None), ("_extras", self.rawfft_extras),
                                     (f"_nfft{self.n_fft}", self.n_fft),
                                     ("_normh1", self.rawfft_norm), ("_franjas", self.franjas), ("_mixf0", self.mix_alinear_f0), ("_mixlugar", self.mix_mismo_lugar),
                                     ("_mixarm", self.mix_armonicas_azar), ("_mixraf", self.mix_por_rafaga),
                                     (f"_max{self.max_por_rafaga}", self.max_por_rafaga), ("_trescala", self.train_escala),
                                     (f"_red{self.train_red_pct:g}", self.train_red_pct), (f"_fc{self.filtro_corriente:g}", self.filtro_corriente)) if v)
        return f"{est}_W{self.W}_{self.ventana}_{self.modelo}{arb}{ms}{mix}{fase}{aug}{fil}{agr}{uc}{ext}_{cv}"


# Preset de referencia (baseline del plan)
BASELINE = Config()
