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
_CAMBIOS_TUPLA = ("filtro_fc", "mix_k", "mix_escala", "snr_db", "escala", "bandas_hz")

ESTRATEGIAS = ("compact", "rawfft")
VENTANAS = ("hann", "hamming", "blackman", "flattop", "rect")
MODELOS = ("rf", "et", "hgb", "svm", "knn", "logreg")
FILTROS = (None, "hp", "lp", "bp", "notch")
NORMALIZACIONES = (None, "rms", "maxabs")
CVS = ("sgkf5", "logo")
MULTI_SALIDAS = ("ovr", "nativo")


def _es_potencia_de_2(n: int) -> bool:
    return isinstance(n, int) and n > 0 and (n & (n - 1)) == 0


@dataclass
class Config:
    # ---------------- datos ----------------
    data_dir: str = ""              # carpeta raíz de capturas (capturas_limpio)
    agrupar: dict = field(default_factory=dict)  # clase -> grupo, ej. {"calefon": "resistiva",
                                    # "horno": "resistiva"}: clases físicamente indistinguibles
                                    # (misma amplitud, THD ~0) se evalúan y exportan como una
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
    mix_total: int = 100            # combinadas sintéticas para train (por fold)
    mix_test: bool = False          # mezclas sintéticas también como bloque de test
                                    # (apagado: las combinadas reales son el test)
    mix_test_total: int = 100       # combinadas sintéticas para test (si mix_test)
    mix_escala: tuple = (0.95, 1.05)  # escalado de amplitud por carga (±5 % ~ red)
    mix_misma_clase: bool = False   # permitir 2 cargas de la misma clase
    mix_sesgo_grande_chica: float = 0.5  # fracción de mixes que fuerzan grande+chica
    mix_fase: str = "alineada"      # "alineada": fundamentales exactamente en fase, como
                                    # cargas reales colgadas de la misma red; "aleatoria"
    mix_jitter_fase: float = 0.0    # grados de desfase aleatorio (±) entre cargas; 0 en la
                                    # base. Referencia: ±2 Hz sobre un ciclo de 50 Hz ≡ ±14.4°

    # ---------------- augmentación (solo train) ----------------
    aug_n: int = 0                  # copias aumentadas por ventana (0 = sin augment:
                                    # en dos datasets distintos el augment empeoró
                                    # individuales y mezclas; las mezclas ya aportan variación)
    aug_balance: bool = True        # nivelar clases (más copias a minorías)
    aug_max_factor: int = 10        # tope de copias = aug_n * aug_max_factor
    snr_db: tuple = (20.0, 50.0)    # rango de SNR para ruido gaussiano
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
    rawfft_fmax: float = 2000.0     # rawfft = SOLO los bins directos de la FFT (sin DC) hasta
                                    # esta frecuencia. Con W=2048 y fs=10 kHz: 409 bins de
                                    # 4.88 Hz. Es exactamente lo que calcula el borde.

    # ---------------- modelo ----------------
    modelo: str = "rf"              # rf|et|hgb|svm|knn|logreg
    multi_salida: str = "ovr"       # "ovr" = un clasificador binario por clase (exportable)
                                    # "nativo" = multi-output de sklearn (referencia)
    n_estimators: int = 20          # 20x8 ≈ 276 KB de nodos en C y mejor F1 que 50x10
    max_depth: int | None = 8       # (3.2 MB) sobre 61 capturas; ver AGENTS.md
    hiperparams: dict = field(default_factory=dict)  # extras para el estimador
    class_weight: str | None = "balanced"
    umbral: float = 0.5             # umbral de decisión multi-label

    # ---------------- evaluación ----------------
    cv: str = "sgkf5"               # "sgkf5" | "logo"
    n_splits: int = 5
    seed: int = 42

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
        if e:
            raise ValueError("Config inválida:\n  - " + "\n  - ".join(e))

    # ---------------- utilidades ----------------
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), default=list, **kwargs)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        d = dict(d)
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
        aug = f"_aug{self.aug_n}" if self.aug_n > 0 else ""
        fil = f"_f{self.filtro}" if self.filtro else ""
        agr = f"_agr{len(set(self.agrupar.values()))}" if self.agrupar else ""
        est = f"{self.estrategia}{self.rawfft_fmax:.0f}" if self.estrategia == "rawfft" else self.estrategia
        arb = f"{self.n_estimators}x{self.max_depth if self.max_depth is not None else 'inf'}"
        fase = "_fasealeat" if self.mix_fase == "aleatoria" else ""
        return f"{est}_W{self.W}_{self.ventana}_{self.modelo}{arb}{mix}{fase}{aug}{fil}{agr}"


# Preset de referencia (baseline del plan)
BASELINE = Config()
