"""
todos/controller.py — Doctor Todo HTTP Endpoints
================================================

All routes nested under /api/v1/cases/{case_id}/todos (set in src/api.py).

ROUTES
------
POST   /       → Doctor creates a todo (201)
GET    /       → Patient or doctor lists todos (200)
PATCH  /{id}   → Doctor updates a todo (200)
DELETE /{id}   → Doctor deletes a todo (204)
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_doctor
from src.database.core import get_async_session
from src.models.user import User
from src.todos import service
from src.todos.schemas import (
    TodoCreateRequest,
    TodoListResponse,
    TodoResponse,
    TodoUpdateRequest,
)

router = APIRouter()


@router.post(
    "",
    response_model=TodoResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a clinical todo for a case (doctor only)",
)
async def create_todo(
    case_id: str,
    request: TodoCreateRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> TodoResponse:
    """
    Doctor creates a follow-up task for the case.
    Doctor must be assigned to the case.
    """
    return await service.create_todo(db, doctor, case_id, request)


@router.get(
    "",
    response_model=TodoListResponse,
    status_code=status.HTTP_200_OK,
    summary="List todos for a case",
)
async def list_todos(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> TodoListResponse:
    """
    List all todos for a case.
    Accessible to the patient who owns the case or the assigned doctor.
    """
    return await service.list_todos(db, user, case_id)


@router.patch(
    "/{todo_id}",
    response_model=TodoResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a todo (doctor only)",
)
async def update_todo(
    case_id: str,
    todo_id: str,
    request: TodoUpdateRequest,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> TodoResponse:
    """
    Partial update — send only the fields to change.
    Setting is_completed=true records completed_at.
    Setting is_completed=false clears completed_at.
    """
    return await service.update_todo(db, doctor, case_id, todo_id, request)


@router.delete(
    "/{todo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a todo (doctor only)",
)
async def delete_todo(
    case_id: str,
    todo_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """Delete a todo. Only the creating doctor can delete their own todos."""
    await service.delete_todo(db, doctor, case_id, todo_id)
