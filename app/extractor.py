"""Bounded local preprocessing, not PLC analysis. Invoked in a disposable process."""

import csv
import io
import json
import sys
import warnings
import zipfile
from pathlib import Path

MAX_TEXT = 150_000
MAX_CELLS = 10_000
MAX_PAGES = 40


class InvalidFile(Exception):
    pass


def decode_text(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "cp949")
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if any(ord(c) < 32 and c not in "\r\n\t" for c in text):
            raise InvalidFile("바이너리 또는 제어 문자가 포함되어 있습니다.")
        return text, encoding
    raise InvalidFile("UTF-8, BOM UTF-16 또는 CP949 텍스트가 필요합니다.")


def extract(path, suffix):
    raw = path.read_bytes()
    text = ""
    tables = []
    details = []
    partial = False
    status = "extracted"
    if suffix in (".txt", ".csv"):
        text, encoding = decode_text(raw)
        details.append(f"인코딩: {encoding}. 문자열·선행 0·수식은 평가하지 않습니다.")
        partial = len(text) > MAX_TEXT
        text = text[:MAX_TEXT]
        if suffix == ".csv":
            csv.field_size_limit(MAX_TEXT)
            sample = text[:4096]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows = []
            cell_count = 0
            for row in csv.reader(io.StringIO(text, newline=""), dialect):
                if len(rows) >= 2000 or cell_count + len(row) > MAX_CELLS:
                    partial = True
                    break
                rows.append(row)
                cell_count += len(row)
            tables.append({"name": "CSV", "rows": rows})
    elif suffix == ".xlsx":
        if not raw.startswith(b"PK\x03\x04"):
            raise InvalidFile("XLSX ZIP 시그니처가 올바르지 않습니다.")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(e.file_size for e in entries) > 80 * 1024**2:
                raise InvalidFile("압축 해제 크기 또는 항목 수 제한을 초과했습니다.")
            if any(e.flag_bits & 1 or e.file_size > 30 * 1024**2
                   or e.file_size / max(1, e.compress_size) > 200 for e in entries):
                raise InvalidFile("암호화 또는 과도한 압축률의 XLSX는 지원하지 않습니다.")
            names = archive.namelist()
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise InvalidFile("Excel 통합 문서 구조가 아닙니다.")
            if any("vbaproject" in name.lower() or "externallinks/" in name.lower()
                   for name in names):
                raise InvalidFile("매크로 또는 외부 연결이 포함된 파일은 허용하지 않습니다.")
        import openpyxl

        source = path.open("rb")
        book = openpyxl.load_workbook(source, read_only=True, data_only=False, keep_links=False)
        try:
            cells = 0
            chars = 0
            for sheet in book.worksheets[:20]:
                rows = []
                max_row = min(sheet.max_row or 2001, 2001)
                max_col = min(sheet.max_column or 101, 101)
                for values in sheet.iter_rows(max_row=max_row, max_col=max_col, values_only=True):
                    if len(rows) >= 2000 or cells + len(values) > MAX_CELLS:
                        partial = True
                        break
                    row = ["" if value is None else str(value) for value in values]
                    chars += sum(len(value) for value in row)
                    if chars > MAX_TEXT:
                        partial = True
                        break
                    rows.append(row)
                    cells += len(row)
                tables.append({"name": sheet.title, "rows": rows})
                if cells >= MAX_CELLS or chars > MAX_TEXT:
                    partial = True
                    break
            if len(book.worksheets) > 20 or any((s.max_column or 0) > 101 for s in book.worksheets):
                partial = True
            text = "\n\n".join(t["name"] + "\n" + "\n".join("\t".join(r) for r in t["rows"])
                               for t in tables)
        finally:
            book.close()
            source.close()
        details.append("셀 값과 수식 문자열만 읽었습니다. 수식 실행·PLC 의미 해석은 하지 않습니다.")
    elif suffix == ".pdf":
        if not raw.startswith(b"%PDF-"):
            raise InvalidFile("PDF 시그니처가 올바르지 않습니다.")
        import pdfplumber

        with pdfplumber.open(path) as document:
            if len(document.pages) > MAX_PAGES:
                raise InvalidFile("PDF는 40페이지 이하만 허용합니다.")
            pages = []
            chars = 0
            blank_pages = 0
            table_cells = 0
            for index, page in enumerate(document.pages, 1):
                content = page.extract_text() or ""
                if not content.strip():
                    blank_pages += 1
                remaining = max(0, MAX_TEXT - chars)
                pages.append(f"[페이지 {index}]\n{content[:remaining]}")
                chars += len(content)
                for table in page.extract_tables()[:10]:
                    rows = []
                    for row in table:
                        values = [value or "" for value in row]
                        if table_cells + len(values) > MAX_CELLS:
                            partial = True
                            break
                        chars += sum(len(value) for value in values)
                        if chars > MAX_TEXT:
                            partial = True
                            break
                        table_cells += len(values)
                        rows.append(values)
                    tables.append({"name": f"페이지 {index}", "rows": rows})
                    if len(text) > MAX_TEXT:
                        partial = True
                    if partial:
                        break
                if chars > MAX_TEXT:
                    partial = True
                    break
            text = "\n\n".join(pages)
            details.append(f"PDF {len(document.pages)}페이지. 텍스트 없는 페이지: {blank_pages}.")
            if blank_pages:
                status = "needs_ocr" if blank_pages == len(document.pages) else "partial"
                details.append("스캔 영역은 OCR 미구현으로 확인 필요. 기호·회로 연결은 판독하지 않았습니다.")
    else:
        signatures = {
            ".png": b"\x89PNG\r\n\x1a\n",
            ".jpg": b"\xff\xd8\xff",
            ".jpeg": b"\xff\xd8\xff",
        }
        if suffix not in signatures or not raw.startswith(signatures[suffix]):
            raise InvalidFile("이미지 시그니처와 확장자가 일치하지 않습니다.")
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = 20_000_000
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                expected = "PNG" if suffix == ".png" else "JPEG"
                if image.format != expected or image.width * image.height > 20_000_000:
                    raise InvalidFile("이미지 형식 또는 2천만 픽셀 제한을 확인하세요.")
                details.append(f"이미지 {image.width} × {image.height}px. 파일 구조 확인.")
                image.verify()
        status = "needs_ocr"
        details.append("로컬 OCR 미구현·텍스트 미추출. 도면 기호·연결 관계 및 AI 분석은 확인 필요.")
    if not raw:
        raise InvalidFile("빈 파일은 업로드할 수 없습니다.")
    if partial:
        status = "partial"
        details.append("추출 자원 제한으로 일부만 읽었습니다. 원본은 그대로 보관됩니다.")
    return {"valid": True, "status": status, "text": text[:MAX_TEXT],
            "tables": tables, "details": details}


def main():
    try:
        # UNIX adds an address-space ceiling. Windows still has a process timeout,
        # archive/pixel/page limits, but is not an OS-level parser sandbox.
        if sys.platform != "win32":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
            resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        result = extract(Path(sys.argv[1]), sys.argv[2])
    except InvalidFile as error:
        result = {"valid": False, "error": str(error)}
    except ImportError:
        result = {"valid": False, "dependency_missing": True,
                  "error": "필수 파서 의존성이 없습니다. requirements.txt를 설치하고 다시 업로드하세요."}
    except Exception:
        # Raw parser errors can contain private paths or document content.
        result = {"valid": False, "error": "파일을 안전하게 읽을 수 없습니다. 손상·암호화·형식을 확인하세요."}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
