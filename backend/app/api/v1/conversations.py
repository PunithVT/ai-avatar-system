from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def list_conversations():
    """List conversations"""
    return {"message": "Conversations endpoint"}


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str):
    """Get conversation by ID"""
    return {"conversation_id": conversation_id}
