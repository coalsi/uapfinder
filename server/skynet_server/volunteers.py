"""Volunteer sign-up ("Host a node" form on the public website).

The endpoint is public and unauthenticated, so the server is deliberately
strict about what it stores and what it reveals:

* only the fields in API.md are kept, after validation; approximate
  coordinates are rounded to 2 decimals (about 1 km) before storage;
* ``website`` is a honeypot: forms rendered for humans leave it empty, so a
  non-empty value is discarded silently but still answered with 201;
* repeat submissions with the same email update the earlier record and get
  the same response, so the endpoint does not reveal who has signed up;
* the public summary aggregates to 1 degree cells and omits cells with fewer
  than ``MIN_CELL_COUNT`` volunteers.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, Iterable, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

MAX_BODY_BYTES = 4096
MIN_CELL_COUNT = 3
RATE_WINDOW_S = 3600.0

# ISO 3166-1 alpha-2, officially assigned codes (Debian iso-codes 4.x).
COUNTRIES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC
CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH
KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW
MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC
SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY
UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())

# Pragmatic address check (no network lookups): one "@", a dot-atom local
# part and a domain of LDH labels with an alphabetic TLD.
_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

FIELDS = ("email", "name", "country", "region", "city", "postal_code", "approx_lat", "approx_lon", "site_type",
          "has_hardware", "interests", "notes", "consent")


def _blank_to_none(v: Any) -> Any:
    return None if isinstance(v, str) and not v.strip() else v


class VolunteerIn(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    email: str = Field(max_length=254)
    name: Optional[str] = Field(default=None, max_length=100)
    country: str
    region: Optional[str] = Field(default=None, max_length=100)
    city: Optional[str] = Field(default=None, max_length=100)
    postal_code: Optional[str] = Field(default=None, max_length=20)
    approx_lat: Optional[float] = Field(default=None, ge=-90.0, le=90.0, allow_inf_nan=False)
    approx_lon: Optional[float] = Field(default=None, ge=-180.0, le=180.0, allow_inf_nan=False)
    site_type: Optional[Literal["rooftop", "yard", "rural_land", "other"]] = None
    has_hardware: Optional[Literal["none", "some", "full"]] = None
    interests: List[Literal["host_node", "software", "data_analysis", "outreach"]] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None, max_length=1000)
    consent: StrictBool
    website: Optional[str] = None

    _blanks = field_validator("name", "region", "city", "postal_code", "notes", "site_type", "has_hardware",
                              "approx_lat", "approx_lon", mode="before")(_blank_to_none)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        local, sep, domain = v.rpartition("@")
        labels = domain.split(".")
        if (not sep or not local or len(local) > 64 or not _LOCAL_RE.match(local) or len(labels) < 2
                or not all(_LABEL_RE.match(label) for label in labels) or not labels[-1].isalpha()
                or len(labels[-1]) < 2):
            raise ValueError("not a valid email address")
        # Addresses are matched case-insensitively for de-duplication.
        return f"{local}@{domain}".lower()

    @field_validator("country", mode="before")
    @classmethod
    def _country(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        code = v.strip().upper()
        code = "GB" if code == "UK" else code
        if code not in COUNTRIES:
            raise ValueError("country must be an ISO 3166-1 alpha-2 code")
        return code

    @field_validator("consent")
    @classmethod
    def _consented(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("consent must be true")
        return v

    @field_validator("interests")
    @classmethod
    def _dedupe(cls, v: List[str]) -> List[str]:
        return sorted(set(v))

    def record(self) -> Dict[str, Any]:
        """The fields to store, with coordinates rounded to about 1 km."""
        out = {name: getattr(self, name) for name in FIELDS}
        for key in ("approx_lat", "approx_lon"):
            if out[key] is not None:
                out[key] = round(out[key], 2)
        return out


def is_honeypot(body: Any) -> bool:
    """True for submissions that filled in the hidden ``website`` field."""
    if not isinstance(body, dict):
        return False
    value = body.get("website")
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


_CSV_COLUMNS = ("id",) + FIELDS + ("created_at", "updated_at")


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ";".join(map(str, v))
    if hasattr(v, "isoformat"):
        return v.isoformat()
    text = str(v)
    # Spreadsheet formula injection: free text from the public must not start
    # a formula when an admin opens the export.
    if isinstance(v, str) and text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def to_csv(rows: Iterable[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(row.get(c)) for c in _CSV_COLUMNS])
    return buf.getvalue()


def to_json_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "updated_at"):
        if hasattr(out.get(key), "isoformat"):
            out[key] = out[key].isoformat()
    return out
