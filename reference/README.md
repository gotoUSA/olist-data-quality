# 보충 참조표

원본 CSV는 고치지 않고, 빠진 참조 값만 여기에 보충한다.

| 파일 | 내용 | 근거 |
|---|---|---|
| `category_translation_extra.csv` | 원본 번역표(`product_category_name_translation.csv`)에 없는 카테고리 2개의 영어 이름 | 2026-11-13. 포르투갈어 단어를 그대로 옮김(portáteis = portable, cozinha = kitchen, preparadores de alimentos = food preparers). `pc_gamer`는 원래 영어 표기 |

검사 C17은 원본 번역표 + 이 보충표에 없는 카테고리가 하나라도 나오면 실패한다.
| `brazil_regions.csv` | 브라질 27개 주(연방구 포함) → 5개 권역(Norte·Nordeste·Centro-Oeste·Sudeste·Sul) | 2026-11-13. 브라질 통계청(IBGE) 공식 권역 구분. 층화 민감도 분석에 씀 |
