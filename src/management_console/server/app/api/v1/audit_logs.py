from app.api.v1 import router
from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from app.database import get_db
from app.models.audit_log import AuditLog
from app.api.deps import is_admin_user
from app.models.user import User
from fastapi import APIRouter
from app.schemas.audit_log import AuditLogResponse


router = APIRouter(prefix="/audit-logs", tags=["Audit Logs"])

@router.get("/", response_model=dict)
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    search: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(is_admin_user)
):
    query = (
        select(AuditLog)
        .order_by(AuditLog.created_at.desc())
    )

    total_query = select(func.count(AuditLog.id))
    if search:
        query = query.where(AuditLog.action.ilike(f"%{search}%"))
        total_query = total_query.where(AuditLog.action.ilike(f"%{search}%"))
        
    total_result = await db.execute(total_query)
    total = total_result.scalar()
    
    # Pagination...
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    logs = result.scalars().all()
    
    return {
        "items": [AuditLogResponse.model_validate(log) for log in logs], 
        "page": page, 
        "page_size": page_size,
        "total": total
    }