import hashlib
import io
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.document_tasks import ocr_configuration
from app.main import create_app


def text_image(text="PLC INPUT X001"):
    image = Image.new("RGB", (1400, 500), "white")
    try:
        font = ImageFont.truetype("arial.ttf" if os.name == "nt" else "DejaVuSans.ttf", 64)
    except OSError:
        font = ImageFont.load_default(size=64)
    ImageDraw.Draw(image).text((70, 150), text, fill="black", font=font)
    return image


def image_bytes(image, format="PNG", **kwargs):
    buffer = io.BytesIO()
    image.save(buffer, format=format, **kwargs)
    return buffer.getvalue()


def scan_pdf():
    first, second = text_image(), text_image("PLC OUTPUT Y002")
    try:
        return image_bytes(first, "PDF", save_all=True, append_images=[second], resolution=150)
    finally:
        first.close()
        second.close()


class DocumentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name)
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 1)),
            base_url="http://localhost",
        )
        self.headers = {"X-Local-Session": (await self.client.get("/api/session")).json()["session"]}
        self.pid = (await self.client.post("/api/projects", headers=self.headers,
                                          json={"name": "문서 처리 검증"})).json()["id"]

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        self.temp.cleanup()

    async def upload(self, name, payload):
        response = await self.client.post(
            f"/api/projects/{self.pid}/files",
            headers={**self.headers, "Content-Type": "application/octet-stream", "X-Filename": quote(name)},
            content=payload,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return f"/api/projects/{self.pid}/files/{response.json()['id']}"

    async def test_real_pdf_pages_and_bounded_preview(self):
        payload = scan_pdf()
        path = await self.upload("scan.pdf", payload)
        metadata = await self.client.get(path + "/document", headers=self.headers)
        self.assertEqual(metadata.json()["page_count"], 2)
        pages = []
        for number in (1, 2):
            preview = await self.client.get(path + f"/preview?page={number}", headers=self.headers)
            self.assertEqual(preview.status_code, 200, preview.text[:100] if preview.status_code != 200 else "")
            self.assertEqual(preview.headers["content-type"], "image/png")
            self.assertEqual(preview.headers["cache-control"], "no-store")
            self.assertEqual(preview.headers["x-page-count"], "2")
            with Image.open(io.BytesIO(preview.content)) as image:
                self.assertLessEqual(max(image.size), 1600)
                self.assertLessEqual(image.width * image.height, 4_000_000)
            pages.append(preview.content)
        self.assertNotEqual(pages[0], pages[1])
        self.assertEqual((await self.client.get(path + "/preview?page=3", headers=self.headers)).status_code, 404)
        self.assertEqual((await self.client.get(path + "/preview?page=0", headers=self.headers)).status_code, 422)
        original = (await self.client.get(path + "/original", headers=self.headers)).content
        self.assertEqual(hashlib.sha256(original).hexdigest(), hashlib.sha256(payload).hexdigest())
        self.assertEqual((await self.client.get(path + "/ocr?page=1", headers=self.headers)).json(), [])

    async def test_image_preview_and_unsupported_type(self):
        image = text_image()
        try:
            path = await self.upload("image.png", image_bytes(image))
        finally:
            image.close()
        self.assertEqual((await self.client.get(path + "/preview", headers=self.headers)).status_code, 200)
        self.assertEqual((await self.client.get(path + "/preview?page=2", headers=self.headers)).status_code, 404)
        txt = await self.upload("code.txt", b"LD X001")
        for suffix in ("/preview", "/document"):
            self.assertEqual((await self.client.get(txt + suffix, headers=self.headers)).status_code, 415)
        self.assertEqual((await self.client.post(txt + "/ocr", headers=self.headers)).status_code, 415)

    async def test_project_scope_and_local_session_apply_to_all_document_routes(self):
        path = await self.upload("scope.pdf", scan_pdf())
        other = (await self.client.post("/api/projects", headers=self.headers,
                                       json={"name": "다른 프로젝트"})).json()["id"]
        other_path = path.replace(self.pid, other)
        for suffix in ("/document", "/preview", "/ocr"):
            self.assertEqual((await self.client.get(other_path + suffix, headers=self.headers)).status_code, 404)
            self.assertEqual((await self.client.get(path + suffix)).status_code, 403)
        self.assertEqual((await self.client.post(other_path + "/ocr", headers=self.headers)).status_code, 404)
        self.assertEqual((await self.client.post(path + "/ocr")).status_code, 403)

    async def test_missing_ocr_configuration_blocks_engine_without_fake_result(self):
        path = await self.upload("unconfigured.pdf", scan_pdf())
        with patch.dict(os.environ, {"PLC_TESSDATA_DIR": str(Path(self.temp.name) / "missing")}):
            with patch("app.main.subprocess.run") as worker:
                response = await self.client.post(path + "/ocr", headers=self.headers)
                self.assertEqual(response.status_code, 503)
                worker.assert_not_called()
        self.assertEqual((await self.client.get(path + "/ocr", headers=self.headers)).json(), [])

    async def test_preview_timeout_and_ocr_failure_do_not_replace_extraction(self):
        path = await self.upload("timeout.pdf", scan_pdf())
        original = (await self.client.get(path + "/original", headers=self.headers)).content
        extracted = (await self.client.get(path + "/extraction", headers=self.headers)).json()
        with patch("app.main.ocr_configuration", return_value={"status": "configured"}):
            with patch("app.main.subprocess.run", side_effect=subprocess.TimeoutExpired("document", 32)):
                self.assertEqual((await self.client.get(path + "/preview", headers=self.headers)).status_code, 422)
                self.assertEqual((await self.client.post(path + "/ocr", headers=self.headers)).status_code, 422)
        history = (await self.client.get(path + "/ocr", headers=self.headers)).json()
        self.assertEqual(history[0]["status"], "failed")
        self.assertEqual(history[0]["text"], "")
        self.assertIn("32", history[0]["details"][0])
        self.assertEqual((await self.client.get(path + "/extraction", headers=self.headers)).json(), extracted)
        self.assertEqual((await self.client.get(path + "/original", headers=self.headers)).content, original)
        self.assertEqual((await self.client.get(path + "/preview", headers=self.headers)).status_code, 200)

    @unittest.skipUnless(ocr_configuration()["status"] == "configured", "Optional local OCR package/languages not installed")
    async def test_real_local_ocr_selected_pdf_page_and_immutable_original(self):
        payload = scan_pdf()
        path = await self.upload("ocr.pdf", payload)
        extracted = (await self.client.get(path + "/extraction", headers=self.headers)).json()
        self.assertEqual(extracted["status"], "needs_ocr")
        result = await self.client.post(path + "/ocr?page=2", headers=self.headers)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["status"], "extracted")
        self.assertIn("OUTPUT", result.json()["text"])
        self.assertNotIn("INPUT", result.json()["text"])
        self.assertTrue(any("오인식" in detail for detail in result.json()["details"]))
        self.assertEqual(result.json()["page"], 2)
        self.assertEqual(result.json()["ai_delivery_status"], "disconnected")
        self.assertEqual((await self.client.get(path + "/ocr?page=1", headers=self.headers)).json(), [])
        self.assertEqual(len((await self.client.get(path + "/ocr?page=2", headers=self.headers)).json()), 1)
        self.assertEqual((await self.client.get(path + "/original", headers=self.headers)).content, payload)
        self.assertEqual((await self.client.get(path + "/extraction", headers=self.headers)).json(), extracted)
        second = await self.client.post(path + "/ocr?page=2", headers=self.headers)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertNotEqual(result.json()["id"], second.json()["id"])
        self.assertEqual(len((await self.client.get(path + "/ocr?page=2", headers=self.headers)).json()), 2)
        with closing(sqlite3.connect(Path(self.temp.name) / "local.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ocr_results").fetchone()[0], 2)
        files = (await self.client.get(f"/api/projects/{self.pid}/files", headers=self.headers)).json()
        self.assertEqual(files[0]["ocr"], {"status": "extracted", "page": 2})

    @unittest.skipUnless(ocr_configuration()["status"] == "configured", "Optional local OCR package/languages not installed")
    async def test_blank_image_returns_no_text_not_successful_analysis(self):
        image = Image.new("RGB", (800, 400), "white")
        try:
            path = await self.upload("blank.png", image_bytes(image))
        finally:
            image.close()
        response = await self.client.post(path + "/ocr", headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "no_text")
        self.assertEqual(response.json()["text"], "")
        self.assertEqual(response.json()["ai_delivery_status"], "disconnected")

    @unittest.skipUnless(ocr_configuration()["status"] == "configured" and
                         Path("C:\\Windows\\Fonts\\malgun.ttf").is_file(), "Korean test font not installed")
    async def test_real_korean_text_recognition(self):
        image = Image.new("RGB", (1400, 800), "white")
        font = ImageFont.truetype("C:\\Windows\\Fonts\\malgun.ttf", 76)
        ImageDraw.Draw(image).text((80, 120), "전원 입력\n비상 정지", fill="black", font=font, spacing=70)
        try:
            path = await self.upload("korean.png", image_bytes(image))
        finally:
            image.close()
        response = await self.client.post(path + "/ocr", headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        text = response.json()["text"].replace(" ", "").replace("\n", "")
        self.assertIn("전원입력", text)
        self.assertIn("비상", text)
        self.assertTrue(any("오인식" in detail for detail in response.json()["details"]))


if __name__ == "__main__":
    unittest.main()
