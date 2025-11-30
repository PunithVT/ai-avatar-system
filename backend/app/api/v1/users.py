from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def list_users():
    """List users"""
    return {"message": "Users endpoint"}


@router.get("/{user_id}")
async def get_user(user_id: str):
    """Get user by ID"""
    return {"user_id": user_id}
