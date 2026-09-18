import asyncio
import io
import json
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import mss
from PIL import Image

try:
    import pyautogui
    pyautogui.FAILSAFE = False
except ImportError:
    pass

app = FastAPI()

TEMPLATES_DIR = Path(__file__).parent / "templates"

@app.get("/")
async def get():
    remote_html = TEMPLATES_DIR / "remote.html"
    if not remote_html.exists():
        return HTMLResponse("<html><body><h1>remote.html not found</h1></body></html>", status_code=404)
    with open(remote_html, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # Task to read commands from the client
    async def listen_to_client():
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    cmd = json.loads(data)
                    if cmd.get("type") == "click":
                        x_pct = cmd.get("x", 0.5)
                        y_pct = cmd.get("y", 0.5)
                        
                        screen_w, screen_h = pyautogui.size()
                        target_x = int(screen_w * x_pct)
                        target_y = int(screen_h * y_pct)
                        
                        # Use pyautogui to click
                        pyautogui.click(target_x, target_y)
                        
                    elif cmd.get("type") == "type":
                        text = cmd.get("text", "")
                        if text:
                            # If it's a special key like Enter, handle it
                            if text == "Enter":
                                pyautogui.press('enter')
                            elif text == "Backspace":
                                pyautogui.press('backspace')
                            else:
                                pyautogui.write(text)
                except Exception as e:
                    logging.error(f"Error executing command: {e}")
        except WebSocketDisconnect:
            pass

    listen_task = asyncio.create_task(listen_to_client())
    
    try:
        with mss.mss() as sct:
            # We capture the first monitor
            monitor = sct.monitors[1]
            while True:
                # Capture screen
                sct_img = sct.grab(monitor)
                # Convert to PIL Image to encode as JPEG
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                
                # Downscale slightly for performance if needed
                # img.thumbnail((1280, 720))
                
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=60)
                image_bytes = buffer.getvalue()
                
                # Send binary frame over websocket
                await websocket.send_bytes(image_bytes)
                
                # Send at roughly 15 FPS
                await asyncio.sleep(1/15)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.error(f"Stream error: {e}")
    finally:
        listen_task.cancel()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
