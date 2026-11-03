"""Download only the two pinned official Tesseract language files, never user data."""

import hashlib
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = {
    "eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "kor": "6b85e11d9bbf07863b97b3523b1b112844c43e713df8b66418a081fd1060b3b2",
}


def main():
    directory = ROOT / ".local-ocr" / "tessdata"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for language, expected in MODELS.items():
        target = directory / (language + ".traineddata")
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            print(f"{language}: verified, already installed")
            continue
        url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/{language}.traineddata"
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read(8 * 1024**2 + 1)
        if len(payload) > 8 * 1024**2 or hashlib.sha256(payload).hexdigest() != expected:
            raise RuntimeError(f"{language}: official language file checksum did not match; nothing replaced.")
        temporary = target.with_suffix(".download")
        try:
            with temporary.open("xb") as file:
                os.chmod(temporary, 0o600)
                file.write(payload)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"{language}: downloaded official 4.1.0 data, SHA-256 verified")
    print("Local Korean/English OCR data is ready. Restart your local server if needed.")


if __name__ == "__main__":
    main()
