"""
Ingesta del dataset NILM.

- Escanea la carpeta de capturas (formato JSON del nodo ESP32-S3) y construye
  un manifest.csv (registro versionable del dataset).
- Soporta tres tipos de capturas:
    * individuales:   capturas_limpio/<categoria>/archivo.json   (categoria="aire")
    * combinadas:     capturas_limpio/combinadas/archivo.json    (categoria="aire,tele")
    * sin carga:      cualquier carpeta, categoria="nada"         → vector cero
- Las etiquetas se representan multi-hot sobre la lista global de clases
  ("nada" es una categoria reservada: no es clase, su etiqueta es todo cero).
- Cada captura es su propio "grupo" (clave para la validación anti-leakage).

Cache: guardar_cache/cargar_cache serializan el dataset a un único .npz para
acelerar la carga en Colab/Drive.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

NOMBRE_CARPETA_COMBINADAS = "combinadas"
CAMPOS_MANIFEST = ["ruta", "tipo", "categoria", "clases", "lugar", "fs",
                   "duracion", "muestras", "sha1", "fecha_archivo"]

TIPO_INDIVIDUAL = "individual"
TIPO_COMBINADA = "combinada_real"
TIPO_NADA = "sin_carga"
CLASE_NADA = "nada"


def parsear_categorias(categoria: str, clase_nada: str = CLASE_NADA) -> tuple[str, ...]:
    """'aire,tele' -> ('aire', 'tele'). Normaliza: minúsculas, sin espacios, ordenadas.
    La categoria reservada `clase_nada` se descarta (sin carga = tupla vacía)."""
    partes = [c.strip().lower() for c in str(categoria).replace(";", ",").split(",")]
    return tuple(sorted(p for p in partes if p and p != clase_nada))


@dataclass
class Registro:
    """Una captura (señal completa, sin segmentar)."""
    ruta: str               # relativa al data_dir
    clases: tuple           # tupla de clases activas (multi-label)
    lugar: str
    fs: float
    muestras: int
    tipo: str               # TIPO_INDIVIDUAL | TIPO_COMBINADA
    datos: np.ndarray       # señal float32
    grupo: str              # id de captura (= ruta sin extensión)

    def multi_hot_en(self, clases: list) -> np.ndarray:
        return multi_hot(self.clases, clases)


@dataclass
class Dataset:
    registros: list
    clases: list            # lista global ordenada de clases
    manifest_filas: list = field(default_factory=list)
    manifest_hash: str = ""
    problemas: list = field(default_factory=list)

    # ---- accesores ----
    def senales(self) -> list:
        return [r.datos for r in self.registros]

    def y(self) -> np.ndarray:
        """Matriz multi-hot (n_registros, n_clases) uint8."""
        if not self.registros:
            return np.empty((0, len(self.clases)), dtype=np.uint8)
        return np.stack([multi_hot(r.clases, self.clases) for r in self.registros])

    def grupos(self) -> np.ndarray:
        return np.array([r.grupo for r in self.registros])

    def tipos(self) -> np.ndarray:
        return np.array([r.tipo for r in self.registros])

    def fs(self) -> float:
        return float(self.registros[0].fs) if self.registros else 0.0

    def filtrar(self, tipo: str | None = None) -> "Dataset":
        regs = [r for r in self.registros if tipo is None or r.tipo == tipo]
        return Dataset(regs, self.clases, self.manifest_filas,
                       self.manifest_hash, self.problemas)

    def agrupar_clases(self, mapa: dict) -> "Dataset":
        """Nuevo Dataset con clases renombradas según `mapa` (clase -> grupo).
        Clases físicamente indistinguibles (ej. calefon/horno/pava/plancha ->
        "resistiva") se fusionan; una captura con dos clases del mismo grupo
        queda con una sola etiqueta. Las no mapeadas se conservan."""
        if not mapa:
            return self
        regs = []
        for r in self.registros:
            clases = tuple(sorted({mapa.get(c, c) for c in r.clases}))
            regs.append(Registro(r.ruta, clases, r.lugar, r.fs, r.muestras, r.tipo, r.datos, r.grupo))
        clases = sorted({c for r in regs for c in r.clases})
        return Dataset(regs, clases, self.manifest_filas, self.manifest_hash, self.problemas)

    def excluir_clases_raras(self, min_capturas: int) -> tuple["Dataset", list]:
        """Quita las clases con menos de `min_capturas` capturas individuales,
        sus capturas y toda combinada que las contenga. Devuelve (dataset, excluidas)."""
        conteo = {c: sum(1 for r in self.registros if r.tipo == TIPO_INDIVIDUAL and r.clases == (c,))
                  for c in self.clases}
        raras = sorted(c for c, n in conteo.items() if n < min_capturas)
        if not raras:
            return self, []
        regs = [r for r in self.registros if not set(r.clases) & set(raras)]
        clases = sorted({c for r in regs for c in r.clases})
        return Dataset(regs, clases, self.manifest_filas, self.manifest_hash, self.problemas), raras

    def resumen(self, min_capturas_aviso: int = 3) -> str:
        lineas = [f"Dataset: {len(self.registros)} capturas, "
                  f"{len(self.clases)} clases, fs={self.fs():.0f} Hz, "
                  f"manifest={self.manifest_hash[:10]}"]
        n_ind = sum(1 for r in self.registros if r.tipo == TIPO_INDIVIDUAL)
        n_comb = sum(1 for r in self.registros if r.tipo == TIPO_COMBINADA)
        n_nada = sum(1 for r in self.registros if r.tipo == TIPO_NADA)
        lineas.append(f"  individuales: {n_ind} | combinadas reales: {n_comb} "
                      f"| sin carga: {n_nada}")
        for c in self.clases:
            inds = [r for r in self.registros
                    if r.tipo == TIPO_INDIVIDUAL and c in r.clases]
            combs = [r for r in self.registros
                     if r.tipo == TIPO_COMBINADA and c in r.clases]
            aviso = "  <- POCAS CAPTURAS" if 0 < len(inds) < min_capturas_aviso else ""
            lineas.append(f"  {c:16s} individuales={len(inds):3d} "
                          f"en_combinadas={len(combs):3d}{aviso}")
        if self.problemas:
            lineas.append(f"  PROBLEMAS ({len(self.problemas)}):")
            lineas += [f"    {p}" for p in self.problemas]
        return "\n".join(lineas)


def multi_hot(clases_registro: tuple, clases_global: list) -> np.ndarray:
    v = np.zeros(len(clases_global), dtype=np.uint8)
    for c in clases_registro:
        v[clases_global.index(c)] = 1
    return v


def _sha1_archivo(ruta: Path) -> str:
    h = hashlib.sha1()
    with open(ruta, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _leer_captura(ruta: Path) -> dict:
    with open(ruta, "r", encoding="utf-8") as f:
        return json.load(f)


def _tipo_captura(rel: str, categoria: str, clases: tuple) -> str:
    if not clases:
        return TIPO_NADA
    en_carpeta_comb = NOMBRE_CARPETA_COMBINADAS in Path(rel).parts
    return TIPO_COMBINADA if (en_carpeta_comb or len(clases) > 1) else TIPO_INDIVIDUAL


def escanear(data_dir: str | Path, clase_nada: str = CLASE_NADA) -> tuple[list, list]:
    """
    Recorre data_dir y devuelve (filas_manifest, problemas), sin cargar las señales.
    Tipo: 'combinada_real' si está bajo la carpeta 'combinadas' o tiene >1 clase;
    'sin_carga' si categoria == clase_nada.
    """
    data_dir = Path(data_dir)
    filas, problemas = [], []
    n_tension = 0
    for ruta in sorted(data_dir.rglob("*.json")):
        rel = ruta.relative_to(data_dir).as_posix()
        try:
            p = _leer_captura(ruta)
            if p.get("magnitud", "corriente") != "corriente":
                n_tension += 1          # canal de tension (CH1): el pipeline usa solo corriente
                continue
            categoria = str(p.get("categoria", "")).strip()
            if not categoria:
                problemas.append(f"{rel}: sin categoria, se omite")
                continue
            clases = parsear_categorias(categoria, clase_nada)
            tipo = _tipo_captura(rel, categoria, clases)
            filas.append({
                "ruta": rel,
                "tipo": tipo,
                "categoria": categoria,
                "clases": "|".join(clases),
                "lugar": p.get("lugar", ""),
                "fs": p.get("fs", ""),
                "duracion": p.get("duracion", ""),
                "muestras": p.get("muestras", ""),
                "sha1": _sha1_archivo(ruta),
                "fecha_archivo": ruta.stem.split("_")[0] if "_" in ruta.stem else "",
            })
        except Exception as e:
            problemas.append(f"{rel}: error al leer ({e})")
    if n_tension:
        problemas.append(f"{n_tension} archivo(s) de tension (magnitud != corriente) omitidos")
    return filas, problemas


def hash_manifest(filas: list) -> str:
    h = hashlib.sha1()
    for f in sorted(filas, key=lambda x: x["ruta"]):
        h.update(f["ruta"].encode())
        h.update(f["sha1"].encode())
    return h.hexdigest()


def guardar_manifest(filas: list, salida_csv: str | Path) -> None:
    Path(salida_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(salida_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_MANIFEST)
        w.writeheader()
        w.writerows(filas)


def cargar_dataset(data_dir: str | Path,
                   incluir_combinadas: bool = True,
                   min_capturas_aviso: int = 3,
                   clase_nada: str = CLASE_NADA) -> Dataset:
    """Escanea y carga todas las capturas. Valida consistencia básica."""
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise FileNotFoundError(f"No existe el directorio de datos: {data_dir}")

    filas, problemas = escanear(data_dir, clase_nada)
    registros = []
    fs_ref = None
    for fila in filas:
        if fila["tipo"] == TIPO_COMBINADA and not incluir_combinadas:
            continue
        ruta = data_dir / fila["ruta"]
        p = _leer_captura(ruta)
        datos = np.asarray(p.get("datos", []), dtype=np.float32)
        fs = float(p.get("fs", 10000))
        muestras = int(p.get("muestras", len(datos)))
        if len(datos) == 0:
            problemas.append(f"{fila['ruta']}: datos vacios, se omite")
            continue
        if len(datos) != muestras:
            problemas.append(f"{fila['ruta']}: len(datos)={len(datos)} != muestras={muestras}, se omite")
            continue
        if not np.isfinite(datos).all():
            problemas.append(f"{fila['ruta']}: datos con NaN/Inf, se omite")
            continue
        if fs_ref is None:
            fs_ref = fs
        elif abs(fs - fs_ref) > 1e-6:
            problemas.append(f"{fila['ruta']}: fs={fs} difiere de {fs_ref}")
        registros.append(Registro(
            ruta=fila["ruta"],
            clases=parsear_categorias(fila["categoria"], clase_nada),
            lugar=fila["lugar"],
            fs=fs,
            muestras=muestras,
            tipo=fila["tipo"],
            datos=datos,
            grupo=fila["ruta"].rsplit(".", 1)[0],
        ))

    clases = sorted({c for r in registros for c in r.clases})
    return Dataset(registros=registros, clases=clases, manifest_filas=filas,
                   manifest_hash=hash_manifest(filas), problemas=problemas)


# ------------------------------- cache .npz -------------------------------

def guardar_cache(ds: Dataset, ruta_npz: str | Path) -> None:
    """Serializa el dataset a un .npz (útil para no releer cientos de JSON en Colab)."""
    Path(ruta_npz).parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps({
        "clases": ds.clases,
        "manifest_hash": ds.manifest_hash,
        "registros": [{k: (list(r.clases) if k == "clases" else getattr(r, k))
                       for k in ("ruta", "clases", "lugar", "fs", "muestras", "tipo", "grupo")}
                      for r in ds.registros],
    })
    arrays = {f"d{i}": r.datos for i, r in enumerate(ds.registros)}
    np.savez_compressed(ruta_npz, meta=np.array(meta), **arrays)


def cargar_cache(ruta_npz: str | Path) -> Dataset:
    z = np.load(ruta_npz, allow_pickle=False)
    meta = json.loads(z["meta"].item())
    registros = []
    for i, rm in enumerate(meta["registros"]):
        registros.append(Registro(
            ruta=rm["ruta"], clases=tuple(rm["clases"]), lugar=rm["lugar"],
            fs=float(rm["fs"]), muestras=int(rm["muestras"]), tipo=rm["tipo"],
            datos=z[f"d{i}"].astype(np.float32), grupo=rm["grupo"],
        ))
    return Dataset(registros=registros, clases=list(meta["clases"]),
                   manifest_hash=meta.get("manifest_hash", ""))
