from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core import database as db
from app.core.tasks import (
    ARCHIVES_DIR,
    ensure_data_dirs,
    process_batch,
    retry_download_item,
    retry_failed,
    save_upload,
    start_download,
    start_background,
)


app = FastAPI(title="Order Design Image Downloader")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def app_password() -> str:
    return os.getenv("APP_PASSWORD", "")


def is_authenticated(request: Request) -> bool:
    password = app_password()
    if not password:
        return True
    return request.cookies.get("app_auth") == password


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


@app.on_event("startup")
def startup() -> None:
    ensure_data_dirs()
    db.init_db()


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/login")
def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": None}
    )


@app.post("/login")
def login(request: Request, password: str = Form("")):
    expected = app_password()
    if expected and password != expected:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "密码不正确"},
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    if expected:
        response.set_cookie("app_auth", expected, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("app_auth")
    return response


@app.get("/")
def index(request: Request):
    require_auth(request)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "batches": db.list_batches(), "password_enabled": bool(app_password())},
    )


@app.get("/uploads/new")
def upload_page(request: Request):
    require_auth(request)
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/uploads")
async def upload_excel(request: Request, file: UploadFile = File(...)):
    require_auth(request)
    filename = file.filename or "orders.xlsx"
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(status_code=400, detail="Only .xlsx files are supported.")

    batch_id = db.create_batch(filename, Path(""))
    content = await file.read()
    source_path = save_upload(batch_id, filename, content)
    db.update_batch_source(batch_id, source_path)
    start_background(process_batch, batch_id, source_path)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.get("/batches/{batch_id}")
def batch_detail(request: Request, batch_id: int):
    require_auth(request)
    batch = db.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    orders = db.get_batch_orders(batch_id)
    single_item_orders = [order for order in orders if len(order["items"]) == 1]
    multi_item_orders = [order for order in orders if len(order["items"]) > 1]
    status_counts = db.get_batch_status_counts(batch_id)
    progress_percent = 0
    if int(batch["link_count"]) > 0:
        progress_percent = round(
            int(batch["success_count"]) * 100 / int(batch["link_count"])
        )
    return templates.TemplateResponse(
        "batch_detail.html",
        {
            "request": request,
            "batch": batch,
            "single_item_orders": single_item_orders,
            "multi_item_orders": multi_item_orders,
            "status_counts": status_counts,
            "progress_percent": progress_percent,
        },
    )


@app.get("/batches/{batch_id}/status")
def batch_status(request: Request, batch_id: int):
    require_auth(request)
    batch = db.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return {"batch": batch, "status_counts": db.get_batch_status_counts(batch_id)}


@app.post("/batches/{batch_id}/start-download")
def start_batch_download(request: Request, batch_id: int):
    require_auth(request)
    batch = db.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    start_background(start_download, batch_id)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/retry-failed")
def retry_failed_items(request: Request, batch_id: int):
    require_auth(request)
    batch = db.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    start_background(retry_failed, batch_id)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.post("/download-items/{download_item_id}/retry")
def retry_one_download_item(request: Request, download_item_id: int):
    require_auth(request)
    item = db.get_download_item(download_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Download item not found")
    start_background(retry_download_item, download_item_id)
    return RedirectResponse(f"/batches/{item['batch_id']}", status_code=303)


@app.get("/batches/{batch_id}/download.zip")
def download_archive(request: Request, batch_id: int):
    require_auth(request)
    batch = db.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    archive_path = Path(batch["archive_path"] or ARCHIVES_DIR / f"batch-{batch_id}.zip")
    if not archive_path.exists():
        raise HTTPException(status_code=404, detail="Archive is not ready")
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"batch-{batch_id}-orders.zip",
    )
