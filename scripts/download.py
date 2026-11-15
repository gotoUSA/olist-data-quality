"""Olist 원본 CSV를 data/raw/에 받는다.

원본은 CC BY-NC-SA 4.0이라 저장소에 올리지 않고, 이 스크립트로만 받는다.
"""
import io
import urllib.request
import zipfile
from pathlib import Path

URL = "https://www.kaggle.com/api/v1/datasets/download/olistbr/brazilian-ecommerce"
RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(URL, timeout=120) as resp:
        body = resp.read()
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        for name in zf.namelist():
            if not name.endswith(".csv") or "/" in name or ".." in name:
                continue
            (RAW / name).write_bytes(zf.read(name))
            print(name)


if __name__ == "__main__":
    main()
