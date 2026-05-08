from pydantic import BaseModel, EmailStr
from uuid import UUID
from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class UserBase(BaseModel):
    username: str
    full_name: str | None = None
    email: EmailStr
    role: UserRole = UserRole.VIEWER
    is_active: bool = True


class UserCreate(UserBase):
    password: str


class UserUpdate(UserBase):
    password: str | None = None


class UserResponse(UserBase):
    id: UUID
    model_config = {"from_attributes": True}