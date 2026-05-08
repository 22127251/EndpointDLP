from sqlalchemy.ext.asyncio import AsyncSession
from app.models.audit_log import AuditLog
from fastapi import Request

async def add_audit_log(
    db: AsyncSession,
    user_id: str,
    username: str,
    action: str,
    target_type: str,
    target_id: str = None,
    description: str = ""
):

    
    new_log = AuditLog(
        user_id=user_id,
        username=username,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
        description=description,
    )
    
    db.add(new_log)