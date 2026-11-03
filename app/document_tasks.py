"""Private PDF rendering and local OCR, bounded to one user-selected page."""

import base64
import importlib.util
import io
import json
import math
import os
import sys
import warnings
from pathlib import Path

from app.extractor import MAX_PAGES, MAX_TEXT

MAX_RENDER_PIXELS = 4_000_000
OCR_WARNING = (
    "로컬 OCR 문자 추출 결과입니다. 오인식·누락 가능성이 있어 원본 대조가 필요합니다. "
    "도면 기호·회로 연결·PLC 논리 및 AI 분석을 검증하지 않았습니다."
)


class DocumentError(Exception):
    def __init__(self, message, code=422):
        super().__init__(message)
        self.code = code


def ocr_configuration():
    directory = Path(os.environ.get("PLC_TESSDATA_DIR", str(Path(".local-ocr") / "tessdata")))
    installed = importlib.util.find_spec("tesserocr") is not None
    missing = [language for language in ("kor", "eng")
               if not (directory / (language + ".traineddata")).is_file()]
    if not installed:
        reason = "로컬 OCR 패키지가 없습니다. requirements-ocr.txt 설치가 필요합니다."
    elif missing:
        reason = "한국어·영어 언어 데이터 설정이 필요합니다: " + ", ".join(missing)
    else:
        reason = "패키지·한국어/영어 데이터가 설정됨. 실제 엔진 초기화는 OCR 실행 시 확인합니다."
    return {"status": "configured" if installed and not missing else "setup_required",
            "reason": reason, "languages": ["kor", "eng"]}


def dimensions(width, height, max_edge):
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise DocumentError("문서의 페이지 크기가 올바르지 않습니다.")
    scale = min(max_edge / max(width, height), math.sqrt(MAX_RENDER_PIXELS / (width * height)), 2.5)
    return max(1, int(width * scale)), max(1, int(height * scale)), scale


def page_image(path, page_number, max_edge, metadata_only=False):
    from PIL import Image, ImageOps

    if path.suffix.lower() == ".pdf":
        import pypdfium2

        with pypdfium2.PdfDocument(path) as document:
            count = len(document)
            if count > MAX_PAGES:
                raise DocumentError("PDF는 40페이지 이하만 지원합니다.")
            if not 1 <= page_number <= count:
                raise DocumentError("존재하지 않는 PDF 페이지입니다.", 404)
            if metadata_only:
                return None, count
            page = document[page_number - 1]
            try:
                _, _, scale = dimensions(*page.get_size(), max_edge)
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
        return image, count
    if page_number != 1:
        raise DocumentError("이미지는 1페이지만 지원합니다.", 404)
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise DocumentError("PDF 또는 PNG/JPG/JPEG 파일만 지원합니다.", 415)
    Image.MAX_IMAGE_PIXELS = 20_000_000
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as original:
            if original.width * original.height > 20_000_000:
                raise DocumentError("이미지 픽셀 제한을 초과했습니다.")
            if metadata_only:
                return None, 1
            image = ImageOps.exif_transpose(original).convert("RGB")
            width, height, _ = dimensions(image.width, image.height, max_edge)
            image.thumbnail((width, height))
    return image, 1


def process(mode, path, page):
    if mode == "metadata":
        _, count = page_image(path, page, 1600, metadata_only=True)
        return {"ok": True, "page_count": count}
    if mode not in ("preview", "ocr"):
        raise DocumentError("지원하지 않는 문서 처리 요청입니다.")
    if mode == "ocr":
        config = ocr_configuration()
        if config["status"] != "configured":
            raise DocumentError(config["reason"], 503)
    image, count = page_image(path, page, 2400 if mode == "ocr" else 1600)
    try:
        if mode == "preview":
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
            payload = buffer.getvalue()
            if len(payload) > 6 * 1024**2:
                raise DocumentError("페이지 미리 보기 이미지 크기 제한을 초과했습니다.")
            return {"ok": True, "page_count": count, "page": page,
                    "image": base64.b64encode(payload).decode("ascii")}
        from PIL import ImageOps
        import tesserocr

        directory = Path(os.environ.get("PLC_TESSDATA_DIR", str(Path(".local-ocr") / "tessdata"))).resolve()
        with tesserocr.PyTessBaseAPI(path=str(directory), lang="kor+eng", psm=tesserocr.PSM.SPARSE_TEXT) as engine:
            grayscale = ImageOps.autocontrast(ImageOps.grayscale(image))
            try:
                engine.SetImage(grayscale)
                text = engine.GetUTF8Text().strip()
            finally:
                grayscale.close()
        partial = len(text) > MAX_TEXT
        return {"ok": True, "page": page, "page_count": count,
                "status": "partial" if partial else ("extracted" if text else "no_text"),
                "text": text[:MAX_TEXT], "details": [
                    OCR_WARNING,
                    "최대 150,000자 제한으로 일부만 보관했습니다." if partial else
                    ("선택한 1페이지의 인식된 문자만 보관했습니다." if text else
                     "인식된 문자가 없습니다. 원본 해상도·방향·내용을 확인하세요."),
                ]}
    finally:
        image.close()


def main():
    try:
        if sys.platform != "win32":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024**2, 1024 * 1024**2))
            resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
        result = process(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]))
    except DocumentError as error:
        result = {"ok": False, "code": error.code, "error": str(error)}
    except ImportError:
        result = {"ok": False, "code": 503, "error": "문서 처리 의존성 설치가 필요합니다."}
    except Exception:
        # Native parser errors may contain private content or absolute paths.
        result = {"ok": False, "code": 422,
                  "error": "문서 렌더링 또는 로컬 OCR을 실행하지 못했습니다. 파일 상태·언어 데이터·엔진 설치를 확인하세요."}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
