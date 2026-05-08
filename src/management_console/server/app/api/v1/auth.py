from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from app.database import get_db
from app.models.user import User
from app.utils.security import verify_password, create_access_token
from app.services.audit_log_service import add_audit_log

router = APIRouter(prefix="/auth", tags=["Authentication"])



class LoginRequest(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: str
    username: str
    full_name: str
    role: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_info: UserResponse



@router.post("/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(User).where(User.username == request.username))
    user = result.scalar_one_or_none()

    if (not user or
        not user.is_active or
        not verify_password(request.password, user.hashed_password) 
        ):

        # audit log for failed login attempt
        await add_audit_log(
            db=db,
            user_id=None,
            username=request.username,
            action="login",
            target_type="user",
            target_id=None,
            description="Failed login attempt"
        )
        await db.commit()

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    access_token = create_access_token(
        data={
            "sub": str(user.id), 
            "role": user.role
        }
    )

    # audit log for successful login
    await add_audit_log(
        db=db,
        user_id=user.id,
        username=user.username,
        action="login",
        target_type="user",
        target_id=str(user.id),
        description="Successful login"
    )
    await db.commit()
    return TokenResponse(access_token=access_token, user_info=UserResponse(id=str(user.id), username=user.username, full_name=user.full_name, role=user.role))

