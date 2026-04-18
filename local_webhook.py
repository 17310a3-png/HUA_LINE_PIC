"""
LINE 貼圖本機 Webhook Listener (streaming + Telegram 審核版)

啟動方式：
  pip install fastapi uvicorn httpx
  python local_webhook.py [--port 5000]

環境變數 / config.json：
  TG_BOT_TOKEN   Telegram bot token
  TG_CHAT_ID     Telegram chat id
  PUBLIC_URL     對外公開的 URL（ngrok 或正式 domain），用來產生審核頁連結

端點：
  POST /sheet              N8N 每張原稿生完就 POST 過來（streaming，避免 OOM）
  GET  /preview/<name>     瀏覽器開這個網址審核 6 張原稿
  POST /finalize/<name>    審核通過 → 呼叫 make_stickers.py 裁切 + 打包
  POST /reject/<name>      審核退回 → 清除狀態
  GET  /image/<name>/<n>   取得單張原稿（給 preview 頁用）
  POST /process            （舊端點，向下相容，一次收全部再處理）
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

# ============================================================
# 設定
# ============================================================

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(BASE_DIR))
GEMINI_DIR = DATA_DIR / "gemini"
STATE_DIR = DATA_DIR / ".state"
ZIP_PARENT = DATA_DIR / "zip儲存區"
OUT_PARENT = DATA_DIR / "完成圖檔區"
MAKE_STICKERS = BASE_DIR / "make_stickers.py"
CONFIG_FILE = BASE_DIR / "config.json"

def load_config():
    cfg = {
        "TG_BOT_TOKEN": os.environ.get("TG_BOT_TOKEN", ""),
        "TG_CHAT_ID": os.environ.get("TG_CHAT_ID", ""),
        "PUBLIC_URL": os.environ.get("PUBLIC_URL", ""),
    }
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k in cfg:
                if data.get(k):
                    cfg[k] = str(data[k])
        except Exception as e:
            print(f"  警告：讀取 config.json 失敗：{e}")
    return cfg

CONFIG = load_config()

app = FastAPI(title="LINE 貼圖本機處理服務")

# ============================================================
# Telegram
# ============================================================

def send_telegram(text: str, disable_web_page_preview: bool = False):
    token = CONFIG.get("TG_BOT_TOKEN")
    chat_id = CONFIG.get("TG_CHAT_ID")
    if not token or not chat_id:
        print(f"  [Telegram 略過]（缺 TG_BOT_TOKEN 或 TG_CHAT_ID）：{text}")
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": int(chat_id),
                "text": text,
                "disable_web_page_preview": disable_web_page_preview,
            },
            timeout=15,
        )
        ok = r.json().get("ok", False)
        if not ok:
            print(f"  Telegram 發送失敗：{r.text}")
        else:
            print(f"  Telegram 已送出")
        return ok
    except Exception as e:
        print(f"  Telegram 發送例外：{e}")
        return False

# ============================================================
# 狀態管理（per set_name）
# ============================================================

def state_path(set_name: str) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"{set_name}.json"

def load_state(set_name: str) -> Optional[dict]:
    p = state_path(set_name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def save_state(set_name: str, state: dict):
    state_path(set_name).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# ============================================================
# 資料模型
# ============================================================

class SheetUpload(BaseModel):
    set_name: str
    sheet_number: int
    total_sheets: int
    total: int = 40
    image_b64: str
    main_src: Optional[List[int]] = None
    tab_src: Optional[List[int]] = None
    model: Optional[str] = None

class SheetData(BaseModel):
    number: int
    image_b64: str

class ProcessRequest(BaseModel):
    set_name: str
    total: int = 40
    sheets: List[SheetData]
    main_src: List[int] = [5, 0]
    tab_src: List[int] = [5, 1]
    callback_url: Optional[str] = None

# ============================================================
# 核心：裁切 + Telegram 完成通知
# ============================================================

def run_make_stickers(set_name: str, total: int, sheet_filenames: List[str],
                      main_src: List[int], tab_src: List[int]) -> dict:
    sheets_arg = ",".join(sheet_filenames)
    main_arg = f"{main_src[0]},{main_src[1]}"
    tab_arg = f"{tab_src[0]},{tab_src[1]}"

    cmd = [
        sys.executable, str(MAKE_STICKERS),
        "--name", set_name,
        "--total", str(total),
        "--sheets", sheets_arg,
        "--main", main_arg,
        "--tab", tab_arg,
    ]
    print(f"\n=== 執行裁切：{' '.join(cmd)} ===")

    env = os.environ.copy()
    env["DATA_DIR"] = str(DATA_DIR)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE_DIR), env=env)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail={
            "error": "make_stickers.py 執行失敗",
            "stderr": result.stderr,
            "stdout": result.stdout,
        })

    print(result.stdout)
    out_dir = OUT_PARENT / set_name
    zip_path = ZIP_PARENT / f"{set_name}.zip"
    files = sorted(f.name for f in out_dir.glob("*.png")) if out_dir.exists() else []

    return {
        "status": "success",
        "set_name": set_name,
        "total": total,
        "output_dir": str(out_dir),
        "zip_path": str(zip_path),
        "zip_exists": zip_path.exists(),
        "files": files,
        "stdout": result.stdout,
    }

# ============================================================
# 端點
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "telegram_configured": bool(CONFIG.get("TG_BOT_TOKEN") and CONFIG.get("TG_CHAT_ID")),
        "public_url": CONFIG.get("PUBLIC_URL") or "(未設定)",
    }

@app.post("/sheet")
def receive_sheet(req: SheetUpload):
    """N8N 每張原稿生完就 POST 一次，最後一張收齊時發 Telegram 審核通知"""
    GEMINI_DIR.mkdir(exist_ok=True)

    # 第 1 張來時重置 state（避免舊資料污染）
    state = load_state(req.set_name)
    if req.sheet_number == 1 or state is None:
        state = {
            "set_name": req.set_name,
            "total_sheets": req.total_sheets,
            "total": req.total,
            "main_src": req.main_src or [req.total_sheets - 1, 0],
            "tab_src": req.tab_src or [req.total_sheets - 1, 1],
            "model": req.model or "",
            "sheets_received": [],
            "sheet_filenames": {},
            "status": "receiving",
            "created_at": time.time(),
        }

    filename = f"{req.set_name}_sheet{req.sheet_number}.png"
    filepath = GEMINI_DIR / filename
    filepath.write_bytes(base64.b64decode(req.image_b64))
    print(f"  收到 {req.set_name} sheet {req.sheet_number}/{req.total_sheets}（{filepath.stat().st_size} bytes）")

    if req.sheet_number not in state["sheets_received"]:
        state["sheets_received"].append(req.sheet_number)
    state["sheet_filenames"][str(req.sheet_number)] = filename

    if len(state["sheets_received"]) >= state["total_sheets"]:
        state["status"] = "awaiting_review"
        save_state(req.set_name, state)

        public = CONFIG.get("PUBLIC_URL") or "http://localhost:5000"
        preview_url = f"{public}/preview/{req.set_name}"
        send_telegram(
            f"貼圖「{req.set_name}」生圖完成（共 {state['total_sheets']} 張原稿）\n\n"
            f"開啟審核頁：\n{preview_url}"
        )
    else:
        save_state(req.set_name, state)

    return {
        "status": "ok",
        "received": len(state["sheets_received"]),
        "total_sheets": state["total_sheets"],
        "all_received": state["status"] == "awaiting_review",
    }

@app.get("/image/{set_name}/{sheet_number}")
def get_image(set_name: str, sheet_number: int):
    state = load_state(set_name)
    if not state:
        raise HTTPException(404, "Set not found")
    filename = state["sheet_filenames"].get(str(sheet_number))
    if not filename:
        raise HTTPException(404, f"Sheet {sheet_number} not found")
    path = GEMINI_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(path, media_type="image/png")

@app.get("/preview/{set_name}", response_class=HTMLResponse)
def preview(set_name: str):
    state = load_state(set_name)
    if not state:
        raise HTTPException(404, "Set not found")

    sheets_html = ""
    for n in sorted(state["sheets_received"]):
        sheets_html += f"""
        <div class="sheet">
          <div class="sheet-title">Sheet {n}</div>
          <img src="/image/{set_name}/{n}" alt="sheet {n}" loading="lazy">
        </div>
        """

    status = state.get("status", "receiving")
    status_label = {
        "receiving": "收圖中",
        "awaiting_review": "待審核",
        "finalized": "已完成",
    }.get(status, status)

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{set_name} 原稿審核</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
         margin: 0; padding: 20px; background: #111; color: #eee; }}
  h1 {{ margin: 0 0 8px; font-size: 22px; }}
  .meta {{ color: #999; font-size: 14px; margin-bottom: 16px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }}
  .sheet {{ background: #1c1c1c; border-radius: 8px; overflow: hidden; padding: 8px; }}
  .sheet-title {{ font-size: 13px; color: #aaa; margin-bottom: 6px; }}
  .sheet img {{ width: 100%; display: block; border-radius: 4px; background: #00ff00; }}
  .actions {{ position: sticky; bottom: 0; padding: 16px 0 0;
             background: linear-gradient(to bottom, transparent, #111 40%); margin-top: 16px; }}
  button {{ font-size: 16px; padding: 14px 24px; border-radius: 8px; border: none;
            cursor: pointer; margin-right: 10px; font-weight: 600; }}
  .confirm {{ background: #27c36a; color: white; }}
  .reject {{ background: #555; color: white; }}
  .confirm:disabled {{ background: #666; cursor: not-allowed; }}
  #status {{ margin-top: 14px; padding: 12px; border-radius: 6px; background: #222; display: none; }}
  #status.show {{ display: block; }}
  #status.ok {{ background: #0f3; color: #000; }}
  #status.err {{ background: #c33; color: white; }}
</style>
</head>
<body>
  <h1>{set_name}</h1>
  <div class="meta">狀態：{status_label}　/　原稿：{len(state['sheets_received'])}/{state['total_sheets']}　/　最終貼圖：{state['total']} 張　/　模型：{state.get('model') or '未知'}</div>

  <div class="grid">{sheets_html}</div>

  <div class="actions">
    <button class="confirm" id="confirmBtn" onclick="confirmCrop()">確認裁切 + 打包</button>
    <button class="reject" onclick="rejectSet()">退回（清除這套）</button>
    <div id="status"></div>
  </div>

<script>
const setName = {json.dumps(set_name)};
const statusBox = document.getElementById('status');
const btn = document.getElementById('confirmBtn');

function show(msg, cls) {{
  statusBox.className = 'show ' + (cls || '');
  statusBox.textContent = msg;
}}

async function confirmCrop() {{
  btn.disabled = true;
  show('裁切中… 40 張約需 30 秒');
  try {{
    const r = await fetch(`/finalize/${{encodeURIComponent(setName)}}`, {{ method: 'POST' }});
    const data = await r.json();
    if (r.ok) {{
      show(`完成！ZIP: ${{data.zip_path}}　共 ${{data.files.length}} 個檔案`, 'ok');
    }} else {{
      show('失敗：' + JSON.stringify(data), 'err');
      btn.disabled = false;
    }}
  }} catch (e) {{
    show('錯誤：' + e.message, 'err');
    btn.disabled = false;
  }}
}}

async function rejectSet() {{
  if (!confirm('確定要退回？會清除這套的原稿與狀態。')) return;
  try {{
    const r = await fetch(`/reject/${{encodeURIComponent(setName)}}`, {{ method: 'POST' }});
    const data = await r.json();
    show('已退回：' + JSON.stringify(data));
  }} catch (e) {{
    show('錯誤：' + e.message, 'err');
  }}
}}
</script>
</body>
</html>"""
    return HTMLResponse(html)

@app.post("/finalize/{set_name}")
def finalize(set_name: str):
    state = load_state(set_name)
    if not state:
        raise HTTPException(404, "Set not found")
    if len(state["sheets_received"]) < state["total_sheets"]:
        raise HTTPException(400, f"尚未收齊原稿（{len(state['sheets_received'])}/{state['total_sheets']}）")

    sheet_filenames = [
        state["sheet_filenames"][str(n)]
        for n in sorted(state["sheets_received"])
    ]

    response = run_make_stickers(
        set_name=set_name,
        total=state["total"],
        sheet_filenames=sheet_filenames,
        main_src=state["main_src"],
        tab_src=state["tab_src"],
    )

    state["status"] = "finalized"
    state["finalized_at"] = time.time()
    save_state(set_name, state)

    public = CONFIG.get("PUBLIC_URL") or "http://localhost:5000"
    download_url = f"{public}/download/{set_name}.zip"
    send_telegram(
        f"「{set_name}」製作完成！\n"
        f"共 {response['total']} 張貼圖 + main + tab\n\n"
        f"下載 ZIP：\n{download_url}"
    )
    response["download_url"] = download_url
    return JSONResponse(response)

@app.get("/download/{set_name}.zip")
def download_zip(set_name: str):
    zip_path = ZIP_PARENT / f"{set_name}.zip"
    if not zip_path.exists():
        raise HTTPException(404, "ZIP 不存在（可能還沒裁切完成）")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{set_name}.zip")

@app.post("/reject/{set_name}")
def reject(set_name: str):
    state = load_state(set_name)
    if not state:
        raise HTTPException(404, "Set not found")
    for fname in state.get("sheet_filenames", {}).values():
        p = GEMINI_DIR / fname
        if p.exists():
            p.unlink()
    state_path(set_name).unlink(missing_ok=True)
    send_telegram(f"「{set_name}」已退回，原稿已清除。請重新生圖。")
    return {"status": "rejected", "set_name": set_name}

# ============================================================
# 舊端點（向下相容）
# ============================================================

@app.post("/process")
def process_stickers(req: ProcessRequest):
    GEMINI_DIR.mkdir(exist_ok=True)
    sheet_filenames = []
    for sheet in sorted(req.sheets, key=lambda s: s.number):
        filename = f"{req.set_name}_sheet{sheet.number}.png"
        filepath = GEMINI_DIR / filename
        filepath.write_bytes(base64.b64decode(sheet.image_b64))
        sheet_filenames.append(filename)
        print(f"  儲存原稿：{filename}")

    response = run_make_stickers(
        set_name=req.set_name,
        total=req.total,
        sheet_filenames=sheet_filenames,
        main_src=req.main_src,
        tab_src=req.tab_src,
    )

    if req.callback_url:
        try:
            httpx.post(req.callback_url, json=response, timeout=10)
        except Exception as e:
            print(f"  回報 N8N 失敗：{e}")

    return JSONResponse(response)

# ============================================================
# 啟動
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LINE 貼圖本機 Webhook Listener")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    print(f"=== LINE 貼圖本機處理服務 ===")
    print(f"  監聽：http://{args.host}:{args.port}")
    print(f"  PUBLIC_URL：{CONFIG.get('PUBLIC_URL') or '(未設定，審核連結會用 localhost)'}")
    print(f"  Telegram：{'已設定' if (CONFIG.get('TG_BOT_TOKEN') and CONFIG.get('TG_CHAT_ID')) else '未設定'}")
    print(f"  端點：")
    print(f"    POST /sheet             （N8N 每張原稿 streaming 上傳）")
    print(f"    GET  /preview/<name>    （瀏覽器審核頁）")
    print(f"    POST /finalize/<name>   （確認裁切 + 打包）")
    print(f"    POST /reject/<name>    （退回清除）")
    print(f"    POST /process           （舊端點，向下相容）")
    print()

    uvicorn.run(app, host=args.host, port=args.port)
