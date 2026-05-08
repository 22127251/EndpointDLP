from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db, Base
from app.models.user import User
from app.schemas.user import UserCreate, UserRole, UserUpdate, UserResponse
from app.api.deps import get_current_user, is_admin_user
from uuid import UUID
from app.utils.security import hash_password
from app.services.audit_log_service import add_audit_log

router = APIRouter(prefix="/users", tags=["Users"], dependencies=[Depends(get_current_user)])


@router.get("/", response_model=list[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_admin: User = Depends(is_admin_user)
):
    
    query = select(User).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    users = result.scalars().all()
    return users


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID, 
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return user


@router.post("/", response_model=UserResponse)
async def create_user(
    data: UserCreate, 
    db: AsyncSession = Depends(get_db)
):
    new_user = User(**data.model_dump(exclude={"password"}), hashed_password=hash_password(data.password))
    db.add(new_user)

    # audit log
    await add_audit_log(
        db=db,
        user_id=None,
        username=None,
        action="create_user",
        target_type="user",
        target_id=str(new_user.id),
        description=f"Created user '{new_user.username}' with ID {new_user.id}"
    )

    await db.commit()
    return new_user


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID, 
    data: UserUpdate, 
    db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user: raise HTTPException(404, "User not found")
    
    update_dict = data.model_dump(exclude_unset=True)
    if "password" in update_dict:
        user.hashed_password = hash_password(update_dict.pop("password"))
    
    for key, value in update_dict.items():
        setattr(user, key, value)
        
    # audit log
    await add_audit_log(
        db=db,
        user_id=None,
        username=None,
        action="update_user",
        target_type="user",
        target_id=str(user.id),
        description=f"Updated user '{user.username}' with ID {user.id}"
    )

    await db.commit()
    return user


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID, 
    db: AsyncSession = Depends(get_db)
):
    user = await db.get(User, user_id)
    if not user: raise HTTPException(404, "User not found")
    
    if user.role == UserRole.ADMIN:
        raise HTTPException(400, "Cannot delete an admin user")

    # audit log
    await add_audit_log(
        db=db,
        user_id=None,
        username=None,
        action="delete_user",
        target_type="user",
        target_id=str(user.id),
        description=f"Deleted user '{user.username}' with ID {user.id}"
    )

    await db.delete(user)
    await db.commit()