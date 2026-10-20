"""The bundled bright-star catalog (HYG v4.0 subset, V <= 6.5; see data/NOTICE.md)."""

from __future__ import annotations

import gzip
import io
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import numpy as np

CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bright_stars.csv.gz")


@dataclass(frozen=True)
class StarCatalog:
    hip: np.ndarray
    hr: np.ndarray
    ra_deg: np.ndarray
    dec_deg: np.ndarray
    pmra_masyr: np.ndarray  # mu_alpha * cos(dec)
    pmdec_masyr: np.ndarray
    vmag: np.ndarray

    def __len__(self) -> int:
        return int(self.vmag.size)

    def brighter_than(self, mag: float) -> "StarCatalog":
        m = self.vmag <= mag
        return StarCatalog(*(getattr(self, f)[m] for f in self.__dataclass_fields__))


@lru_cache(maxsize=4)
def load_catalog(path: Optional[str] = None) -> StarCatalog:
    with gzip.open(path or CATALOG_PATH, "rb") as f:
        text = f.read()
    a = np.loadtxt(io.BytesIO(text), delimiter=",", comments="#", dtype=np.float64, ndmin=2)
    return StarCatalog(a[:, 0].astype(np.int64), a[:, 1].astype(np.int64), a[:, 2], a[:, 3], a[:, 4], a[:, 5], a[:, 6])
