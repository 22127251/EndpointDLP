
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from sqlalchemy.orm.attributes import flag_modified
from app.models.setting import ServerConfiguration
from app.schemas.setting import ServerConfigurationUpdate

router = APIRouter(prefix="/settings", tags=["Settings"])

@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.get(ServerConfiguration, 1)
    return result.settings

@router.patch("/settings")
async def update_settings(
    data: ServerConfigurationUpdate,
    db: AsyncSession = Depends(get_db)

):
    config = await db.get(ServerConfiguration, 1)
    update_data = data.settings

    if not config:
        config = ServerConfiguration(id=1, settings=update_data)
        db.add(config)
    else:
        current_settings = dict(config.settings) if config.settings else {}
        
        current_settings.update(update_data)
        
        config.settings = current_settings
        
        flag_modified(config, "settings")

    await db.commit()
    await db.refresh(config)
    return config.settings