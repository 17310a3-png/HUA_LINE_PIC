"""
LINE 貼圖後端服務（Supabase 版）

所有真實狀態 → Supabase DB + Storage。
相比舊版移除了本機 .state/、gemini/、完成圖檔區/、zip儲存區/ 的依賴
（仍會用 /tmp 作為 make_stickers.py 的工作目錄）。

啟動方式：
  pip install -r requirements.txt
  python local_webhook.py [--port 5000]

必填環境變數 / config.json 欄位：
  SUPABASE_URL              e.g. https://xxx.supabase.co
  SUPABASE_SERVICE_KEY      service_role key（繞過 RLS）
  SUPABASE_JWT_SECRET       驗證前端送來的 access token

選填：
  N8N_WEBHOOK_URL           前端建立 job 後，觸發 N8N 的 webhook
  N8N_SHARED_SECRET         N8N 回呼 /jobs/{id}/sheet 要帶的 header secret
  INTERNAL_CLEANUP_TOKEN    pg_cron 呼叫 /internal/cleanup 的 bearer token
  PUBLIC_URL                對外公開 URL（僅用於日誌顯示）

端點：
  健康檢查
    GET  /health

  前端（需帶 Authorization: Bearer <supabase_access_token>）
    POST /jobs                    建立 job（multipart：參考圖 + json 欄位）
    GET  /jobs                    列出我的 jobs
    GET  /jobs/{id}               查詢 job 詳情（含 sheet signed URLs）
    POST /jobs/{id}/finalize      確認裁切
    POST /jobs/{id}/reject        拒絕重生
    GET  /jobs/{id}/zip           取得 ZIP signed URL

  N8N 回呼（X-Webhook-Secret header）
    POST /jobs/{id}/sheet         上傳單張 sheet 原稿

  內部維護（Authorization: Bearer <INTERNAL_CLEANUP_TOKEN>）
    POST /internal/cleanup        清過期 job 的 Storage 檔案
"""

import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional
from uuid import UUID

import httpx
import jwt
import uvicorn
from fastapi import (
    BackgroundTasks, Depends, FastAPI, File, Form, Header,
    HTTPException, UploadFile, status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client

# ============================================================
# 設定
# ============================================================

BASE_DIR = Path(__file__).parent
MAKE_STICKERS = BASE_DIR / "make_stickers.py"
CONFIG_FILE = BASE_DIR / "config.json"

STORAGE_BUCKET = "sticker-assets"
SIGNED_URL_TTL_PREVIEW = 3600        # 1 hour
SIGNED_URL_TTL_DOWNLOAD = 300        # 5 min


def load_config():
    keys = [
        "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_JWT_SECRET",
        "N8N_WEBHOOK_URL", "N8N_SHARED_SECRET", "INTERNAL_CLEANUP_TOKEN",
        "PUBLIC_URL",
    ]
    cfg = {k: os.environ.get(k, "") for k in keys}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k in keys:
                if not cfg[k] and data.get(k):
                    cfg[k] = str(data[k])
        except Exception as e:
            print(f"  警告：讀取 config.json 失敗：{e}")
    return cfg


CONFIG = load_config()

required = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_JWT_SECRET"]
missing = [k for k in required if not CONFIG.get(k)]
if missing:
    print(f"  警告：缺少必填設定 {missing}，Supabase 相關端點將無法運作", file=sys.stderr)

sb: Optional[Client] = None
if CONFIG.get("SUPABASE_URL") and CONFIG.get("SUPABASE_SERVICE_KEY"):
    sb = create_client(CONFIG["SUPABASE_URL"], CONFIG["SUPABASE_SERVICE_KEY"])


app = FastAPI(title="LINE 貼圖後端服務（Supabase）")


# ============================================================
# Auth
# ============================================================

def require_user(authorization: Optional[str] = Header(None)) -> str:
    """驗證 Supabase access token，回傳 user_id (sub)"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    secret = CONFIG.get("SUPABASE_JWT_SECRET")
    if not secret:
        raise HTTPException(500, "Server missing SUPABASE_JWT_SECRET")
    try:
        payload = jwt.decode(
            token, secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid token: {e}")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(401, "Token missing sub")
    return user_id


def require_n8n_secret(x_webhook_secret: Optional[str] = Header(None)):
    expected = CONFIG.get("N8N_SHARED_SECRET")
    if not expected:
        # 未設定 secret → 警告但放行（開發方便；上線務必設定）
        return
    if x_webhook_secret != expected:
        raise HTTPException(401, "Invalid webhook secret")


def require_internal_token(authorization: Optional[str] = Header(None)):
    expected = CONFIG.get("INTERNAL_CLEANUP_TOKEN")
    if not expected:
        raise HTTPException(500, "Server missing INTERNAL_CLEANUP_TOKEN")
    if not authorization or authorization != f"Bearer {expected}":
        raise HTTPException(401, "Invalid internal token")


# ============================================================
# Supabase helper
# ============================================================

def _ensure_sb() -> Client:
    if sb is None:
        raise HTTPException(503, "Supabase client not configured")
    return sb


def _storage_upload(path: str, data: bytes, content_type: str):
    _ensure_sb().storage.from_(STORAGE_BUCKET).upload(
        path, data,
        file_options={"content-type": content_type, "upsert": "true"},
    )


def _storage_download(path: str) -> bytes:
    return _ensure_sb().storage.from_(STORAGE_BUCKET).download(path)


def _storage_remove(paths: List[str]):
    if paths:
        _ensure_sb().storage.from_(STORAGE_BUCKET).remove(paths)


def _signed_url(path: str, ttl: int) -> str:
    resp = _ensure_sb().storage.from_(STORAGE_BUCKET).create_signed_url(path, ttl)
    return resp.get("signedURL") or resp.get("signed_url") or ""


# ============================================================
# Models
# ============================================================

class JobCreateResp(BaseModel):
    job_id: str
    status: str


class SheetUpload(BaseModel):
    image_b64: str
    sheet_number: int
    total_sheets: int


# ============================================================
# 端點
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "supabase_configured": sb is not None,
        "n8n_webhook_set": bool(CONFIG.get("N8N_WEBHOOK_URL")),
        "n8n_secret_set": bool(CONFIG.get("N8N_SHARED_SECRET")),
        "public_url": CONFIG.get("PUBLIC_URL") or "(未設定)",
    }


# ---------- 前端：建立 job ----------

@app.post("/jobs", response_model=JobCreateResp)
async def create_job(
    background: BackgroundTasks,
    user_id: str = Depends(require_user),
    set_name: str = Form(...),
    character_name: str = Form(""),
    character_prompt: str = Form(""),
    series_id: str = Form(...),
    total: int = Form(40),
    model: str = Form("flash"),
    reference_image: UploadFile = File(...),
):
    client = _ensure_sb()

    # 上傳參考圖 → Storage（此時 job_id 未知，用 tmp 路徑後再搬）
    ref_bytes = await reference_image.read()
    if not ref_bytes:
        raise HTTPException(400, "reference_image is empty")

    # 先建 job 取得 id
    ins = client.table("jobs").insert({
        "user_id": user_id,
        "set_name": set_name,
        "character_name": character_name or None,
        "character_prompt": character_prompt or None,
        "series_id": series_id,
        "total": total,
        "model": model,
        "status": "pending",
    }).execute()
    job = ins.data[0]
    job_id = job["id"]

    # 上傳參考圖到正式路徑
    ref_path = f"{user_id}/{job_id}/reference.png"
    _storage_upload(ref_path, ref_bytes, reference_image.content_type or "image/png")
    client.table("jobs").update({
        "reference_image_path": ref_path,
        "status": "generating",
    }).eq("id", job_id).execute()

    # 背景觸發 N8N
    background.add_task(_trigger_n8n, job_id, user_id)

    return JobCreateResp(job_id=job_id, status="generating")


def _trigger_n8n(job_id: str, user_id: str):
    url = CONFIG.get("N8N_WEBHOOK_URL")
    if not url:
        print(f"  [N8N 未設定] job={job_id} 建立完成但未觸發 N8N")
        return
    client = _ensure_sb()
    # 讀 job 完整資料
    job = client.table("jobs").select("*").eq("id", job_id).single().execute().data
    series = client.table("series").select("*").eq("id", job["series_id"]).single().execute().data

    # 參考圖 signed URL
    ref_url = _signed_url(job["reference_image_path"], SIGNED_URL_TTL_PREVIEW)

    payload = {
        "job_id": job_id,
        "user_id": user_id,
        "set_name": job["set_name"],
        "character_name": job["character_name"],
        "character_prompt": job["character_prompt"],
        "series_id": job["series_id"],
        "series_name": series["name"] if series else "",
        "series_items": (series["items"] if series else []),
        "total": job["total"],
        "model": job["model"],
        "reference_image_url": ref_url,
        "callback_url": f"{CONFIG.get('PUBLIC_URL','').rstrip('/')}/jobs/{job_id}/sheet",
    }
    try:
        r = httpx.post(url, json=payload, timeout=30)
        print(f"  觸發 N8N：{r.status_code}")
        if r.status_code >= 400:
            print(f"  N8N 回應：{r.text[:300]}")
    except Exception as e:
        print(f"  觸發 N8N 失敗：{e}")
        client.table("jobs").update({"status": "failed", "error": f"n8n trigger: {e}"}).eq("id", job_id).execute()


# ---------- 前端：列出 / 查詢 ----------

@app.get("/jobs")
def list_jobs(user_id: str = Depends(require_user)):
    client = _ensure_sb()
    rows = (
        client.table("jobs")
        .select("id,set_name,series_id,total,model,status,created_at,expires_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute().data
    )
    return {"jobs": rows}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, user_id: str = Depends(require_user)):
    client = _ensure_sb()
    job = _get_my_job(client, job_id, user_id)

    sheets = (
        client.table("sheets")
        .select("sheet_number,storage_path")
        .eq("job_id", job_id)
        .order("sheet_number")
        .execute().data
    )
    sheet_urls = [
        {
            "sheet_number": s["sheet_number"],
            "url": _signed_url(s["storage_path"], SIGNED_URL_TTL_PREVIEW),
        }
        for s in sheets
    ]
    return {"job": job, "sheets": sheet_urls}


def _get_my_job(client: Client, job_id: str, user_id: str) -> dict:
    try:
        UUID(job_id)
    except ValueError:
        raise HTTPException(400, "Invalid job id")
    resp = client.table("jobs").select("*").eq("id", job_id).single().execute()
    job = resp.data
    if not job:
        raise HTTPException(404, "Job not found")
    if job["user_id"] != user_id:
        raise HTTPException(403, "Not your job")
    return job


# ---------- N8N 回呼：上傳單張 sheet ----------

@app.post("/jobs/{job_id}/sheet")
def upload_sheet(
    job_id: str,
    body: SheetUpload,
    _=Depends(require_n8n_secret),
):
    client = _ensure_sb()
    try:
        UUID(job_id)
    except ValueError:
        raise HTTPException(400, "Invalid job id")

    job = client.table("jobs").select("*").eq("id", job_id).single().execute().data
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in ("generating", "pending"):
        raise HTTPException(409, f"Job not accepting sheets (status={job['status']})")

    try:
        img_bytes = base64.b64decode(body.image_b64)
    except Exception:
        raise HTTPException(400, "image_b64 decode failed")

    storage_path = f"{job['user_id']}/{job_id}/sheet_{body.sheet_number}.png"
    _storage_upload(storage_path, img_bytes, "image/png")

    client.table("sheets").upsert({
        "job_id": job_id,
        "sheet_number": body.sheet_number,
        "storage_path": storage_path,
    }, on_conflict="job_id,sheet_number").execute()

    # 收齊就轉 review
    received = client.table("sheets").select("sheet_number", count="exact").eq("job_id", job_id).execute()
    received_count = received.count or 0
    if received_count >= body.total_sheets:
        client.table("jobs").update({"status": "review"}).eq("id", job_id).execute()

    return {"ok": True, "received": received_count, "total_sheets": body.total_sheets}


# ---------- 前端：finalize（確認裁切） ----------

@app.post("/jobs/{job_id}/finalize")
def finalize(job_id: str, user_id: str = Depends(require_user)):
    client = _ensure_sb()
    job = _get_my_job(client, job_id, user_id)
    if job["status"] != "review":
        raise HTTPException(409, f"Job status must be 'review' (got '{job['status']}')")

    sheets = (
        client.table("sheets")
        .select("sheet_number,storage_path")
        .eq("job_id", job_id)
        .order("sheet_number")
        .execute().data
    )
    if not sheets:
        raise HTTPException(400, "No sheets")

    client.table("jobs").update({"status": "finalizing"}).eq("id", job_id).execute()

    try:
        result = _run_cropping(job, sheets)
    except Exception as e:
        client.table("jobs").update({"status": "failed", "error": str(e)}).eq("id", job_id).execute()
        raise

    # 上傳 ZIP
    zip_path = result["zip_path"]
    zip_bytes = Path(zip_path).read_bytes()
    zip_storage = f"{user_id}/{job_id}/{job['set_name']}.zip"
    _storage_upload(zip_storage, zip_bytes, "application/zip")

    client.table("jobs").update({
        "status": "done",
        "zip_path": zip_storage,
    }).eq("id", job_id).execute()

    return {"ok": True, "zip_path": zip_storage, "files": result["files"]}


def _run_cropping(job: dict, sheets: List[dict]) -> dict:
    """把 sheets 從 Supabase 下載到 /tmp → 呼叫 make_stickers.py → 回傳 zip 路徑"""
    tmp = Path(tempfile.mkdtemp(prefix=f"job_{job['id'][:8]}_"))
    gemini_dir = tmp / "gemini"
    gemini_dir.mkdir(parents=True)

    sheet_filenames = []
    total_sheets = len(sheets)
    for s in sheets:
        fname = f"{job['set_name']}_sheet{s['sheet_number']}.png"
        (gemini_dir / fname).write_bytes(_storage_download(s["storage_path"]))
        sheet_filenames.append(fname)

    # main/tab 取最後一張 sheet 的 0,1 格
    main_sheet_idx = total_sheets - 1
    cmd = [
        sys.executable, str(MAKE_STICKERS),
        "--name", job["set_name"],
        "--total", str(job["total"]),
        "--sheets", ",".join(sheet_filenames),
        "--main", f"{main_sheet_idx},0",
        "--tab", f"{main_sheet_idx},1",
    ]
    env = os.environ.copy()
    env["DATA_DIR"] = str(tmp)
    print(f"  跑 make_stickers: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE_DIR), env=env)
    if proc.returncode != 0:
        # 清 tmp 再丟錯
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, {"stderr": proc.stderr, "stdout": proc.stdout})

    out_dir = tmp / "完成圖檔區" / job["set_name"]
    zip_path = tmp / "zip儲存區" / f"{job['set_name']}.zip"
    files = sorted(f.name for f in out_dir.glob("*.png")) if out_dir.exists() else []

    return {"zip_path": str(zip_path), "files": files, "tmp": str(tmp)}


# ---------- 前端：reject / zip ----------

@app.post("/jobs/{job_id}/reject")
def reject(job_id: str, user_id: str = Depends(require_user)):
    client = _ensure_sb()
    job = _get_my_job(client, job_id, user_id)

    # 刪 storage
    sheets = (
        client.table("sheets")
        .select("storage_path")
        .eq("job_id", job_id)
        .execute().data
    )
    paths = [s["storage_path"] for s in sheets]
    if job.get("reference_image_path"):
        paths.append(job["reference_image_path"])
    if paths:
        _storage_remove(paths)

    client.table("sheets").delete().eq("job_id", job_id).execute()
    client.table("jobs").update({"status": "rejected"}).eq("id", job_id).execute()
    return {"ok": True}


@app.get("/jobs/{job_id}/zip")
def download_zip(job_id: str, user_id: str = Depends(require_user)):
    client = _ensure_sb()
    job = _get_my_job(client, job_id, user_id)
    if job["status"] != "done" or not job.get("zip_path"):
        raise HTTPException(409, f"ZIP not ready (status={job['status']})")
    url = _signed_url(job["zip_path"], SIGNED_URL_TTL_DOWNLOAD)
    return {"url": url}


# ---------- 內部：pg_cron 過期清除 ----------

@app.post("/internal/cleanup")
def cleanup(_=Depends(require_internal_token)):
    client = _ensure_sb()
    # 找過期 + 狀態還在 done/review 的 job
    expired = (
        client.table("jobs")
        .select("id,user_id,reference_image_path,zip_path")
        .lt("expires_at", "now()")
        .in_("status", ["done", "review"])
        .execute().data
    )

    removed = 0
    for j in expired:
        sheets = (
            client.table("sheets")
            .select("storage_path")
            .eq("job_id", j["id"])
            .execute().data
        )
        paths = [s["storage_path"] for s in sheets]
        if j.get("reference_image_path"):
            paths.append(j["reference_image_path"])
        if j.get("zip_path"):
            paths.append(j["zip_path"])
        if paths:
            _storage_remove(paths)
            removed += len(paths)
        client.table("sheets").delete().eq("job_id", j["id"]).execute()

    return {"ok": True, "jobs_expired": len(expired), "storage_objects_removed": removed}


# ============================================================
# 啟動
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LINE 貼圖後端（Supabase 版）")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    print(f"=== LINE 貼圖後端（Supabase） ===")
    print(f"  監聽：http://{args.host}:{args.port}")
    print(f"  SUPABASE_URL：{CONFIG.get('SUPABASE_URL') or '(未設定)'}")
    print(f"  Supabase client：{'已連線' if sb else '未連線'}")
    print(f"  N8N webhook：{CONFIG.get('N8N_WEBHOOK_URL') or '(未設定)'}")
    print(f"  N8N secret：{'已設定' if CONFIG.get('N8N_SHARED_SECRET') else '未設定（任何請求都會被接受）'}")
    print(f"  PUBLIC_URL：{CONFIG.get('PUBLIC_URL') or '(未設定)'}")
    print()

    uvicorn.run(app, host=args.host, port=args.port)
