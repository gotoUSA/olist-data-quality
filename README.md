# Olist 데이터 품질 진단 (학습 프로젝트)

Kaggle Olist 브라질 이커머스 공개 데이터(2016-09~2018-10, 주문 99,441건)로
결측·중복·이상값을 진단하고, 검증 기준을 먼저 정한 뒤 자동 검사로 만들고,
배송 지연과 리뷰 점수의 관계를 분석한 학습 프로젝트입니다. 결과와 한계는 `docs/RECORD.md`에 있습니다.

## 순서
1. 검증 기준을 문서로 먼저 정한다 — `docs/CRITERIA.md`
2. 기준을 자동 검사로 만든다 — `checks/`
3. 오류 주입 테스트와 replay 백테스트로 검사를 확인하고, 미리 정한 방법으로 배송 지연과 리뷰의 관계를 분석한다 — `tests/`, `checks/replay.py`, `analysis/`
4. 검사가 찾은 것, 계획을 바꾼 발견, 결과와 한계를 기록한다 — `docs/RECORD.md`

## 실행
```
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
docker compose up -d                      # PostgreSQL 16, localhost:5440
.venv/Scripts/python scripts/download.py  # 원본 CSV → data/raw/ (저장소에 올리지 않음)
.venv/Scripts/python scripts/load_raw.py  # raw 스키마에 전 열 TEXT로 적재
.venv/Scripts/python -m checks.run        # 검사 C0~C18(C16은 replay), 관문 실패 시 종료 코드 1
.venv/Scripts/python -m pytest tests -q   # 오류 주입 테스트(raw를 inj 스키마로 복사해 오류를 넣고 확인)
.venv/Scripts/python -m checks.replay     # replay 백테스트 → reports/replay.md
.venv/Scripts/python -m analysis.run      # 분석 → reports/analysis.md
```

## 데이터 라이선스
원본은 CC BY-NC-SA 4.0입니다. 원본 CSV는 이 저장소에 넣지 않고 `scripts/download.py`로 받습니다.
