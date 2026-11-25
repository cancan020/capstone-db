# PostgreSQL과 레이더 수신 API

레이더가 생성한 상세 이벤트와 주기별 요약을 PostgreSQL에 저장하고, 가중평균(`valid_sample_count`) 기반 일일 집계·14일 주간 건강 리포트와 14일·365일 보관 삭제 배치를 제공합니다. [ERD와 데이터 사전](docs/PostgreSQL_ERD_데이터사전.md)에서 테이블 관계와 필드를 확인할 수 있습니다.

## 새 로컬 DB 준비

pgAdmin에서 `capstone` 데이터베이스를 만든 다음 Query Tool에서 아래 SQL 파일을 순서대로 한 번씩 실행합니다.

1. [`sql/000_schema.sql`](sql/000_schema.sql) (빈 DB 초기 테이블)
2. [`sql/001_message_dedup.sql`](sql/001_message_dedup.sql) (`source_id`, `message_id` 재전송 중복 방지)
3. [`sql/002_summary_valid_sample_count.sql`](sql/002_summary_valid_sample_count.sql) (`vital_summaries.valid_sample_count` 추가)
4. [`sql/003_daily_weekly_retention.sql`](sql/003_daily_weekly_retention.sql) (`daily_summaries`, `weekly_reports` 및 복합 인덱스)
5. [`sql/004_daily_summary_pruned_lock.sql`](sql/004_daily_summary_pruned_lock.sql) (이전 버전 `003` 적용 DB에 `detail_pruned_at` 추가, 멱등 실행)

새 DB의 첫 시연 대상자를 등록합니다. API 실행기는 기본적으로 대상자 1번을 사용합니다.

```sql
INSERT INTO public.subjects (display_name) VALUES ('[시연] 기본 대상자');
```

## API 실행

PowerShell에서 이 저장소 폴더를 열고 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_vital_api.py
```

프롬프트에 로컬 PostgreSQL 비밀번호를 입력합니다. 접속 기본값은 호스트 `127.0.0.1`, 포트 `5432`, DB 이름 `capstone`, 사용자 `postgres`입니다. 환경 변수 `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`로 변경할 수 있습니다. 시연 대상자 번호는 `VITAL_DEFAULT_SUBJECT_ID`로 지정할 수 있습니다.

[API 테스트 화면](http://127.0.0.1:8000/docs)에서 아래 경로를 실행할 수 있습니다.

- `POST /api/v1/vital-events`: `DETAILED_EVENT_LOG` 수신 저장
- `POST /api/v1/vital-summary`: `PERIODIC_SUMMARY_STATS` 수신 저장 (`valid_sample_count` 지원)
- `GET /api/v1/summary-report`: `vital_summaries`(`valid_sample_count` 가중평균) 및 마감된 `daily_summaries` 기반 14일 비교 리포트
- `GET /api/v1/weekly-reports`: `weekly_reports`에 보관된 주간 건강 리포트(365일 보관) 조회
- `GET /api/v1/care-subjects`, `GET /api/v1/care-report`, `GET /api/v1/breathing-report`: 시연 대상자 및 정오 시연 호흡수 조회

동일한 `source_id`와 `message_id`로 같은 요청을 재전송하면 기존 기록 번호가 반환되며, 같은 ID로 내용을 바꾸면 `409`가 반환됩니다.

## 실측 일일 집계 및 14일·365일 보관 삭제 배치

```powershell
# 실측 14일 집계 + 주간 리포트 저장
.\.venv\Scripts\python.exe summary_report.py --subject-id 1 --report-date 2026-11-23 --aggregate --save-report

# 14일/365일 만료 대상 미리보기 (기본 dry-run: DB 변경/삭제 없음)
.\.venv\Scripts\python.exe retention_batch.py --reference-date 2026-11-25

# 실제 반영 (REPEATABLE READ 스냅샷, 이번 배치에서 집계된 PK 중 주간 리포트가 존재하는 행만 삭제 후 detail_pruned_at 마감 잠금)
.\.venv\Scripts\python.exe retention_batch.py --reference-date 2026-11-25 --generate-weekly --apply
```

## 시연 대상자 등록

가상 대상자 4명의 기록으로 조회 API를 시험하려면 대상자 1번을 등록한 뒤 실행합니다.

```powershell
.\.venv\Scripts\python.exe seed_care_demo.py
```

## API 및 배치 자동 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
