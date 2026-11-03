import asyncio
import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.document_tasks import ocr_configuration

ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 15 * 1024**2
MAX_STORAGE = 250 * 1024**2
EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".csv", ".txt"}
BROKER_HOST = "dl-broker-juwon-4jt3z.azurewebsites.net"
BROKER_PATH = "/api/directlineToken"


def now():
    return datetime.now(timezone.utc).isoformat()


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    gx_works_version: Literal["GX Works2", "GX Works3", "미정"] = "미정"
    cpu_model: str = Field(default="", max_length=100)
    requirements: str = Field(default="", max_length=20_000)


class VersionInput(BaseModel):
    kind: Literal["original", "proposal"]
    text: str = Field(min_length=1, max_length=150_000)
    base_id: str | None = None


def create_app(data_dir=None):
    storage = Path(data_dir or os.environ.get("PLC_DATA_DIR", ROOT / ".local-data")).resolve()
    web = ROOT / "web"
    if storage == web or web in storage.parents or storage == ROOT or storage in ROOT.parents:
        raise RuntimeError("PLC_DATA_DIR must be a dedicated private directory, not the web or repository root.")
    lock = threading.Lock()
    session_key = secrets.token_urlsafe(32)
    processing = asyncio.Semaphore(1)
    broker_lock = asyncio.Lock()
    last_broker = 0.0

    @contextmanager
    def database():
        connection = sqlite3.connect(storage / "local.sqlite3", timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def event(connection, project_id, action, detail):
        connection.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                           (str(uuid.uuid4()), project_id, action, detail, now()))

    def project_exists(connection, project_id):
        if not connection.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone():
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")

    @asynccontextmanager
    async def lifespan(app):
        storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        (storage / "files").mkdir(exist_ok=True, mode=0o700)
        with database() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, gx_works_version TEXT NOT NULL,
                    cpu_model TEXT NOT NULL, requirements TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL, suffix TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, extraction TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    kind TEXT NOT NULL, text TEXT NOT NULL, base_id TEXT,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ocr_results (
                    id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(id),
                    page INTEGER NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
        yield

    app = FastAPI(title="PLC Engineering AI — Local only", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "")
        allowed_host = host.split(":")[0] in ("localhost", "127.0.0.1")
        if not allowed_host or not request.client or request.client.host not in ("127.0.0.1", "::1"):
            return JSONResponse({"detail": "로컬 단일사용 전용입니다. 공개 배포 및 프록시 접근은 금지됩니다."}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"http://{host}":
            return JSONResponse({"detail": "동일 출처 요청만 허용됩니다."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "다른 사이트에서 접근할 수 없습니다."}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/session":
            if not hmac.compare_digest(request.headers.get("x-local-session", ""), session_key):
                return JSONResponse({"detail": "로컬 세션을 새로고침하세요."}, status_code=403)
        # Bound JSON requests before the framework buffers/parses them.
        if request.method in ("POST", "PUT", "PATCH") and not request.url.path.endswith("/files"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    return JSONResponse({"detail": "요청 크기 제한을 초과했습니다."}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdn.botframework.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; "
            "font-src 'self' data:; connect-src 'self' https://*.directline.botframework.com "
            "wss://*.directline.botframework.com https://directline.botframework.com "
            "wss://directline.botframework.com; frame-src 'none'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/api/session")
    def session():
        return {"session": session_key, "scope": "local-single-user"}

    @app.get("/api/status")
    def status():
        enabled = os.environ.get("PLC_ENABLE_BROKER", "").lower() == "true"
        sealed = bool(os.environ.get("PLC_BROKER_SEALED", "").strip())
        return {
            "scope": "local-single-user", "public_deployment_allowed": False,
            "broker_enabled": enabled, "broker_configured": sealed,
            "agent_connection": "unverified", "agent_routing": "unverified",
            "file_delivery": "unverified", "ocr": ocr_configuration(),
            "webchat_version": "4.18.0", "max_upload_bytes": MAX_UPLOAD,
            "max_pdf_pages": 40,
        }

    @app.get("/api/projects")
    def projects():
        with database() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM projects ORDER BY created_at DESC")]

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectInput):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "프로젝트 이름을 입력하세요.")
        project_id = str(uuid.uuid4())
        with lock, database() as connection:
            if connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] >= 50:
                raise HTTPException(409, "로컬 프로젝트 한도(50개)에 도달했습니다.")
            connection.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                               (project_id, name, body.gx_works_version, body.cpu_model.strip(),
                                body.requirements, now()))
            event(connection, project_id, "project_created", "프로젝트 생성")
        return {"id": project_id}

    @app.put("/api/projects/{project_id}")
    def update_project(project_id: str, body: ProjectInput):
        if not body.name.strip():
            raise HTTPException(422, "프로젝트 이름을 입력하세요.")
        with lock, database() as connection:
            project_exists(connection, project_id)
            connection.execute("UPDATE projects SET name=?, gx_works_version=?, cpu_model=?, requirements=? WHERE id=?",
                               (body.name.strip(), body.gx_works_version, body.cpu_model.strip(),
                                body.requirements, project_id))
            event(connection, project_id, "project_updated", "기본 정보 및 요구사항 저장")
        return {"id": project_id}

    @app.get("/api/projects/{project_id}/files")
    def files(project_id: str):
        with database() as connection:
            project_exists(connection, project_id)
            result = []
            for row in connection.execute("SELECT * FROM files WHERE project_id=? ORDER BY created_at DESC",
                                          (project_id,)):
                item = dict(row)
                extraction = json.loads(item.pop("extraction"))
                item.update(upload_status="stored", extraction_status=extraction["status"],
                            ai_delivery_status="disconnected", details=extraction["details"])
                latest = connection.execute(
                    "SELECT page, json_extract(result, '$.status') AS status FROM ocr_results "
                    "WHERE file_id=? ORDER BY created_at DESC LIMIT 1", (item["id"],)).fetchone()
                item["ocr"] = dict(latest) if latest else {"status": "not_run", "page": None}
                result.append(item)
            return result

    def parse_file(path, suffix):
        try:
            process = subprocess.run(
                [sys.executable, "-m", "app.extractor", str(path), suffix],
                cwd=ROOT, capture_output=True, timeout=25, check=False,
                env={key: value for key, value in os.environ.items()
                     if key not in ("PLC_BROKER_SEALED", "PLC_ENABLE_BROKER")},
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(422, "파일 처리 제한 시간(25초)을 초과했습니다. 더 작은 파일로 나누세요.")
        if process.returncode != 0 or len(process.stdout) > 4 * 1024**2:
            raise HTTPException(422, "파일 처리 자원 제한 또는 파서 실행 오류입니다.")
        try:
            result = json.loads(process.stdout)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "파일 처리 응답이 올바르지 않습니다.")
        if not result.get("valid"):
            raise HTTPException(503 if result.get("dependency_missing") else 422,
                                result.get("error", "파일 검증에 실패했습니다."))
        return result

    @app.post("/api/projects/{project_id}/files", status_code=201)
    async def upload(project_id: str, request: Request):
        name = unquote(request.headers.get("x-filename", ""))
        if not name or len(name) > 180 or name in (".", "..") or any(c in name for c in '/\\:\x00'):
            raise HTTPException(422, "경로가 아닌 파일 이름만 허용합니다(180자 이하).")
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise HTTPException(422, "파일 이름에 제어 문자를 사용할 수 없습니다.")
        suffix = Path(name).suffix.lower()
        if suffix not in EXTENSIONS:
            raise HTTPException(415, "PDF, JPG, JPEG, PNG, XLSX, CSV, TXT만 지원합니다.")
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise HTTPException(415, "application/octet-stream 원본 파일 본문이 필요합니다.")
        if processing.locked():
            raise HTTPException(429, "다른 파일을 처리 중입니다. 완료 후 다시 시도하세요.")
        async with processing:
            with database() as connection:
                project_exists(connection, project_id)
                total, count = connection.execute("SELECT COALESCE(SUM(size),0), COUNT(*) FROM files").fetchone()
                if count >= 100:
                    raise HTTPException(409, "로컬 파일 한도(100개)에 도달했습니다.")
            file_id = str(uuid.uuid4())
            temporary = storage / "files" / (file_id + ".upload")
            destination = storage / "files" / (file_id + suffix)
            size = 0
            digest = hashlib.sha256()
            try:
                with temporary.open("xb") as target:
                    os.chmod(temporary, 0o600)
                    try:
                        async with asyncio.timeout(30):
                            async for chunk in request.stream():
                                size += len(chunk)
                                if size > MAX_UPLOAD or total + size > MAX_STORAGE:
                                    raise HTTPException(413, "파일 15MB 또는 전체 보관량 250MB 제한을 초과했습니다.")
                                digest.update(chunk)
                                target.write(chunk)
                    except TimeoutError:
                        raise HTTPException(408, "파일 업로드 제한 시간(30초)을 초과했습니다.")
                if not size:
                    raise HTTPException(422, "빈 파일은 업로드할 수 없습니다.")
                extraction = await asyncio.to_thread(parse_file, temporary, suffix)
                temporary.rename(destination)
                with database() as connection:
                    project_exists(connection, project_id)
                    connection.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)",
                                       (file_id, project_id, name, suffix, size, digest.hexdigest(),
                                        json.dumps(extraction, ensure_ascii=True), now()))
                    event(connection, project_id, "file_stored", f"{name}: 원본 보관·{extraction['status']}·AI 전달 미연결")
            except BaseException:
                temporary.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise
        return {"id": file_id, "upload_status": "stored", "extraction_status": extraction["status"],
                "ai_delivery_status": "disconnected"}

    def owned_file(connection, project_id, file_id):
        row = connection.execute("SELECT * FROM files WHERE id=? AND project_id=?", (file_id, project_id)).fetchone()
        if not row:
            raise HTTPException(404, "이 프로젝트의 파일을 찾을 수 없습니다.")
        return row

    def document_file(project_id, file_id):
        with database() as connection:
            row = owned_file(connection, project_id, file_id)
        if row["suffix"] not in (".pdf", ".jpg", ".jpeg", ".png"):
            raise HTTPException(415, "PDF 또는 PNG/JPG/JPEG 파일만 지원합니다.")
        path = storage / "files" / (row["id"] + row["suffix"])
        if not path.is_file():
            raise HTTPException(410, "원본이 디스크에 없습니다. 로컬 보관 폴더를 확인하세요.")
        return path

    def document_task(mode, path, page):
        try:
            process = subprocess.run(
                [sys.executable, "-m", "app.document_tasks", mode, str(path), str(page)],
                cwd=ROOT, capture_output=True, timeout=32, check=False,
                env={key: value for key, value in os.environ.items()
                     if key not in ("PLC_BROKER_SEALED", "PLC_ENABLE_BROKER")},
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(422, "문서 처리 제한 시간(32초)을 초과했습니다. 원본은 변경되지 않았습니다.")
        if process.returncode != 0 or len(process.stdout) > 9 * 1024**2:
            raise HTTPException(422, "문서 처리 자원 제한 또는 엔진 실행 오류입니다.")
        try:
            result = json.loads(process.stdout)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "문서 처리 응답이 올바르지 않습니다.")
        if not result.get("ok"):
            raise HTTPException(result.get("code", 422), result.get("error", "문서 처리 실패"))
        return result

    @app.get("/api/projects/{project_id}/files/{file_id}/document")
    async def document_metadata(project_id: str, file_id: str):
        path = document_file(project_id, file_id)
        if processing.locked():
            raise HTTPException(429, "다른 파일을 처리 중입니다. 완료 후 다시 시도하세요.")
        async with processing:
            return await asyncio.to_thread(document_task, "metadata", path, 1)

    @app.get("/api/projects/{project_id}/files/{file_id}/preview")
    async def preview(project_id: str, file_id: str, page: int = Query(default=1, ge=1, le=40)):
        path = document_file(project_id, file_id)
        if processing.locked():
            raise HTTPException(429, "다른 파일을 처리 중입니다. 완료 후 다시 시도하세요.")
        async with processing:
            result = await asyncio.to_thread(document_task, "preview", path, page)
        return Response(base64.b64decode(result["image"], validate=True), media_type="image/png",
                        headers={"X-Page-Count": str(result["page_count"]), "X-Preview-Page": str(page)})

    @app.get("/api/projects/{project_id}/files/{file_id}/ocr")
    def ocr_history(project_id: str, file_id: str, page: int = Query(default=1, ge=1, le=40)):
        with database() as connection:
            owned_file(connection, project_id, file_id)
            rows = connection.execute(
                "SELECT * FROM ocr_results WHERE file_id=? AND page=? ORDER BY created_at DESC LIMIT 5",
                (file_id, page))
            return [dict(json.loads(row["result"]), id=row["id"], page=row["page"],
                         created_at=row["created_at"]) for row in rows]

    @app.post("/api/projects/{project_id}/files/{file_id}/ocr", status_code=201)
    async def run_ocr(project_id: str, file_id: str, page: int = Query(default=1, ge=1, le=40)):
        path = document_file(project_id, file_id)
        config = ocr_configuration()
        if config["status"] != "configured":
            raise HTTPException(503, config["reason"])
        if processing.locked():
            raise HTTPException(429, "다른 파일을 처리 중입니다. 완료 후 다시 시도하세요.")
        async with processing:
            with database() as connection:
                if connection.execute("SELECT COUNT(*) FROM ocr_results").fetchone()[0] >= 500:
                    raise HTTPException(409, "로컬 OCR 실행 보관 한도(500회)에 도달했습니다.")
            error = None
            try:
                result = await asyncio.to_thread(document_task, "ocr", path, page)
            except HTTPException as failure:
                if failure.status_code == 404:
                    raise
                error = failure
                result = {"status": "failed", "text": "", "details": [str(failure.detail)]}
            result.update(id=str(uuid.uuid4()), page=page, created_at=now(),
                          ai_delivery_status="disconnected")
            with database() as connection:
                connection.execute("INSERT INTO ocr_results VALUES (?,?,?,?,?)",
                                   (result["id"], file_id, page, json.dumps(result, ensure_ascii=True),
                                    result["created_at"]))
                label = {"extracted": "문자 추출·원본 대조 필요", "partial": "일부 문자 추출·원본 대조 필요",
                         "no_text": "인식 문자 없음", "failed": "실패"}[result["status"]]
                event(connection, project_id, "local_ocr",
                      f"로컬 OCR · {page}페이지 · {label} · AI 전달 미연결")
            if error:
                raise error
            return result

    @app.get("/api/projects/{project_id}/files/{file_id}/extraction")
    def extraction(project_id: str, file_id: str):
        with database() as connection:
            row = owned_file(connection, project_id, file_id)
            return json.loads(row["extraction"])

    @app.get("/api/projects/{project_id}/files/{file_id}/original")
    def original(project_id: str, file_id: str):
        with database() as connection:
            row = owned_file(connection, project_id, file_id)
        path = storage / "files" / (row["id"] + row["suffix"])
        if not path.is_file():
            raise HTTPException(410, "원본이 디스크에 없습니다. 로컬 보관 폴더를 확인하세요.")
        return FileResponse(path, filename=row["name"], media_type="application/octet-stream",
                            content_disposition_type="attachment")

    @app.get("/api/projects/{project_id}/versions")
    def versions(project_id: str):
        with database() as connection:
            project_exists(connection, project_id)
            return [dict(row) for row in connection.execute(
                "SELECT * FROM versions WHERE project_id=? ORDER BY created_at ASC", (project_id,))]

    @app.post("/api/projects/{project_id}/versions", status_code=201)
    def save_version(project_id: str, body: VersionInput):
        with lock, database() as connection:
            project_exists(connection, project_id)
            if connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] >= 500:
                raise HTTPException(409, "로컬 텍스트 버전 한도(500개)에 도달했습니다.")
            current = connection.execute(
                "SELECT id FROM versions WHERE project_id=? AND kind IN ('original','approved') ORDER BY created_at DESC LIMIT 1",
                (project_id,)).fetchone()
            if body.kind == "original" and current:
                raise HTTPException(409, "원본은 덮어쓸 수 없습니다. 수정안으로 저장하세요.")
            if body.kind == "proposal" and (not current or body.base_id != current["id"]):
                raise HTTPException(409, "현재 원본/승인본을 기준으로 수정안을 저장하세요.")
            version_id = str(uuid.uuid4())
            connection.execute("INSERT INTO versions VALUES (?,?,?,?,?,?)",
                               (version_id, project_id, body.kind, body.text, body.base_id, now()))
            event(connection, project_id, "version_saved", f"{body.kind}: 텍스트 저장·미검사")
        return {"id": version_id}

    @app.post("/api/projects/{project_id}/versions/{version_id}/approve", status_code=201)
    def approve(project_id: str, version_id: str):
        with lock, database() as connection:
            if connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] >= 500:
                raise HTTPException(409, "로컬 텍스트 버전 한도(500개)에 도달했습니다.")
            proposal = connection.execute(
                "SELECT * FROM versions WHERE id=? AND project_id=? AND kind='proposal'", (version_id, project_id)).fetchone()
            if not proposal:
                raise HTTPException(404, "수정안을 찾을 수 없습니다.")
            current = connection.execute(
                "SELECT id FROM versions WHERE project_id=? AND kind IN ('original','approved') ORDER BY created_at DESC LIMIT 1",
                (project_id,)).fetchone()
            if not current or proposal["base_id"] != current["id"]:
                raise HTTPException(409, "기준 버전이 변경되었습니다. 최신 버전과 다시 비교하세요.")
            approved_id = str(uuid.uuid4())
            connection.execute("INSERT INTO versions VALUES (?,?,?,?,?,?)",
                               (approved_id, project_id, "approved", proposal["text"], version_id, now()))
            event(connection, project_id, "version_approved", "사용자 승인본 새 버전 저장·재검사 필요(미연결)")
        return {"id": approved_id, "inspection_status": "not_run"}

    @app.get("/api/history")
    def history(project_id: str | None = None):
        with database() as connection:
            if project_id:
                project_exists(connection, project_id)
                rows = connection.execute("SELECT * FROM events WHERE project_id=? ORDER BY created_at DESC LIMIT 200",
                                          (project_id,))
            else:
                rows = connection.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT 200")
            return [dict(row) for row in rows]

    @app.post("/api/chat/token")
    async def token():
        nonlocal last_broker
        if os.environ.get("PLC_ENABLE_BROKER", "").lower() != "true":
            raise HTTPException(503, "브로커 호출 비활성. 운영 확인 후 PLC_ENABLE_BROKER=true 설정이 필요합니다.")
        sealed = os.environ.get("PLC_BROKER_SEALED", "").strip()
        if not sealed:
            raise HTTPException(503, "서버 환경변수 PLC_BROKER_SEALED 설정이 필요합니다.")
        async with broker_lock:
            if time.monotonic() - last_broker < 10:
                raise HTTPException(429, "토큰 요청 후 10초 뒤 다시 시도하세요.")
            last_broker = time.monotonic()

            def relay():
                connection = http.client.HTTPSConnection(BROKER_HOST, timeout=8)
                try:
                    # Preserve the existing endpoint and JSON {sealed: ...} body.
                    # http.client does not add a Content-Type header.
                    body = json.dumps({"sealed": sealed}).encode("utf-8")
                    connection.request("POST", BROKER_PATH, body=body)
                    response = connection.getresponse()
                    raw = response.read(65_537)
                    if response.status != 200 or len(raw) > 65_536:
                        raise HTTPException(502, "기존 브로커가 정상 토큰 응답을 반환하지 않았습니다.")
                    try:
                        data = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        raise HTTPException(502, "기존 브로커 응답이 JSON이 아닙니다.")
                    value = data.get("token") if isinstance(data, dict) else None
                    if not isinstance(value, str) or not value.strip() or len(value) > 16_384:
                        raise HTTPException(502, "기존 브로커 응답에 유효한 token 문자열이 없습니다.")
                    return {"token": value}
                except (OSError, http.client.HTTPException):
                    raise HTTPException(502, "기존 브로커 연결 오류입니다. 서버 설정과 네트워크를 확인하세요.")
                finally:
                    connection.close()

            return await asyncio.to_thread(relay)

    app.mount("/static", StaticFiles(directory=web), name="static")

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    return app


app = create_app()
