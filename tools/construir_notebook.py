import json, sys

celdas = []
def md(s): celdas.append({"cell_type": "markdown", "metadata": {}, "source": s.strip("\n")})
def code(s): celdas.append({"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": s.strip("\n")})

md("""
# NILM — análisis exploratorio y pipeline de clasificación

El notebook tiene dos partes independientes, que comparten solo la celda de setup:

- **Parte A: Análisis Exploratorio de Datos (EDA).** No entrena nada. Para correr solo esta parte: ejecutá el setup y las celdas A, y frená antes de la Parte B.
- **Parte B: pipeline de clasificación.** Validación, barridos y export al ESP32-S3.

La lógica vive en el paquete `nilm_pipeline/` (repo `NILM_pipeline`): `eda.py` para la Parte A y el resto para la Parte B.

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
# Parte A: Análisis Exploratorio de Datos

**Qué queremos saber.** Antes de elegir modelo: qué hay en estas señales, si los datos son confiables y, sobre todo, **en qué frecuencias se destaca cada clase**, para trabajar solo sobre las franjas que aportan y descartar las que son ruido.

**Unidad de análisis.** Las capturas de una misma ráfaga están separadas por segundos y son casi duplicados. Por eso las estadísticas por clase pesan igual a cada ráfaga, y los tests de separabilidad usan la ráfaga como unidad. En `luces` cada ráfaga es otra cantidad de lámparas encendidas, y el cambio puede tardar menos que la pausa que separa ráfagas, así que ahí también se corta cuando el nivel salta más de 1.5 dB entre capturas.

**Una clase reúne equipos de distinta potencia.** Cada equipo consume lo suyo, así que una diferencia de corriente dentro de una clase es esperable y no es, por sí sola, una anomalía.

**Espectro.** Se calcula con las mismas funciones que el borde: ventana Hann de 2048 muestras (0.2 s), bins de 4.88 Hz, amplitud en A.

**Cómo leer esta parte.** Los textos "Lectura" comentan el dataset `dfe60d02ca` (297 capturas, 2026-11-04). Si el hash que imprime la celda siguiente es otro, los gráficos se actualizan solos pero esos textos hay que releerlos.
""")
code("""
from nilm_pipeline import eda
eda.estilo()
ds_eda = cargar_dataset(RUTA_DATOS)
E = eda.calcular_espectros(ds_eda)
print(ds_eda.resumen())
print(f"\\n{E.mag.shape[0]} ventanas x {E.mag.shape[1]} bins de {E.fs / E.W:.2f} Hz")

# Clases físicamente equivalentes, para los análisis que comparan una clase contra el resto
GRUPOS = {"calefon": "resistiva", "horno": "resistiva", "pava": "resistiva", "plancha": "resistiva",
          "secadorpelo": "resistiva", "airecalor": "aire"}
""")

md("""
## A1. Inventario: cuántos datos hay realmente
""")
code("""
display(eda.inventario(E))
eda.plot_inventario(E)
plt.show()
display(eda.combinadas_por_lugar(E))
""")
md("""
**Lectura.**
- Las 169 capturas individuales son **51 ráfagas**: ese es el tamaño efectivo. `parasito`, `tele`, `airecalor`, `hidro` y `secadorpelo` tienen una sola.
- **Ráfagas no es lo mismo que equipos**: `pava` tiene 2 ráfagas en 2 lugares, pero es la misma pava. Las 17 de `luces` son configuraciones, varias de las mismas lámparas. En cambio, las 5 de `aire` son 5 equipos distintos.
- Ninguna clase está en los 4 lugares. `lavarropa`, por ejemplo, solo se midió en dos.
- Las 112 combinadas vienen de solo dos lugares y casi todas las combinaciones tienen una única ráfaga. Toda conclusión sobre combinadas se apoya en muy pocas ocasiones independientes.
""")

md("""
## A2. Calidad de los datos
Faltantes y saturación, offset de continua, frecuencia de red real y capturas que se apartan de su propia ráfaga.
""")
code("""
q = eda.calidad(E)
print(f"Capturas por duración [s]: {q['duracion_s']}")
print(f"Corriente máxima: {q['max_abs_A']:.1f} A (riel ±{q['riel_A']:.2f} A); capturas con muestras en el riel: {q['con_muestras_en_riel']}")
print(f"f0 mediana por captura: {q['f0_captura_min_max'][0]:.2f} a {q['f0_captura_min_max'][1]:.2f} Hz")
print(f"Con ese desvío, la armónica {q['armonica_que_se_corre_un_bin']:.0f} ya se corre un bin entero ({q['ancho_bin_hz']:.2f} Hz)")
display(q["dc_mA_por_lugar"])
eda.plot_calidad(E)
plt.show()
print("Capturas que se apartan de su ráfaga:")
display(eda.atipicas(E))
""")
md("""
**Lectura.**
- **Sin saturación ni capturas dañadas**: máximo 22.3 A contra un riel de ±24.75 A. El loader tampoco reporta archivos inválidos ni faltantes.
- **El offset es una marca del lugar**: unos -25 mA en `casa`, +118 en `depaleo`, +105 en `depamica`. Si llegara al modelo, aprendería a reconocer la casa. El pipeline lo quita por ventana, y esto confirma que hay que seguir haciéndolo. En `local` además varió dentro de la sesión: unos -200 mA en los dos aires (18:25 a 18:30), +27 mA a las 18:37 y ~+85 mA desde las 18:44. No depende del equipo, porque los dos aires dan lo mismo, y la causa no se conoce.
- **La red se mueve**: 49.91 a 50.11 Hz entre capturas, ya descontado un sesgo del estimador de f0, que con esta ventana lee +0.074 Hz. A partir de la armónica ~43 (2.1 kHz) eso corre la línea un bin completo. En otra red o en otro momento puede moverse más: la Parte B mide lo que eso le hace al modelo.
- **Atípicas**: se eliminó del dataset `lavarropa/20260922_180045`, que medía 81 mA contra ~1.35 A de mediana y parecía la máquina en pausa. Las `lavarropa` que quedan en la tabla, y un `aireinverter` de `casa` (0.77 A contra ~1.8 A), parecen cambios de estado reales. La A12 lo analiza con la firma completa.
""")

md("""
## A3. Forma de onda
Un ciclo promedio por captura, alineado a la fundamental de la propia corriente. Cada escala vertical es distinta: acá importa la forma.
""")
code("""
eda.plot_ciclos(E)
plt.show()
""")
md("""
**Lectura.** Se distinguen tres comportamientos físicos:
- **Resistivas** (`pava`, `plancha`, `horno`, `calefon`): senoidales puras. Solo cambia la amplitud, porque cada equipo tiene su potencia: `horno` tiene un equipo de ~5.5 A pico y otro de ~11 A.
- **Motores** (`aire`, `airecalor`, `heladera`): senoide deformada y, en `aire`, algo asimétrica.
- **Electrónica** (`tele`, `luces`, `aireinverter` de `depamica`): pulsos angostos, típicos de un rectificador con capacitor. `lavarropa` queda entre medio.

Dos alertas para una casa nunca vista: los dos `aireinverter` son **ondas muy distintas** entre sí, y `lavarropa` va de ~1 A a 8 A pico según el momento del ciclo.
""")

md("""
## A4. Descriptores simples
Tres números por captura. Sirven de referencia: ¿qué se separa ya sin mirar el espectro?
""")
code("""
eda.plot_descriptores(E)
plt.show()
""")
md("""
**Lectura.**
- **El THD ordena por familia**: resistivas 0.02, `heladera` 0.17, `aire` 0.22, `lavarropa` 0.49, `aireinverter` 0.79, `tele` y `luces` ~1.2. En `nada` y `parasito` no significa lo mismo, porque la fundamental es casi tan chica como el ruido.
- **Las cuatro resistivas se pisan** en los tres descriptores, y el RMS tampoco las separa: dos equipos de clases distintas consumen lo mismo.
- **La corriente sola no alcanza**, porque las clases se solapan: `aire` y `calefon`, o `tele` y `heladera`.
""")

md("""
## A5. Firmas acumuladas por clase
Se superponen los espectros de **todas las ventanas de cada clase**. El color indica qué fracción de las ventanas tiene esa amplitud en esa frecuencia, de modo que la "nube" muestra la firma estadística y no un único ejemplo. Línea oscura: mediana de la clase. Línea gris: piso de ruido (P99 de `nada`).
""")
code("""
eda.plot_firmas_acumuladas(E, fmax=3000.0)
plt.show()
""")
code("""
# Detalle de la zona baja
eda.plot_firmas_acumuladas(E, fmax=500.0, ncols=4)
plt.show()
""")
md("""
**Lectura.**
- **Toda la información está en el peine de armónicas de 50 Hz.** Entre dientes la señal cae al piso, salvo en `aireinverter` y parte de `lavarropa`.
- **El piso es muy bajo y plano**: unos 0.2 mA por bin (P99 ~0.5 mA) hasta 5 kHz. Más de 80 dB bajo una carga grande.
- **Las resistivas no son "una línea sola"**: tienen H3 y H5 claras. Comparado con los pares I+V de calibración de octubre, la corriente sobre carga resistiva tiene H3/H5 = 1.41 % / 1.12 % y la tensión 1.55 % / 1.05 %: **esas armónicas son la distorsión de la tensión de red, no la firma de la carga**. Lo confirma un experimento natural: **la misma pava** da H1 = 12.24 y 12.23 A en `depaleo` y `depamica`, pero H3 = 0.11 y 0.19 A, y H5 = 0.17 y 0.09 A. Cambian con la casa, así que no conviene aprender de ellas.
- **Armónicas pares**: en resistivas, `tele` y `luces` quedan muy por debajo de las impares; en `aire` y `lavarropa` son importantes. Esta es una franja útil de verdad (ver A9).
- **`aireinverter` es otro mundo**: nube ancha y un lomo de 1.5 a 3.5 kHz, mucho más fuerte en el equipo de `casa`.
""")

md("""
## A6. Firma por armónica y por lugar
La pregunta de una casa nunca vista: ¿la misma clase tiene la misma firma en otro lugar? Mediana por lugar y banda P10-P90. Primero en amperes y después relativa a la fundamental (forma).
""")
code("""
eda.plot_firmas_armonicas(E)
plt.show()
""")
code("""
eda.plot_firmas_armonicas(E, relativo=True)
plt.show()
""")
md("""
**Lectura.**
- **Una referencia útil**: la misma `pava` en dos casas difiere 4.2 dB en forma, y 0.1 % en corriente. Una diferencia de forma de ese orden en una carga con pocas armónicas propias puede ser solo la red.
- **Transfieren bien entre lugares**: resistivas, `lavarropa` y, en buena medida, `heladera` y `aire`.
- **No transfiere**: `aireinverter`. Entre sus dos unidades hay diferencias de un orden de magnitud por armónica.
- **`lavarropa` generaliza muy bien**: son máquinas distintas y su forma difiere apenas 2.3 dB entre `depaleo` y `depamica`. Son solo dos unidades, así que falta confirmarlo con más.
- **Un lugar no es un equipo**: en `local` se midieron dos aires, uno normal y un aire ventana, con H3 muy distinta (1.29 A y 0.51 A). En `casa` también hay dos, de 8.5 y ~3.5 A.
- `luces` conserva el patrón impar, pero su amplitud cambia con la cantidad y el tipo de lámparas.
""")

md("""
## A7. Energía acumulada: hasta dónde llega cada clase
La otra lectura de "acumulada": cuánta potencia se juntó al llegar a cada frecuencia. Se resta el piso y se excluye la fundamental, porque si no todas las curvas llegan casi al 100 % en 50 Hz.
""")
code("""
display(eda.energia_acumulada(E)[0].round(1))
eda.plot_energia_acumulada(E)
plt.show()
""")
md("""
**Lectura.**
- **La fundamental domina**: 99.9-100 % en resistivas, 96-98 % en `aire` y `heladera`, 82 % en `lavarropa`, y solo 42-53 % en `tele`, `luces` y `aireinverter`.
- **El 99 % de lo demás está antes de ~1.15 kHz** en casi todas las clases. Las excepciones son `parasito` (1.35 kHz, ya casi en el piso) y `aireinverter` (1.97 kHz).
- **Ojo con concluir "cortemos en 1.2 kHz"**: energía no es lo mismo que información. A9 muestra que hay armónicas de muy poca energía que separan clases.
""")

md("""
## A8. Dónde hay señal y dónde solo ruido
Potencia sobre el piso por banda (0 dB = igual que `nada`), y cuánta potencia cae fuera de las armónicas de 50 Hz.
""")
code("""
eda.plot_snr_por_banda(E)
plt.show()
print("% de la potencia (sin fundamental, hasta 3 kHz) que cae entre armónicas:")
print(eda.energia_interarmonica(E).to_string())
""")
md("""
**Lectura: acá está lo que se puede descartar con seguridad.**
- **Arriba de 3 kHz solo queda piso**: unos 0-5 dB en todas las clases, salvo `aireinverter` (23 dB entre 3 y 3.5 kHz). Coincide con el anti-alias de 2.2 kHz, que a 3 kHz ya atenúa 11 dB.
- **Entre armónicas tampoco hay nada**: menos del 1 % de la potencia, salvo `aireinverter` con 17 %. El equipo de `depamica` tiene bandas laterales en 100 ± 17, 200 ± 15 y 300 ± 17 Hz, posiblemente ligadas a la velocidad del compresor, y el de `casa` el lomo de ~2 kHz.
""")

md("""
## A9. Qué frecuencias separan a cada clase
Tener energía no alcanza: una armónica sirve si **difiere de las demás clases**. Se compara cada clase contra el resto con el AUC, por armónica y con ráfagas como unidad: 1 = la clase siempre tiene más, 0 = siempre menos, 0.5 = no separa.

Con tan pocas ráfagas, y mirando muchas armónicas a la vez, un AUC alto puede salir por azar. Por eso se marca con un **punto negro** lo que supera un test de permutación que corrige por todas las armónicas juntas (5 % de error por clase).

Las clases se agrupan con `GRUPOS`. En combinadas se compara dentro de cada lugar, para no confundir la clase con la casa.
""")
code("""
eda.plot_auc(E, GRUPOS)
plt.show()
display(eda.armonicas_significativas(E, GRUPOS))
""")
md("""
**Lectura** (`+k`: la clase tiene más de esa armónica; `-k`: tiene menos).
- **`resistiva`**: se define por H1 alta y por tener menos de casi todo lo demás. Coherente con A5: lo que queda es la red.
- **`aire`**: sobre todo **H2, H4, H6 y H8**, armónicas pares y bajas (<400 Hz).
- **`lavarropa`**: casi todas las **pares** de H8 en adelante, más H3. Es la clase que más gana con las pares.
- **`luces`**: **impares** altas en forma (H3 en adelante), con poca fundamental y pocas pares. Sus ráfagas no son del todo independientes: varias son las mismas lámparas en otra configuración.
- **`aireinverter`**: casi todo el rango alto, incluso en combinadas. Ojo: son 2 ráfagas individuales, y sus combinadas son todas del mismo equipo.
- **`heladera`**: no tiene una franja propia; solo se destaca por tener menos que el resto.

**El resultado más importante está en el panel de combinadas**: solo `aireinverter` y `resistiva` (H1) superan el azar. `heladera`, `lavarropa` y `luces` **no tienen ninguna armónica que por sí sola las delate en una mezcla real**. Esto no prueba que sean indetectables, porque un clasificador combina varias. De hecho, el modelo logra F1 0.91 con `lavarropa` en combinadas, y son dos máquinas distintas (A6). En cambio `heladera` (0.38) y `luces` (0.32) sí son las difíciles. Falta `aire`: en `depaleo` casi todas las combinadas lo tienen, y no hay contra qué compararlo.

**Cuidado con la forma en armónicas altas**: dividir por H1 infla el piso en cargas chicas y lo achica en las grandes. Parte de los puntos de `luces` y `resistiva` por encima de k≈25 pueden ser ese efecto.
""")

md("""
## A10. Parecidos entre clases
Diferencia media en dB entre las firmas medianas de cada par de clases, ordenadas por agrupamiento jerárquico. Solo se escriben los valores chicos.
""")
code("""
eda.plot_distancias(E)
plt.show()
display(eda.variacion_entre_lugares(E))
""")
md("""
**Lectura.**
- **En la forma aparecen tres familias sin habérselo pedido**: resistivas (2-4 dB entre sí), motores (`aire`, `airecalor`, `heladera`, 7-10 dB) y electrónica (`tele`, `luces`, `aireinverter`, 5-9 dB). `lavarropa` queda algo aparte (9-15 dB de los motores), y `parasito` no se puede ubicar con confianza porque está casi en el piso. Respalda agrupar las resistivas y la idea pendiente de clasificar por tipo eléctrico.
- **`tele` y `luces` están a 5.2 dB**: probablemente son el mismo tipo de circuito, una fuente conmutada. Ayuda a explicar que `tele` aparezca como falso positivo.
- **La tabla explica la dificultad de una casa nueva**: en `aireinverter` (11.3 dB contra 7.6), `luces` (7.6 contra 5.2) y `heladera` (7.7 contra 6.6) **una misma clase cambia más entre lugares que lo que la separa de su clase más parecida**. Ningún modelo puede resolver eso con estos datos: faltan más unidades de cada aparato.
""")

md("""
## A11. Redundancia entre frecuencias
Correlación de Spearman entre armónicas y PCA sobre el log del espectro, ambos por ráfaga. ¿Cuántas de las 614 variables aportan algo distinto?
""")
code("""
eda.plot_correlacion(E)
plt.show()
display(eda.pca_resumen(E))
eda.plot_pca(E, GRUPOS)
plt.show()
eda.plot_pca_cargas(E)
plt.show()
""")
md("""
**Lectura.**
- **Hay mucha redundancia**: la mayoría de las armónicas se correlacionan fuerte entre sí, con un patrón de damero que separa pares de impares. Son dos grupos de variables, no 60 independientes.
- **5 componentes explican el 90 % de la varianza y 10 el 95 %**, sobre 614 bins. Es una cota optimista porque son solo 51 ráfagas (máximo posible: 50 componentes), pero indica que mirar cientos de bins no agrega cientos de datos.
- **Los ejes se interpretan físicamente**: PC1 (63 %) es el nivel general de contenido sobre el piso, con cargas del mismo signo en todo el espectro. PC2 contrasta baja frecuencia con alta y pares con impares. PC3 pesa casi solo las impares.
- **Se ven las familias**: `aire` se separa bien, `resistiva` queda compacta y `luces` forma su propio grupo, con `tele` cerca, a pesar de sus distintas cantidades de lámparas. `lavarropa` está más dispersa, y `heladera` y `resistiva` caen cerca entre sí en este plano, porque 2 componentes no bastan. `aireinverter` son dos puntos lejanos, otra vez dos equipos muy distintos.
""")

md("""
## A12. Distribución dentro de cada clase y outliers
Cada muestra es un espectro de muchas dimensiones, así que un histograma por variable diría poco. La firma de cada captura se resume en sus 40 primeras armónicas (en dB y recortadas al piso de ruido), y se compara contra **el resto de su clase, sin su propia ráfaga**. La diferencia se separa en dos preguntas:

- **Nivel**: ¿consume más o menos? Es la diferencia en H1. Con 6 dB, la corriente es el doble o la mitad.
- **Forma**: ¿tiene otra firma? Es la diferencia de H_k/H1, solo en armónicas que superan el piso.

**Outlier no significa error.** Se revisan tres situaciones distintas:
1. **Una captura que cambia dentro de su ráfaga** (6 dB de nivel u 8 dB de forma contra capturas tomadas segundos antes): cambio de estado o accidente.
2. **Una ráfaga entera que difiere del resto de su clase**: es esperable, porque suele ser otro equipo u otra configuración. Sirve para ver la variedad real de la clase, no para buscar errores.
3. **Una captura más cerca de otra clase que de la suya**: candidata a etiqueta dudosa. Las clases de un mismo grupo (`GRUPOS`) no compiten entre sí.
""")
code("""
D = eda.distribucion_muestras(E, GRUPOS)
eda.plot_distribucion_clases(D)
plt.show()
""")
md("""
**Lectura.**
- **Las más compactas**: `pava`, `plancha` y `airecalor`.
- **Las más dispersas en nivel**: `luces` (de -8 a +14 dB, según cuántas lámparas haya), `lavarropa` (de -11 a +9 dB, según la etapa) y `aire`, con un equipo de `casa` a +8 dB.
- **La más dispersa en forma**: `aireinverter`, con todas sus capturas sobre el umbral.
- **En resistivas, la forma no indica un problema**: sus ~5 dB vienen de las armónicas de la red (A5) y de que están cerca del piso. Lo que las distingue es el nivel: `horno` de `local` queda 6 dB abajo y `calefon` de `casa` 3 dB arriba, que son otros equipos.
""")
md("""
### Nivel contra forma, y qué revisar
Cada punto es una captura. Las líneas son los umbrales y los números identifican las capturas de la primera tabla. La segunda tabla lista las ráfagas enteras que se apartan de su clase.
""")
code("""
eda.plot_nivel_forma(D)
plt.show()
print("Capturas para revisar:")
display(eda.muestras_atipicas(D))
print("Ráfagas que se apartan del resto de su clase:")
display(eda.rafagas_atipicas(D))
""")
md("""
### Mapa de muestras: en qué se diferencia cada captura
Cada fila es una captura y cada columna una armónica, contra el centro de su clase. Las líneas separan ráfagas, y a la derecha van los números de la tabla. Una banda vertical indica una armónica que varía en toda la clase; un bloque horizontal, una captura o ráfaga distinta.
""")
code("""
eda.plot_mapa_muestras(E, D)
plt.show()
""")
md("""
### Todas las capturas en un mismo plano
PCA ajustado sobre ráfagas y aplicado a todas las capturas. En gris están las demás clases, para ver dónde cae cada una respecto del resto.
""")
code("""
eda.plot_pca_muestras(E, D)
plt.show()
""")
md("""
**Lectura.**

**No aparece ninguna etiqueta claramente errónea.** Casi todo lo marcado son estados reales de la carga:

| Clase | Qué se ve | Interpretación |
|---|---|---|
| `lavarropa` | 0.43 A (#10), 2.6 A con THD 0.13 (#9), de 2.8 a 4.4 A dentro de una misma captura (#8) | etapas del ciclo |
| `aireinverter` | en `casa`, 0.77, 1.7 y 1.8 A con formas distintas (#1 a #6); en `depamica`, un grupo compacto | el inverter cambia de régimen, y sus dos equipos no se parecen |
| `heladera` | ~0.43 A con THD 0.19, y ~0.6-0.7 A con THD 0.12-0.17 | la corriente baja con el tiempo: 0.59 a 0.44 A en `depaleo` y 0.72 a 0.61 A en `local` |
| `aire` | en `casa`, un equipo de 8.5 A y otro que sube de 2.8 a 4.2 A en 3 minutos | equipos de distinta potencia, y un régimen que todavía no se estabilizó |
| `luces` | de 0.10 a 0.37 A en `depaleo` y hasta 0.88 A en `casa` | distinta cantidad de lámparas encendidas |
| `parasito` | tres niveles de forma dentro de su única ráfaga (#16, #17) | variación chica de una señal cercana al piso |

**Sobre las capturas "más cerca de otra clase".**
- `luces` de `casa` (#14, #15) queda más cerca de `tele` que de las demás `luces`: coincide con que son el mismo tipo de circuito (A10).
- `aireinverter` (#1, #5, #6, #7) no tiene una zona propia: sus equipos caen lejos entre sí.
- `aire/20260912_174929` no está marcada, pero en el PCA se corre hacia `airecalor`. Mantiene los 8.35 A de su ráfaga, así que no parece una etiqueta cruzada.

**Qué implica.**
- **Una ráfaga no siempre es un solo estado**: `aireinverter` y `lavarropa` cambian dentro de la suya.
- **Una clase no es una sola nube**: `lavarropa`, `heladera`, `aire` y `aireinverter` tienen varios regímenes. Conviene capturar y anotar el estado de la carga, y el tiempo desde que arrancó.
- **El nivel no es una propiedad de la clase**: cada equipo tiene su potencia, y una casa nueva puede tener una potencia que el modelo nunca vio. La Parte B probó rangos de amplitud más amplios: en las mezclas no cambian nada, y por captura suben las individuales pero bajan el exact en combinadas.
- **No conviene borrar nada más**: se eliminó la captura de 81 mA de `lavarropa`, y lo que queda son estados que el modelo va a encontrar en una casa real.
""")
md("""
### Las combinadas reales
La misma vista, tomando cada combinación como una categoría. Interesa porque son el test: una etiqueta mal puesta acá afectaría directamente las métricas.
""")
code("""
DC = eda.distribucion_muestras(E, bloque="comb")
eda.plot_distribucion_clases(DC)
plt.show()
display(eda.muestras_atipicas(DC))
display(eda.rafagas_atipicas(DC))
""")
md("""
**Lectura.**
- **La dispersión viene de `lavarropa`**: `lavarropa+luces` y `heladera+lavarropa+luces` van de -14 a +9 dB, porque el ciclo sigue cambiando dentro de la combinada. Todas sus marcas son compatibles con eso.
- **`aireinverter+heladera+luces` es la de forma más variable** (8 dB de mediana), coherente con los regímenes del inverter.
- **El resto es compacto**, y no aparece ninguna captura que sugiera una etiqueta equivocada. Ojo: esta vista compara cada captura con su propia combinación, así que no detectaría una combinación entera mal etiquetada.
""")

md("""
## A13. Combinadas reales: ¿la firma de A+B es la suma de las firmas?
Las mezclas sintéticas del pipeline suponen que sí, así que conviene medirlo. Cada combinada se compara con la suma de sus cargas medidas solas en el mismo lugar. Si las corrientes están en fase se suman en forma lineal; si tienen fases sin relación, se suman en cuadratura.
""")
code("""
eda.plot_superposicion(E)
plt.show()
""")
md("""
**Lectura.**
- **La fundamental suma lineal**: real / suma lineal = 1.02 en la mediana. Las cargas están casi en fase.
- **Las armónicas no**: desde H3, las impares dan 0.74 contra la suma lineal y 0.97 contra la cuadrática. **Se comportan como si tuvieran fases sin relación**, porque cada carga tiene su propio ángulo en cada armónica.
- **Para el pipeline**: las mezclas sintéticas alineadas por la fundamental ya reproducen este comportamiento (medido aparte contra la suma lineal: H1 1.00, impares 0.77, pares 0.83; las reales dan 1.02, 0.74 y 0.86), porque cada carga conserva sus propias fases de armónicas. La fase aleatoria, en cambio, subestima H1 (0.89).
- **Dispersión grande**: `lavarropa` cambia de estado, así que la "carga sola" no siempre es la que estaba en la combinada. Los puntos muy por encima de la diagonal son de combinaciones con `lavarropa`.
""")

md("""
### Visibilidad de una carga chica bajo una grande
Para cada par: |H_k| de la carga S dividido por cuánto varía por sí sola esa armónica en la carga G, entre ventanas de una misma ráfaga. Se muestra la mejor armónica. Con margen menor a 1, lo que aporta S queda dentro de la fluctuación de G. Es una cota **optimista**: no incluye la variación entre unidades ni entre estados.
""")
code("""
eda.plot_visibilidad(E)
plt.show()
""")
md("""
**Lectura.**
- **`parasito` queda bajo la fluctuación** de cualquier motor o inverter (margen 0.1-0.3). Con esas cargas prendidas no se lo puede detectar. Sola, en cambio, se distingue del piso (fundamental de ~24 mA contra ~5 mA de `nada`).
- **`heladera` está justo en el límite** bajo `aire`, `airecalor`, `lavarropa` y `aireinverter` (1.0-1.5). Coincide con su F1 de 0.38 en combinadas en casa nueva.
- **`luces` bajo `aire` da margen 5.3**, y aun así su F1 en combinadas es 0.32. Físicamente se la puede ver: el problema es más bien que cada casa tiene otras lámparas (A10).
- **Las resistivas se ven siempre**, en H1.
""")

md("""
## A14. Síntesis: franjas por clase
""")
code("""
display(eda.sintesis_franjas(E))
display(eda.costo_franjas(E))
plt.rcdefaults()     # la Parte B usa el estilo por defecto de matplotlib
""")
md("""
**Respuesta a la pregunta inicial: qué franjas usar**

| Clase | Qué la identifica | Franja candidata |
|---|---|---|
| `resistiva` | amplitud de la fundamental | 50 Hz. Lo demás es la distorsión de la red |
| `aire` | fundamental y **armónicas pares bajas** | 50-400 Hz |
| `lavarropa` | H3 y **armónicas pares** | pares; la energía termina ~450 Hz, la separación llega hasta 3 kHz |
| `heladera` | sin franja propia; poco contenido alto | < 750 Hz |
| `luces`, `tele` | **impares** altas | impares, 150 Hz a ~2 kHz |
| `aireinverter` | contenido ancho, también entre armónicas | 0-3 kHz, **sin** restringirse a armónicas |

**Lo que se puede descartar con buen respaldo**
1. **Arriba de 3 kHz**: es piso de ruido en todas las clases salvo `aireinverter`.
2. **Los bins entre armónicas**: menos del 1 % de la potencia, también salvo `aireinverter`.
3. **H3 y superiores como firma de una resistiva**: reflejan la red de la casa.

**Lo que NO conviene descartar**
- **Las armónicas pares**: tienen poca energía, pero son lo que más separa a `aire` y `lavarropa`. Un filtro solo por impares o solo por energía las perdería.
- **El rango entre 1.2 y 3 kHz**: casi no tiene energía, pero sí separación. Un barrido anterior de la Parte B también favorecía 3 kHz sobre 2 kHz (F1 0.686 contra 0.668), aunque por poco más que el ruido entre semillas.

**Costo real en el ESP32-S3.** La FFT calcula todos los bins igual: elegir franjas **no ahorra FFT**, sino memoria y tamaño de los árboles. Como estimación, una FFT radix-2 de 2048 puntos cuesta ~45 mil multiplicaciones reales y Goertzel ~2 mil por bin, así que Goertzel solo conviene por debajo de ~22 bins, lo cual queda corto incluso para H1-H20 con tolerancia. Además, un Random Forest ya elige sus variables, y la Parte B lo confirmó: **quedarse solo con las armónicas no empeora ni reduce el modelo, y las franjas angostas por clase empeoran**.

**Lo que limita más que las franjas.** Una misma clase cambia entre casas más de lo que la separa de otra (A10), y en mezclas reales `heladera` y `luces` no tienen una armónica que las delate por sí sola (A9). Ninguna selección de frecuencias resuelve eso: hacen falta más unidades de cada aparato.

**Próximos pasos posibles**
1. Sumar `aireinverter`, `heladera` y `luces` de otras casas.
2. Capturar siempre corriente y tensión, para separar la distorsión de la red de la firma de la carga.

Las franjas y la corrección de f0 ya se probaron en la sección 6 de la Parte B.
""")

md("""
# Parte B: pipeline de clasificación
A partir de acá: validación por lugar, barridos y export al ESP32-S3. No depende de nada de la Parte A.
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
`Config` es la única fuente de verdad. Base (2026-11-04): **FFT cruda** (solo los 614 bins directos de la FFT de W=2048 hasta 3 kHz) **con el eje corregido por la f0 estimada** de cada ventana,
sin augment, 300 mezclas sintéticas por fold con todas las combinaciones por igual (solo para entrenar), OvR de RandomForest 20×8.
El test son siempre capturas reales; la validación por default es leave-one-lugar-out (casa nunca vista).
**Las clases con pocas muestras se descartan hasta tener más** (menos de 3 capturas o menos de 2 ráfagas: hoy `hidro`, `secadorpelo` y `tele`).
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
- Cada experimento se repite con varias semillas (`run_repetido`): el desvío entre semillas es del orden de 0.02–0.05, así que
  **una diferencia menor que el desvío no es evidencia**. Para comparar dos configs usá `comparar(a, b)` (pareado por semilla) y
  `comparar_bootstrap(ds, a, b)`: su intervalo remuestrea ráfagas del test, porque el desvío entre semillas no mide cuánto depende el
  resultado de qué ocasiones se midieron. Ninguno de los dos mide la variación entre casas, porque hay solo 4 lugares.
- Referencia dentro de las casas conocidas: `cv="sgkf5"` agrupado por **ráfaga** (capturas casi idénticas nunca se reparten
  entre train y test). El CV por captura que usábamos antes era optimista.
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
## 6. Barridos: hipótesis del EDA
Cada variante es un `CFG.clonar(...)` corrido con `N_SEMILLAS_BARRIDO` semillas; el leaderboard guarda media y desvío. Cada grupo prueba algo que salió de la Parte A.
La tabla agrega la diferencia contra la base y su intervalo del 95 % por ráfagas (`comparar_bootstrap`). Si el intervalo incluye el 0, la diferencia no es evidencia.

Este barrido es corto a propósito, porque en Colab cada variante tarda varios minutos. El script `tools/experimentos_eda.py` corre el barrido completo de la auditoría (franjas por clase, forma, red simulada, etc.) con más semillas.
""")
code("""
N_SEMILLAS_BARRIDO = 3
BASE_ANTERIOR = dict(mix_sesgo_grande_chica=0.5, mix_total=100, rawfft_f0=False)     # sin contar el descarte de clases
variantes = {
    # --- de dónde viene la base actual
    "base_anterior":   CFG.clonar(**BASE_ANTERIOR),
    "sin_eje_f0":      CFG.clonar(rawfft_f0=False),
    "con_sesgo":       CFG.clonar(mix_sesgo_grande_chica=0.5),
    "mix100":          CFG.clonar(mix_total=100),
    # --- la otra candidata de la auditoría y sus partes
    "rafaga_hoja5":    CFG.clonar(**BASE_ANTERIOR, mix_por_rafaga=True, hiperparams={"min_samples_leaf": 5}),
    "por_rafaga":      CFG.clonar(mix_por_rafaga=True),
    "hoja5":           CFG.clonar(hiperparams={"min_samples_leaf": 5}),
    # --- franjas de frecuencia
    "solo_armonicas":  CFG.clonar(rawfft_mascara="armonicas"),
    "fmax2000":        CFG.clonar(rawfft_fmax=2000.0),
    # --- nivel por equipo
    "train_escala1.6": CFG.clonar(train_escala=(0.6, 1.6)),
    # --- referencia
    "sin_mix":         CFG.clonar(mix_habilitado=False),
}
PROYECCION = {}
NO_COMPARABLES = ()
reps = {"base": rep}
filas = []
for nombre, c in variantes.items():
    r = run_repetido(ds, c, n_semillas=N_SEMILLAS_BARRIDO, leaderboard=LEADERBOARD, etiqueta=nombre)
    reps[nombre] = r
    f = {"variante": nombre, "todas_s": r.media["todas_s_f1_micro"], "comb_s": r.media["comb_s_f1_micro"],
         "comb_exact": r.media["comb_s_exact_match"], "ind_s": r.media["ind_s_f1_micro"], "nodos": round(r.media["nodos_totales"])}
    if nombre not in NO_COMPARABLES:
        t = comparar_bootstrap(ds, rep, r, "todas", mapa_a=PROYECCION.get(nombre))
        k = comparar_bootstrap(ds, rep, r, "comb", mapa_a=PROYECCION.get(nombre))
        f.update(dif_todas=t["dif_f1"], ic_todas=t["ic95_f1"], dif_comb=k["dif_f1"], ic_comb=k["ic95_f1"], dif_exact=k["dif_exact"], ic_exact=k["ic95_exact"])
    filas.append(f)
    print(f"{nombre:18s} todas {f['todas_s']:.3f}  comb {f['comb_s']:.3f}  exact {f['comb_exact']:.3f}  nodos {f['nodos']}", flush=True)
tabla_barrido = pd.DataFrame(filas).set_index("variante").round(3).sort_values("dif_todas", ascending=False)
display(tabla_barrido)
""")
code("""
# Solo filas de este dataset: el leaderboard puede traer corridas viejas si se persiste entre sesiones
lb = cargar_leaderboard(LEADERBOARD, ordenar_por="todas_s_f1_micro")
lb = lb[lb["manifest"] == ds.manifest_hash[:10]]
cols = ["etiqueta", "nombre", "todas_s_f1_micro", "todas_s_f1_micro_std", "comb_s_f1_micro", "comb_s_exact_match",
        "ind_s_f1_micro", "nodos_totales", "n_semillas"]
display(lb[[c for c in cols if c in lb.columns]].head(20))
plots.plot_leaderboard(lb, y="todas_s_f1_micro", y2="comb_s_f1_micro")
plt.show()
""")
md("""
### Robustez a la frecuencia de red
Los datos se midieron con la red entre 49.91 y 50.11 Hz. Acá se simula otra frecuencia en las capturas de test. `eje_f0` estima la f0 de cada ventana y corrige el eje.
""")
code("""
filas = {}
for nombre, c in (("base", CFG), ("sin_eje_f0", variantes["sin_eje_f0"])):
    filas[nombre] = {f"{d:+.1f} Hz": run_repetido(ds, c.clonar(test_f0_hz=d), n_semillas=N_SEMILLAS_BARRIDO).media["todas_s_f1_micro"]
                     for d in (-0.5, 0.0, 0.5)}
display(pd.DataFrame(filas).T.round(3))
""")
md("""
### Resultados de la auditoría (dataset `dfe60d02ca`, 2026-11-04)
De acá salió la base actual, `sesgo0_mix300` + `eje_f0`, a la que después se le sacó `tele` por tener una sola ráfaga. Esta tabla es previa a ese descarte. Validación por lugar, clases vistas. Base anterior: todas 0.708, individuales 0.743, combinadas 0.683, combinadas exact 0.277, 9.2 k nodos (10 semillas). Entre paréntesis, el intervalo del 95 % por ráfagas.

| Candidata (10 semillas) | Δ todas | Δ combinadas | Δ exact comb. | Δ exact ind. | Nodos | F1 con f0 ±0.5 Hz | Export |
|---|---|---|---|---|---|---|---|
| Base anterior | - | - | - | - | 9.2 k | 0.58 / 0.46 | sí |
| `rafaga_hoja5` | +0.028 (0.012, 0.045) | +0.031 (0.009, 0.053) | +0.016 (-0.026, 0.066) | +0.014 (-0.010, 0.044) | 7.4 k | 0.62 / 0.56 | sí |
| `sesgo0_mix300` | +0.033 (0.006, 0.059) | +0.063 (0.029, 0.097) | +0.108 (0.021, 0.209) | -0.054 (-0.115, -0.004) | 15.5 k | 0.65 / 0.55 | sí |
| **`sesgo0_mix300` + `eje_f0` (base actual)** | +0.024 (-0.002, 0.050) | +0.050 (0.015, 0.086) | +0.094 (0.009, 0.188) | -0.050 (-0.106, -0.003) | 15.2 k | 0.73 / 0.73 | sí |
| `rafaga_hoja5` + `eje_f0` | +0.012 (-0.002, 0.029) | +0.012 (-0.008, 0.033) | +0.016 (-0.031, 0.074) | +0.011 (-0.017, 0.042) | 7.3 k | 0.71 / 0.70 | sí |

**Lectura.**
- **Lo que más limitaba eran las mezclas**: 91 % tenían una carga grande (66 % en las reales), y `lavarropa+luces`, la combinación real más común, era apenas 1 %. Cubrir todas las combinaciones por igual y con más mezclas sube claramente las combinadas (`sesgo0_mix300`), aunque baja algo las individuales.
- **Las mejoras no se suman**: `rafaga_hoja5` junto con `sesgo0_mix300` da menos que cada una por separado (todas +0.016).
- **La base es frágil ante la frecuencia de red**: corrigiendo el eje con la f0 estimada, el F1 casi no cambia en ±0.5 Hz. Es lo más importante para un producto real, y en condiciones nominales cuesta poco.

**Hipótesis del EDA que no funcionaron.**
- **Franjas angostas por clase** (A14): caen, exact -0.14. Con franjas amplias da lo mismo que la base. Solo armónicas no pierde nada, pero tampoco reduce los nodos.
- **Solo impares**: bajan las individuales. Confirma que las pares importan.
- **Features de forma + H1**: empeoran. El nivel sigue siendo información útil.
- **Escala más amplia**: en las mezclas no cambia nada. Por captura sube las individuales (+0.06 con ±2.5×) y baja exact en combinadas.
- **Distorsión de red simulada**, **alinear con f0** y **armónicas al azar**: efectos chicos.
- **Filtro por corriente**: no aporta. Con factor 0.25 no cambia nada, y con 0.5 empeora apenas.

**Una corrección**: fusionar `tele+luces` parecía sumar +0.044, pero comparado en las mismas clases son +0.022.

**Cuidado.** Se probaron unas 50 variantes sobre los mismos 4 lugares, así que alguna mejora puede ser azar de selección, y estos intervalos no incluyen la variación entre casas. Una casa nueva, sin mirar durante los barridos, es lo único que confirmaría estos números.
""")

md("""
## 7. Evaluación del elegido
Elegí `MEJOR` a mano: solo cambiá la base si la mejora es mayor al desvío, en casi todas las semillas, y su intervalo de `comparar_bootstrap` excluye el 0.
Si no, quedate con la base. `franjas`, `forma_h1` y `filtro_corriente` todavía no se pueden exportar a C. Las matrices por clase son del LOLO (casa no vista), con umbral 0.5.
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
Entrena con todas las individuales (+ mezclas + augment), genera `nilm_modelo.h`, `nilm_config.h`, `nilm_features.h`, `pipeline_config.json` y `modelo.joblib`. Compila en el host un programa C que debe reproducir `predict_proba` de sklearn (diferencia < 1e-5), y otro que del espectro saca las features, corrección de f0 incluida, sin cambiar ninguna decisión.
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
# El espectro -> features en C (corrección de f0 incluida). Parte del mismo espectro: no cubre la FFT de esp-dsp.
paridad_f = test_paridad_features(CARPETA_EXPORT, sen, MEJOR, fs, modelo)
print(paridad_f)
assert paridad_f["acuerdo_decisiones"] == 1.0, "las features en C cambian decisiones del modelo: no flashear"
rob = robustez_snr(modelo, ds, MEJOR)
print("robustez SNR (sobre datos de entrenamiento: mide degradación relativa):", {k: round(v, 3) for k, v in rob.items()})
plots.plot_robustez(rob)
plt.show()
print("archivos en", CARPETA_EXPORT, ":", sorted(os.listdir(CARPETA_EXPORT)))
""")

md("""
## 9. Test congelado (una sola vez, al final)
Reservá desde el principio uno o más lugares/sesiones en `Config.test_congelado`: `run_experiment` los ignora durante
todos los barridos y solo esta celda los usa. Si se mira este resultado para elegir configuraciones, deja de ser un test.
Con los datos actuales (4 lugares) conviene esperar a tener una casa nueva y congelarla entera: todas las decisiones de la Parte B se tomaron mirando estas 4.
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
