from fastapi import APIRouter, Depends
from app.schemas.policy import RuleType, PolicyAction, PolicyChanel
from app.models.agent import AgentStatus
from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter(prefix="/metadata", tags=["Metadata"])

@router.get("/constants", response_model=dict)
async def get_constants(
    current_user: User = Depends(get_current_user),
):
    return {
        "rule_types": [e.value for e in RuleType],
        "policy_actions": [e.value for e in PolicyAction],
        "policy_channels": [e.value for e in PolicyChanel],
        "agent_statuses": [e.value for e in AgentStatus],
    }