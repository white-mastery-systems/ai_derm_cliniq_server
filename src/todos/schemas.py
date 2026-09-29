"""
todos/schemas.py — Doctor Todo Request & Response Models
=========================================================

FOUR ENDPOINTS
--------------
POST   /api/v1/cases/{case_id}/todos              → Doctor creates a todo
GET    /api/v1/cases/{case_id}/todos              → List todos (patient or doctor)
PATCH  /api/v1/cases/{case_id}/todos/{todo_id}    → Doctor updates a todo
DELETE /api/v1/cases/{case_id}/todos/{todo_id}    → Doctor deletes a todo

TODO LIFECYCLE
--------------
1. Doctor creates todo after reviewing case (POST)
2. Doctor optionally sets a due_date
3. Doctor marks todo complete via PATCH with is_completed=true
4. completed_at is recorded automatically on completion
5. Patient can read todos to see doctor's follow-up plan
"""

from datetime import datetime

from pydantic import BaseModel, Field


class TodoCreateRequest(BaseModel):
    """POST body — create a new clinical todo for a case."""
    title: str = Field(min_length=3, max_length=500)
    description: str | None = None
    due_date: datetime | None = None


class TodoUpdateRequest(BaseModel):
    """PATCH body — update any combination of fields."""
    title: str | None = Field(default=None, min_length=3, max_length=500)
    description: str | None = None
    due_date: datetime | None = None
    is_completed: bool | None = None


class TodoResponse(BaseModel):
    """Returned for create, update, and individual items in list."""
    id: str
    case_id: str
    doctor_id: str
    title: str
    description: str | None
    due_date: datetime | None
    is_completed: bool
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TodoListResponse(BaseModel):
    """Returned by GET /todos — all todos for a case."""
    case_id: str
    total: int
    items: list[TodoResponse]
