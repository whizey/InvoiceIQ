from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import CompanySegment


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    email: str
    phone: str | None
    role: str | None
    is_primary: bool


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    industry: str | None
    region: str | None
    city: str | None
    zipcode: str | None
    segment: CompanySegment
    created_at: datetime


class CompanyDetailOut(CompanyOut):
    contacts: list[ContactOut] = []
