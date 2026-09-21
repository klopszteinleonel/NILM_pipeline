"""Pipeline de clasificación/desagregación NILM (Colab -> ESP32-S3)."""

from .config import Config, BASELINE
from .data import (Dataset, Registro, cargar_dataset, escanear, hash_manifest,
                   guardar_manifest, guardar_cache, cargar_cache,
                   parsear_categorias, multi_hot,
                   TIPO_INDIVIDUAL, TIPO_COMBINADA, TIPO_NADA, CLASE_NADA)
from .preprocess import (disenar_filtro, filtrar, preprocesar_captura,
                         preprocesar_ventana, preprocesar_ventanas)
from .segment import segmentar, segmentar_dataset
from .mix import Mezclas, rms_por_clase, combinar, sintetizar_combinadas
from .augment import copias_por_ventana, aumentar_ventana, aumentar_ventanas
from .features import (ventana_fft, espectro, estimar_f0, armonicas,
                       nombres_features, extraer_features)

from .evaluate import (metricas_multilabel, agregar_por_grupo, matrices_por_clase,
                       umbrales_optimos, umbrales_optimos_exact, importancias, reporte, reporte_combinaciones)
from .train import (Resultado, construir_modelo, predecir_proba, costo_modelo,
                    armar_conjunto, armar_conjunto_train, dividir, run_experiment,
                    entrenar_final, registrar, cargar_leaderboard, robustez_snr)

from .export import exportar, test_paridad, config_pipeline
from . import plots

__version__ = "0.6.0"
