from __future__ import annotations

from .ats import AshbySource, GreenhouseSource, LeverSource
from .base import Source
from .generic import GenericSource
from .hosted import KekaSource, OracleSource, ZohoRecruitSource
from .workday import WorkdaySource

REGISTRY: dict[str, Source] = {
    "greenhouse": GreenhouseSource(),
    "lever": LeverSource(),
    "ashby": AshbySource(),
    "workday": WorkdaySource(),
    "keka": KekaSource(),
    "zoho": ZohoRecruitSource(),
    "oracle": OracleSource(),
    "custom": GenericSource(),
}
