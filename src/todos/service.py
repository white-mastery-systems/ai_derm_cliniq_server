"""
todos/service.py — Doctor Todo Business Logic
==============================================

Four operations:
1. create_todo  — doctor creates a clinical action item for a case
2. list_todos   — patient or assigned doctor reads the todo list
3. update_todo  — doctor updates title/description/due_date/completion
4. delete_todo  — doctor removes a todo

ACCESS RULES
------------
Create / Update / Delete: doctor only, must be assigned to the case
List: patient who owns the case, OR the assigned doctor

COMPLETION LOGIC
----------------
When is_completed transitions to True:
- completed_at is set to UTC now
When is_completed goes back to False (un-complete):
- completed_at is cleared to None
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.exceptions import (
    CaseNotFoundException,
    ForbiddenException,
    NotFoundException,
)
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import Case
from src.models.todo import Todo
from src.models.user import User, UserRole
from src.todos.schemas import (
    TodoCreateRequest,
    TodoListResponse,
    TodoResponse,
    TodoUpdateRequest,
)

logger = get_logger(__name__)


def _to_response(todo: Todo) -> TodoResponse:
    return TodoResponse(
        id=todo.id,
        case_id=todo.case_id,
        doctor_id=todo.doctor_id,
        title=todo.title,
        description=todo.description,
        due_date=todo.due_date,
        is_completed=todo.is_completed,
        completed_at=todo.completed_at,
        created_at=todo.created_at,
        updated_at=todo.updated_at,
    )


async def _get_case_for_doctor(db: AsyncSession, doctor: User, case_id: str) -> Case:
    """Load case and verify doctor is assigned. Returns case or raises."""
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()

    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if case.doctor_id != doctor.id:
        raise ForbiddenException(
            message="You are not assigned to this case."
        )
    return case


async def create_todo(
    db: AsyncSession,
    doctor: User,
    case_id: str,
    request: TodoCreateRequest,
) -> TodoResponse:
    """Doctor creates a new todo for an assigned case."""
    if doctor.role != UserRole.DOCTOR:
        raise ForbiddenException(message="Only doctors can create todos")

    await _get_case_for_doctor(db, doctor, case_id)

    todo = Todo(
        id=new_uuid(),
        case_id=case_id,
        doctor_id=doctor.id,
        title=request.title,
        description=request.description,
        due_date=request.due_date,
        is_completed=False,
    )
    db.add(todo)
    await db.flush()
    await db.refresh(todo)

    logger.info("todo_created", case_id=case_id, todo_id=todo.id)
    return _to_response(todo)


async def list_todos(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> TodoListResponse:
    """List all todos for a case. Accessible to patient (owner) or assigned doctor."""
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()

    if case is None:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if user.role == UserRole.PATIENT and case.patient_id != user.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")

    if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
        raise ForbiddenException(message="You are not assigned to this case.")

    result = await db.execute(
        select(Todo)
        .where(Todo.case_id == case_id)
        .order_by(Todo.created_at)
    )
    todos = list(result.scalars().all())

    return TodoListResponse(
        case_id=case_id,
        total=len(todos),
        items=[_to_response(t) for t in todos],
    )


async def update_todo(
    db: AsyncSession,
    doctor: User,
    case_id: str,
    todo_id: str,
    request: TodoUpdateRequest,
) -> TodoResponse:
    """Doctor updates a todo — partial update, any combination of fields."""
    if doctor.role != UserRole.DOCTOR:
        raise ForbiddenException(message="Only doctors can update todos")

    await _get_case_for_doctor(db, doctor, case_id)

    result = await db.execute(
        select(Todo).where(Todo.id == todo_id, Todo.case_id == case_id)
    )
    todo = result.scalar_one_or_none()

    if todo is None:
        raise NotFoundException(message=f"No todo found with id: {todo_id}")

    if todo.doctor_id != doctor.id:
        raise ForbiddenException(message="You can only update your own todos.")

    if request.title is not None:
        todo.title = request.title
    if request.description is not None:
        todo.description = request.description
    if request.due_date is not None:
        todo.due_date = request.due_date

    if request.is_completed is not None:
        todo.is_completed = request.is_completed
        if request.is_completed and todo.completed_at is None:
            todo.completed_at = datetime.now(tz=timezone.utc)
        elif not request.is_completed:
            todo.completed_at = None

    logger.info("todo_updated", todo_id=todo_id, is_completed=todo.is_completed)
    return _to_response(todo)


async def delete_todo(
    db: AsyncSession,
    doctor: User,
    case_id: str,
    todo_id: str,
) -> None:
    """Doctor deletes one of their todos."""
    if doctor.role != UserRole.DOCTOR:
        raise ForbiddenException(message="Only doctors can delete todos")

    await _get_case_for_doctor(db, doctor, case_id)

    result = await db.execute(
        select(Todo).where(Todo.id == todo_id, Todo.case_id == case_id)
    )
    todo = result.scalar_one_or_none()

    if todo is None:
        raise NotFoundException(message=f"No todo found with id: {todo_id}")

    if todo.doctor_id != doctor.id:
        raise ForbiddenException(message="You can only delete your own todos.")

    await db.delete(todo)
    logger.info("todo_deleted", todo_id=todo_id, case_id=case_id)
