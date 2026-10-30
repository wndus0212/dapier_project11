"""Forward only towel viewer requests; the server holds the API token."""
import os
import json
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter()


def _forward(path, data=None):
    token = os.getenv('TOWEL_API_TOKEN', '')
    base = os.getenv('TOWEL_API_URL', '').rstrip('/')
    if not token or not base:
        raise HTTPException(503, 'TOWEL_API_URL 및 TOWEL_API_TOKEN 설정 필요')
    request = urllib.request.Request(base + path, data=data,
                                     headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return Response(response.read(), status_code=response.status, media_type='application/json',
                            headers={'Cache-Control': 'no-store'})
    except urllib.error.HTTPError as error:
        return Response(error.read(), status_code=error.code, media_type='application/json')
    except (OSError, ValueError):
        raise HTTPException(503, '수건 API에 연결할 수 없습니다')


@router.get('/api/towel/viewer')
def get_viewer():
    return _forward('/station/viewer')


@router.post('/api/towel/reference')
async def save_reference(request: Request):
    origin = request.headers.get('origin')
    if not origin or origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '동일 웹 화면에서만 기준을 저장할 수 있습니다')
    body = await request.json()
    if body != {'confirm': True}:
        raise HTTPException(400, '기준 등록 확인이 필요합니다')
    return _forward('/station/reference', b'{"confirm":true}')


@router.post('/api/towel/tolerances')
async def save_tolerances(request: Request):
    origin = request.headers.get('origin')
    if not origin or origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '동일 웹 화면에서만 허용 오차를 저장할 수 있습니다')
    body = await request.json()
    if not isinstance(body, dict) or body.get('confirm') is not True:
        raise HTTPException(400, '측정값 확인이 필요합니다')
    return _forward('/station/tolerances', json.dumps(body, allow_nan=False).encode())
