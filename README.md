# Olist 데이터 품질 진단 (학습 프로젝트)

Kaggle Olist 브라질 이커머스 공개 데이터(2016-09~2018-10, 주문 99,441건)로
결측·중복·이상값을 진단하고, 검증 기준을 먼저 정한 뒤 자동 검사로 만들고,
정제 전후 차이를 숫자와 그림으로 남기는 학습 프로젝트입니다.

## 순서
1. 검증 기준을 문서로 먼저 정한다 — `docs/CRITERIA.md`
2. 기준을 자동 검사로 만든다 — `checks/`
3. 정제 전후 차이를 기록한다 — `docs/RECORD.md`

## 실행
```
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
docker compose up -d                      # PostgreSQL 16, localhost:5440
.venv/Scripts/python scripts/download.py  # 원본 CSV → data/raw/ (저장소에 올리지 않음)
.venv/Scripts/python scripts/load_raw.py  # raw 스키마에 전 열 TEXT로 적재
```

## 데이터 라이선스
원본은 CC BY-NC-SA 4.0입니다. 원본 CSV는 이 저장소에 넣지 않고 `scripts/download.py`로 받습니다.
