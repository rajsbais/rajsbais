from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TargetOwnership(BaseModel):
    company_code_map: dict[str, str] = Field(default_factory=dict, description="source company code -> target company code")
    plant_map: dict[str, str] = Field(default_factory=dict)
    controlling_area_map: dict[str, str] = Field(default_factory=dict)


class ScopeDefinition(BaseModel):
    name: str
    description: str = ""
    scenario_type: Literal["CARVE_OUT", "SDT", "MERGER", "BLUEFIELD"] = "CARVE_OUT"
    carve_out_direction: Literal["FORWARD", "REVERSE"] = "FORWARD"
    source_system_id: str
    target_system_id: str
    company_codes: list[str] = Field(min_length=1)
    plants: list[str] = Field(default_factory=list)
    fiscal_year_from: int | None = None
    fiscal_year_to: int | None = None
    object_types_include: list[str] = Field(default_factory=list)
    object_types_exclude: list[str] = Field(default_factory=lambda: ["BASIS.RfcDestination", "BASIS.IdocPartner", "BASIS.BackgroundJob"])
    document_status: Literal["ALL", "OPEN_ONLY", "CLOSED_ONLY"] = "ALL"
    historical_policy: Literal["FULL", "OPEN_ITEMS_AND_BALANCES", "YEARS"] = "FULL"
    shared_object_policy: Literal["DUPLICATE", "REFERENCE", "EXCLUDE", "MANUAL"] = "DUPLICATE"
    cross_company_policy: Literal["INCLUDE_FLAG", "REFERENCE", "EXCLUDE"] = "INCLUDE_FLAG"
    edge_policies: dict[str, str] = Field(default_factory=dict, description="override traversal policy per edge type")
    type_policies: dict[str, str] = Field(default_factory=dict, description="override traversal policy per object type")
    target_ownership: TargetOwnership = Field(default_factory=TargetOwnership)


CLASSIFICATIONS = (
    "FULLY_TRANSFERRED",
    "PARTIALLY_TRANSFERRED",
    "SHARED_DUPLICATED",
    "RETAINED_BY_SELLER",
    "REFERENCE_ONLY",
    "EXCLUDED",
    "MANUAL_DISPOSITION",
)
