from fastapi import APIRouter, Depends
from fastapi.responses import Response
import json

from src.config.database import db_session
from src.utils.security import current, writer
from src.schemas.search import SearchInput
from src.services import searches as service

router = APIRouter()


@router.get('/searches')
def listing(db=Depends(db_session), user=Depends(current)):
    return service.listing(db)


@router.post('/searches', status_code=201)
def create(data: SearchInput, db=Depends(db_session), user=Depends(writer)):
    return service.create_task(db, data, user)


@router.get('/searches/{task_id}')
def detail(task_id: int, db=Depends(db_session), user=Depends(current)):
    return service.detail(db, task_id)


@router.post('/searches/{task_id}/resume', status_code=202)
def resume(task_id: int, db=Depends(db_session), user=Depends(writer)):
    return service.resume(db, task_id, user)


@router.get('/searches/{task_id}/export')
def export(task_id: int, db=Depends(db_session), user=Depends(current)):
    report = service.export(db, task_id)
    return Response(
        json.dumps(report, ensure_ascii=False, indent=2),
        media_type='application/json',
        headers={'Content-Disposition': f'attachment; filename="search-{task_id}.json"'},
    )
