from fastapi import APIRouter, WebSocket

router = APIRouter()


@router.websocket("/ws/echo")
async def ws_echo(websocket: WebSocket):
    await websocket.accept()
    data = await websocket.receive_text()
    await websocket.send_text(data)
    await websocket.close()
