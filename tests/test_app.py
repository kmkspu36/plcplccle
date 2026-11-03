import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import httpx

from app.main import MAX_UPLOAD, create_app


class LocalAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 50000)),
            base_url="http://localhost",
        )
        response = await self.client.get("/api/session")
        self.key = response.json()["session"]
        self.headers = {"X-Local-Session": self.key}
        self.pid = await self.new_project("테스트 프로젝트")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.temp.cleanup()

    async def new_project(self, name):
        response = await self.client.post("/api/projects", headers=self.headers,
                                          json={"name": name, "gx_works_version": "GX Works3"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    async def upload(self, name, content, pid=None):
        return await self.client.post(
            f"/api/projects/{pid or self.pid}/files",
            headers={**self.headers, "Content-Type": "application/octet-stream", "X-Filename": quote(name)},
            content=content,
        )

    async def test_project_persistence_and_real_history(self):
        response = await self.client.get("/api/projects", headers=self.headers)
        self.assertEqual(response.json()[0]["cpu_model"], "")
        self.assertEqual(response.json()[0]["requirements"], "")
        with patch("app.main.http.client.HTTPSConnection") as remote:
            status = await self.client.get("/api/status", headers=self.headers)
            self.assertEqual(status.json()["agent_connection"], "unverified")
            remote.assert_not_called()
        history = await self.client.get("/api/history", headers=self.headers)
        self.assertEqual(len(history.json()), 1)
        self.assertEqual(history.json()[0]["action"], "project_created")
        second = create_app(self.temp.name)
        async with second.router.lifespan_context(second):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second, client=("127.0.0.1", 1)),
                                         base_url="http://localhost") as client:
                session = (await client.get("/api/session")).json()["session"]
                restored = await client.get("/api/projects", headers={"X-Local-Session": session})
                self.assertEqual(restored.json()[0]["id"], self.pid)

    async def test_local_boundary_and_session(self):
        self.assertEqual((await self.client.get("/api/projects")).status_code, 403)
        self.assertEqual((await self.client.get("/api/session", headers={"Origin": "https://evil.example"})).status_code, 403)
        self.assertEqual((await self.client.get("/api/session", headers={"Host": "evil.example"})).status_code, 403)
        self.assertEqual((await self.client.get("/api/session", headers={"Sec-Fetch-Site": "cross-site"})).status_code, 403)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("192.0.2.10", 123)),
                                     base_url="http://localhost") as remote:
            self.assertEqual((await remote.get("/api/session")).status_code, 403)
        self.assertEqual((await self.client.get("/static/../.local-data/local.sqlite3")).status_code, 404)

    async def test_text_upload_original_hash_and_project_scope(self):
        content = "입력,X001\r\n정지,X002\r\n".encode("utf-8-sig")
        response = await self.upload("원본.csv", content)
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()
        self.assertEqual(item["ai_delivery_status"], "disconnected")
        self.assertEqual(item["extraction_status"], "extracted")
        files = (await self.client.get(f"/api/projects/{self.pid}/files", headers=self.headers)).json()
        self.assertEqual(files[0]["sha256"], hashlib.sha256(content).hexdigest())
        file_id = item["id"]
        original = await self.client.get(f"/api/projects/{self.pid}/files/{file_id}/original", headers=self.headers)
        self.assertEqual(original.content, content)
        self.assertIn("attachment", original.headers["content-disposition"])
        extraction = await self.client.get(f"/api/projects/{self.pid}/files/{file_id}/extraction", headers=self.headers)
        self.assertEqual(extraction.json()["tables"][0]["rows"][1], ["정지", "X002"])
        other = await self.new_project("다른 프로젝트")
        wrong_scope = await self.client.get(f"/api/projects/{other}/files/{file_id}/original", headers=self.headers)
        self.assertEqual(wrong_scope.status_code, 404)
        self.assertFalse(any(Path(self.temp.name, "files").glob("*.upload")))

    async def test_encoding_and_truncation_are_explicit(self):
        response = await self.upload("한글.txt", "정지 조건\r\n".encode("cp949"))
        self.assertEqual(response.status_code, 201, response.text)
        response = await self.upload("긴문서.txt", b"x" * 150_001)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["extraction_status"], "partial")

    async def test_empty_oversize_path_and_disguised_files_rejected(self):
        for name, payload, expected in [
            ("empty.txt", b"", 422), ("large.txt", b"x" * (MAX_UPLOAD + 1), 413),
            ("../escape.txt", b"no", 422), ("a\\b.txt", b"no", 422),
            ("bad.pdf", b"not a pdf", 422), ("bad.png", b"%PDF-1.4", 422),
            ("bad.xlsx", b"not a zip", 422), ("script.exe", b"MZ", 415),
            ("binary.txt", b"\x00\x01\x02", 422),
        ]:
            with self.subTest(name=name):
                response = await self.upload(name, payload)
                self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(list(Path(self.temp.name, "files").iterdir()), [])
        response = await self.client.get(f"/api/projects/{self.pid}/files", headers=self.headers)
        self.assertEqual(response.json(), [])

    async def test_xlsx_strings_and_formulas_not_evaluated(self):
        import openpyxl

        source = io.BytesIO()
        book = openpyxl.Workbook()
        book.active.append(["주소", "값"])
        book.active.append(["X001", "00123"])
        book.active.append(["D001", "=1+2"])
        book.save(source)
        book.close()
        response = await self.upload("io.xlsx", source.getvalue())
        self.assertEqual(response.status_code, 201, response.text)
        file_id = response.json()["id"]
        result = (await self.client.get(f"/api/projects/{self.pid}/files/{file_id}/extraction",
                                        headers=self.headers)).json()
        rows = result["tables"][0]["rows"]
        self.assertEqual(rows[1], ["X001", "00123"])
        self.assertEqual(rows[2][1], "=1+2")

    async def test_zip_bomb_rejected(self):
        source = io.BytesIO()
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", b"x" * 1_000_000)
            archive.writestr("xl/workbook.xml", b"invalid")
        response = await self.upload("bomb.xlsx", source.getvalue())
        self.assertEqual(response.status_code, 422)

    async def test_parser_timeout_cleans_uncommitted_original(self):
        with patch("app.main.subprocess.run", side_effect=subprocess.TimeoutExpired("parser", 25)):
            response = await self.upload("timeout.txt", b"do not retain failed upload")
        self.assertEqual(response.status_code, 422)
        self.assertIn("25", response.json()["detail"])
        self.assertEqual(list(Path(self.temp.name, "files").iterdir()), [])

    async def test_broker_malformed_json_and_token_are_explicit_errors(self):
        for raw in (b"not-json", b'{"token":""}', b'{"token":42}', b"[]"):
            app = create_app(self.temp.name)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                    base_url="http://localhost",
                ) as client:
                    session = (await client.get("/api/session")).json()["session"]
                    with patch.dict(os.environ, {"PLC_ENABLE_BROKER": "true", "PLC_BROKER_SEALED": "TEST-ONLY"}):
                        with patch("app.main.http.client.HTTPSConnection") as remote:
                            remote.return_value.getresponse.return_value.status = 200
                            remote.return_value.getresponse.return_value.read.return_value = raw
                            response = await client.post("/api/chat/token", headers={"X-Local-Session": session})
                            self.assertEqual(response.status_code, 502)

    async def test_image_verification_and_ocr_not_claimed(self):
        from PIL import Image

        for extension, fmt in (("png", "PNG"), ("jpg", "JPEG"), ("jpeg", "JPEG")):
            source = io.BytesIO()
            image = Image.new("RGB", (10, 10), "white")
            image.save(source, format=fmt)
            response = await self.upload("drawing." + extension, source.getvalue())
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["extraction_status"], "needs_ocr")

    async def test_pdf_page_limit_and_no_fake_ocr(self):
        def pdf(pages):
            objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
                       f"<< /Type /Pages /Kids [{' '.join(f'{i + 3} 0 R' for i in range(pages))}] /Count {pages} >>".encode()]
            objects.extend(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << >> >>"
                           for _ in range(pages))
            result = bytearray(b"%PDF-1.4\n")
            offsets = [0]
            for index, obj in enumerate(objects, 1):
                offsets.append(len(result))
                result.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
            xref = len(result)
            result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
            for offset in offsets[1:]:
                result.extend(f"{offset:010d} 00000 n \n".encode())
            result.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
            return bytes(result)

        response = await self.upload("scan.pdf", pdf(1))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["extraction_status"], "needs_ocr")
        response = await self.upload("too-many.pdf", pdf(41))
        self.assertEqual(response.status_code, 422, response.text)

    async def test_version_approval_preserves_original_and_rejects_stale(self):
        path = f"/api/projects/{self.pid}/versions"
        original = await self.client.post(path, headers=self.headers, json={"kind": "original", "text": "원본"})
        original_id = original.json()["id"]
        duplicate = await self.client.post(path, headers=self.headers, json={"kind": "original", "text": "덮어쓰기"})
        self.assertEqual(duplicate.status_code, 409)
        proposal = await self.client.post(path, headers=self.headers,
                                          json={"kind": "proposal", "text": "수정안", "base_id": original_id})
        proposal_id = proposal.json()["id"]
        approved = await self.client.post(f"{path}/{proposal_id}/approve", headers=self.headers)
        self.assertEqual(approved.status_code, 201, approved.text)
        self.assertEqual(approved.json()["inspection_status"], "not_run")
        stale = await self.client.post(f"{path}/{proposal_id}/approve", headers=self.headers)
        self.assertEqual(stale.status_code, 409)
        result = (await self.client.get(path, headers=self.headers)).json()
        self.assertEqual([v["kind"] for v in result], ["original", "proposal", "approved"])
        self.assertEqual(result[0]["text"], "원본")

    async def test_broker_disabled_and_missing_secret_do_not_call_network(self):
        with patch.dict(os.environ, {"PLC_ENABLE_BROKER": "false", "PLC_BROKER_SEALED": ""}):
            with patch("app.main.http.client.HTTPSConnection") as remote:
                response = await self.client.post("/api/chat/token", headers=self.headers)
                self.assertEqual(response.status_code, 503)
                remote.assert_not_called()
        with patch.dict(os.environ, {"PLC_ENABLE_BROKER": "true", "PLC_BROKER_SEALED": ""}):
            with patch("app.main.http.client.HTTPSConnection") as remote:
                response = await self.client.post("/api/chat/token", headers=self.headers)
                self.assertEqual(response.status_code, 503)
                remote.assert_not_called()

    async def test_broker_contract_with_mock_only(self):
        with patch.dict(os.environ, {"PLC_ENABLE_BROKER": "true", "PLC_BROKER_SEALED": "TEST-ONLY-NOT-A-REAL-SECRET"}):
            with patch("app.main.http.client.HTTPSConnection") as cls:
                connection = cls.return_value
                response = connection.getresponse.return_value
                response.status = 200
                response.read.return_value = b'{"token":"test-only-token"}'
                result = await self.client.post("/api/chat/token", headers=self.headers)
                self.assertEqual(result.json(), {"token": "test-only-token"})
                request = connection.request.call_args
                self.assertEqual(request.args, ("POST", "/api/directlineToken"))
                self.assertEqual(json.loads(request.kwargs["body"]), {"sealed": "TEST-ONLY-NOT-A-REAL-SECRET"})
                self.assertNotIn("headers", request.kwargs)
                self.assertEqual(result.headers["cache-control"], "no-store")
                retry = await self.client.post("/api/chat/token", headers=self.headers)
                self.assertEqual(retry.status_code, 429)

    async def test_page_and_no_external_script_loaded_by_default(self):
        page = await self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('lang="ko"', page.text)
        self.assertNotIn("cdn.botframework.com", page.text)
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        script = await self.client.get("/static/app.js")
        self.assertIn("/4.18.0/webchat.js", script.text)
        self.assertNotIn("TEST-ONLY-NOT-A-REAL-SECRET", script.text)


if __name__ == "__main__":
    unittest.main()
