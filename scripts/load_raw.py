"""data/raw/*.csv를 PostgreSQL raw 스키마에 그대로 적재한다.

모든 열을 TEXT로 둔다. 형 변환에 실패하는 값(빈 문자열, 잘못된 날짜 등)도
적재 단계에서 버려지지 않게 하고, 그런 값은 검사(checks/)에서 센다.
"""
import csv
import os
from pathlib import Path

import psycopg

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
DSN = os.environ.get("OLIST_DSN", "postgresql://olist:olist@localhost:5440/olist")


def table_name(path: Path) -> str:
    name = path.stem.removeprefix("olist_").removesuffix("_dataset")
    return name


def main() -> None:
    with psycopg.connect(DSN) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for path in sorted(RAW.glob("*.csv")):
            table = table_name(path)
            with path.open(encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f))
            cols = ", ".join(f'"{c}" TEXT' for c in header)
            conn.execute(f'DROP TABLE IF EXISTS raw."{table}"')
            conn.execute(f'CREATE TABLE raw."{table}" ({cols})')
            with path.open("rb") as f, conn.cursor().copy(
                f'COPY raw."{table}" FROM STDIN WITH (FORMAT csv, HEADER true)'
            ) as copy:
                first = f.read(3)
                if first != b"\xef\xbb\xbf":  # BOM 제거
                    copy.write(first)
                while chunk := f.read(1 << 20):
                    copy.write(chunk)
            n = conn.execute(f'SELECT count(*) FROM raw."{table}"').fetchone()[0]
            print(f"{table:35s} {n:>9,d}")


if __name__ == "__main__":
    main()
