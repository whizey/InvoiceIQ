from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.models import Company
from app.schemas.company import CompanyDetailOut, CompanyOut

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanyOut])
async def list_companies(db: AsyncSession = Depends(get_db)) -> list[Company]:
    result = await db.execute(select(Company).order_by(Company.name))
    return list(result.scalars().all())


@router.get("/{company_id}", response_model=CompanyDetailOut)
async def get_company(company_id: UUID, db: AsyncSession = Depends(get_db)) -> Company:
    stmt = (
        select(Company)
        .where(Company.id == company_id)
        .options(selectinload(Company.contacts))
    )
    result = await db.execute(stmt)
    company = result.scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company
