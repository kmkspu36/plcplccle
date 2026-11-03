"""Real Chromium checks against a disposable loopback server, never real agents."""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from test_documents import image_bytes, scan_pdf, text_image
PAGES = [
    ("dashboard", "대시보드"), ("studio", "AI 엔지니어링 작업실"),
    ("drawings", "PLC 도면 분석"), ("ladder", "PLC 작화 작성"),
    ("code", "PLC 코드 생성"), ("inspection", "오류 검사"),
    ("correction", "오류 자동 수정"), ("validation", "PLC 검증"),
    ("reports", "보고서"), ("projects", "프로젝트 관리"),
    ("history", "작업 이력"), ("settings", "설정"),
]


def main():
    with tempfile.TemporaryDirectory(prefix="plc-ui-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        environment = dict(os.environ, PLC_ENABLE_BROKER="false", PLC_DATA_DIR=directory)
        environment.pop("PLC_BROKER_SEALED", None)
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-proxy-headers", "--no-access-log", "--log-level", "error"],
            cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            for attempt in range(60):
                if server.poll() is not None:
                    raise RuntimeError(server.stderr.read().decode("utf-8", errors="replace"))
                try:
                    with urllib.request.urlopen(base, timeout=1) as response:
                        if b"<title>PLC Engineering AI</title>" in response.read():
                            break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("Disposable preview did not start.")
            run_browser(base, directory)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            server.stderr.close()


def run_browser(base, directory):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        errors = []
        outside = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def network_boundary(route):
            if route.request.url.startswith(base + "/"):
                route.continue_()
            else:
                outside.append(route.request.url)
                route.abort()

        context.route("**/*", network_boundary)

        def navigate(slug, title):
            page.goto(base + "/#" + slug)
            expect(page.locator("h1")).to_have_text(title)
            page.wait_for_function("!state.busy")

        for width, height in ((1440, 1000), (768, 1024), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            for slug, title in PAGES:
                navigate(slug, title)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, slug, "page overflow")
                if slug == "studio":
                    expect(page.locator("#connect-chat")).to_be_disabled()
                if slug == "validation":
                    assert page.get_by_text("미수행", exact=True).count() == 5
            if width == 390:
                page.locator("#menu-toggle").click()
                expect(page.locator("#menu-toggle")).to_have_attribute("aria-expanded", "true")
                page.locator("#navigation").get_by_role("link", name="대시보드", exact=True).click()
                expect(page.locator("h1")).to_have_text("대시보드")
                expect(page.locator("#menu-toggle")).to_have_attribute("aria-expanded", "false")
        print("PASS: 12 screens x PC/tablet/mobile, no document overflow, mobile navigation, disabled AI.")

        page.set_viewport_size({"width": 1440, "height": 1000})
        navigate("projects", "프로젝트 관리")
        page.locator("#new-name").fill("UI 검증 프로젝트 <확인>")
        page.locator("#new-gx").select_option(label="GX Works3")
        page.locator("#new-requirements").fill("브라우저 시험용 요구사항 · 실제 설비 사용 금지")
        page.locator("#new-project").get_by_role("button", name="프로젝트 생성", exact=True).click()
        expect(page.locator("#edit-name")).to_have_value("UI 검증 프로젝트 <확인>")
        page.reload()
        expect(page.locator("#edit-name")).to_have_value("UI 검증 프로젝트 <확인>")

        navigate("drawings", "PLC 도면 분석")
        payload = "종류,주소\r\n입력,X001\r\n정지,X002\r\n".encode("utf-8-sig")
        page.locator("#upload-file").set_input_files({"name": "검증용.csv", "mimeType": "text/csv", "buffer": payload})
        page.get_by_role("button", name="업로드 및 무료 추출", exact=True).click()
        expect(page.locator(".file-name")).to_contain_text("검증용.csv")
        page.get_by_role("button", name="추출 보기", exact=True).click()
        expect(page.locator("#file-detail pre")).to_contain_text("X001")
        with page.expect_download() as downloaded:
            page.get_by_role("button", name="원본", exact=True).click()
        assert Path(downloaded.value.path()).read_bytes() == payload
        page.locator("#upload-file").set_input_files({"name": "bad.pdf", "mimeType": "application/pdf", "buffer": b"not pdf"})
        page.get_by_role("button", name="업로드 및 무료 추출", exact=True).click()
        expect(page.locator("#notice")).to_contain_text("PDF 시그니처")
        assert page.locator(".file-name").count() == 1
        print("PASS: browser project persistence, real upload/extraction/download byte preservation, invalid file failure.")

        pdf_payload = scan_pdf()
        page.locator("#upload-file").set_input_files({"name": "pages.pdf", "mimeType": "application/pdf", "buffer": pdf_payload})
        page.get_by_role("button", name="업로드 및 무료 추출", exact=True).click()
        pdf_row = page.locator("tr").filter(has_text="pages.pdf")
        expect(pdf_row).to_be_visible()
        pdf_row.get_by_role("button", name="미리 보기 / OCR", exact=True).click()
        expect(page.locator("#document-page")).to_have_value("1")
        expect(page.get_by_role("button", name="이전 페이지", exact=True)).to_be_disabled()
        expect(page.locator("#file-detail")).to_contain_text("이 페이지는 OCR 미실행")
        page.get_by_role("button", name="다음 페이지", exact=True).click()
        expect(page.locator("#document-page")).to_have_value("2")
        expect(page.get_by_role("button", name="다음 페이지", exact=True)).to_be_disabled()
        expect(page.locator("#file-detail .preview-image")).to_be_visible()
        assert page.locator("#file-detail .preview-image").evaluate("(img) => img.complete && img.naturalWidth > 0")
        if page.locator("#run-local-ocr").is_enabled():
            page.locator("#run-local-ocr").click()
            expect(page.locator("#ocr-text")).to_contain_text("OUTPUT", timeout=25000)
            recognized = page.locator("#ocr-text").inner_text()
            page.locator("#document-page").select_option("1")
            expect(page.locator("#file-detail")).to_contain_text("이 페이지는 OCR 미실행")
            page.locator("#document-page").select_option("2")
            expect(page.locator("#ocr-text")).to_have_text(recognized)
            with page.expect_download() as downloaded:
                page.locator("tr").filter(has_text="pages.pdf").get_by_role("button", name="원본", exact=True).click()
            assert Path(downloaded.value.path()).read_bytes() == pdf_payload
            print("PASS: real PDF preview/navigation, selected-page local OCR, persisted result and immutable original.")
        else:
            print("PASS: PDF preview/navigation; optional OCR is disabled with setup guidance (engine/languages missing).")
        screenshots = os.environ.get("PLC_TEST_SCREENSHOTS")
        if screenshots:
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(Path(screenshots) / "ocr-pdf-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        expect(page.locator("#document-page")).to_be_visible()
        if screenshots:
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(Path(screenshots) / "ocr-pdf-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})

        image = text_image()
        try:
            png = image_bytes(image)
        finally:
            image.close()
        page.locator("#upload-file").set_input_files({"name": "input.png", "mimeType": "image/png", "buffer": png})
        page.get_by_role("button", name="업로드 및 무료 추출", exact=True).click()
        image_row = page.locator("tr").filter(has_text="input.png")
        image_row.get_by_role("button", name="미리 보기 / OCR", exact=True).click()
        expect(page.locator("#document-page")).to_have_value("1")
        expect(page.get_by_role("button", name="다음 페이지", exact=True)).to_be_disabled()
        if page.locator("#run-local-ocr").is_enabled():
            page.locator("#run-local-ocr").click()
            expect(page.locator("#ocr-text")).to_contain_text("INPUT", timeout=25000)
            expect(page.locator("#file-detail")).to_contain_text("오인식·누락")
            print("PASS: real local image OCR through browser controls.")

        navigate("code", "PLC 코드 생성")
        original = "LD X000\nOUT Y000"
        changed = "LD X001\nOUT Y000"
        page.locator("#code-editor").fill(original)
        page.get_by_role("button", name="원본으로 저장", exact=True).click()
        expect(page.get_by_role("button", name="수정안으로 저장", exact=True)).to_be_visible()
        page.locator("#code-editor").fill(changed)
        page.get_by_role("button", name="수정안으로 저장", exact=True).click()
        expect(page.get_by_role("button", name="텍스트 보기", exact=True)).to_have_count(2)
        navigate("correction", "오류 자동 수정")
        page.locator("#proposal-select").select_option(index=1)
        expect(page.locator("#proposal-text")).to_have_text(changed)
        expect(page.locator("#approve-version")).to_be_disabled()
        page.locator("#approve-check").check()
        page.locator("#approve-version").click()
        expect(page.get_by_text("비교할 수정안이 없습니다", exact=True)).to_be_visible()
        navigate("code", "PLC 코드 생성")
        expect(page.locator("#code-editor")).to_have_value(changed)
        expect(page.get_by_role("button", name="텍스트 보기", exact=True)).to_have_count(3)
        page.get_by_role("button", name="텍스트 보기", exact=True).first.click()
        expect(page.locator("#version-detail")).to_have_text(original)
        print("PASS: browser original/proposal/approval, explicit approval gate, original retained, no inspection success.")

        page.set_viewport_size({"width": 390, "height": 844})
        for slug, title in PAGES:
            navigate(slug, title)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (390, slug, "populated page overflow")
        print("PASS: populated project across all 12 mobile screens, including file table and code versions.")
        assert errors == [], errors
        assert outside == [], outside
        print("PASS: no JavaScript page errors and no external network requests.")
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
