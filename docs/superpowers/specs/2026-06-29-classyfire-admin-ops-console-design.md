# ClassyFire 운영 admin 콘솔 설계

- **날짜**: 2026-06-29
- **상태**: 승인됨 (구현 계획 대기)
- **브랜치**: `feat/admin-ops-console`
- **갱신(2026-06-30)**: 시드리스 전환으로 `SeedImport` 모델/admin이 제거됨 — 본문의 `SeedImport` 등록 항목은 더 이상 유효하지 않습니다(나머지 설계는 유효).

## 1. 배경 / 문제

`django.contrib.admin`은 `INSTALLED_APPS`에 있고 `config/urls.py`의 `admin/` URL도
연결돼 있지만, 프로젝트 앱(`apps/*`) 중 `admin.py`를 가진 앱이 하나도 없다. 그래서
Django admin에 등록된 프로젝트 모델이 없고, 운영자가 admin에서 `BatchJob`,
`RemoteTask`, `SystemEvent`, 분류 캐시 등을 **조회하거나 운영 조작할 방법이 없다**.
"기본 admin만 켜져 있는" 상태다.

목표: admin을 **읽기전용 운영/디버깅 콘솔**로 만들고, 의도된 운영 동작만 안전한
액션으로 노출한다.

## 2. 목표 / 비목표

### 목표
- 7개 앱의 모든 업무 모델(~20개)을 admin에 등록한다.
- 모든 모델을 **읽기전용**으로 등록한다(추가/수정/삭제 불가, 조회·검색·필터만).
- 검증된 기존 운영 로직을 재사용하는 **큐레이션된 운영 액션**만 노출한다.
- 모든 운영 액션은 행위자(`request.user`)를 기록한 `SystemEvent` 감사 row를 남긴다.
- status / state / provider / principal / InChIKey / job UUID로 빠르게 검색·필터한다.

### 비목표 (Out of scope)
- 슈퍼유저/스태프 계정 생성 — `manage.py createsuperuser`로 별도 처리(접속 전제조건).
- admin URL IP 화이트리스트 등 추가 네트워크 하드닝 — 별도 보안 작업.
- Django 기본 User/Group admin 커스터마이징 — 기본 그대로 둔다.
- 인라인(예: BatchJob 안에 BatchItem 인라인) — 대용량 위험으로 두지 않는다.

## 3. 핵심 안전 모델

다수 테이블은 **append-only / immutable 계약**이다:
`ClassificationResult`, `ClassyfireLive`, `ClassificationHistory`, `SystemEvent`,
`RemoteAttempt`, `SourceArtifact`, `AuditSignature`, `ClassyfireSeed`,
`RemoteBudgetLedger` 등. admin에서 이 행들을 편집/삭제하면 불변 계약과 감사 무결성이
깨진다. 따라서 **전 모델 읽기전용**을 기본으로 하고, 상태 변경은 오직 명시적으로
설계된 운영 액션을 통해서만, 그것도 기존 검증 로직을 재사용해 수행한다.

## 4. 아키텍처 / 파일 구성

`admin.py`는 Django가 앱별로 자동 탐색하므로 `INSTALLED_APPS`/URL 변경은 불필요하다.
새로 추가할 파일:

```
apps/core/admin_base.py        # ReadOnlyModelAdmin 믹스인 (공통 안전장치)
apps/core/admin.py             # LoginThrottle
apps/batches/admin.py          # BatchJob(+action), BatchItem, BatchQuotaEvent, ExportArtifact
apps/remote/admin.py           # Remote* 13개 (Task/ProviderControl/Submission에 action)
apps/classifications/admin.py  # ClassificationResult, ClassyfireLive, ClassyfireSeed,
                               #   ClassificationHistory, SourceArtifact
apps/operations/admin.py       # SeedImport, SystemEvent, AuditSignature
apps/compounds/admin.py        # CompoundStructure, CompoundUsage
apps/chemont/admin.py          # ChemOntNode
```

신규/수정 서비스 함수(액션 재사용용):
- `reset_provider_auth(provider, actor) -> SystemEvent` — **신규 공유 함수**.
  `apps/remote`에 둔다(provider 로직은 remote 앱 소유). 기존
  `reset_provider_auth` management command가 이 함수를 호출하도록 리팩터한다.
  현재 command에 **누락된 SystemEvent 감사 로깅을 이 함수에 추가**한다.
- `fail_unknown_submission(submission, actor) -> None` — **신규 공유 함수**,
  `apps/remote`에 둔다. `resolve_submission` command의 `_mark_failed`
  로직(상태 FAILED 전이 + `RemoteSubmissionMember` 활성 예약 해제를 한
  트랜잭션에서 수행)을 추출해 command와 admin 액션이 공유한다. + SystemEvent 감사.
- 기존 그대로 재사용: `apps/operations/admin_views.py`의
  `pause_remote`, `resume_remote`, `is_remote_paused`, `retry_failed_tasks`;
  `apps/batches/services.py`의 `recompute_job_counters`.

## 5. ReadOnlyModelAdmin 믹스인 계약

`apps/core/admin_base.py`의 `ReadOnlyModelAdmin(admin.ModelAdmin)`:
- `has_add_permission(request) -> False`
- `has_change_permission(request, obj=None) -> False`
- `has_delete_permission(request, obj=None) -> False`
- `has_view_permission` 은 기본값(스태프면 조회 가능) 유지.
- `show_full_result_count = False` — 대용량 테이블에서 `COUNT(*)` 회피.
- `get_readonly_fields()` 가 모델의 모든 구체 필드를 반환 — detail 화면도 읽기전용.

운영 액션이 붙는 ModelAdmin도 이 믹스인을 상속한다. 액션은 admin **action**(또는
체인지리스트 커스텀 뷰)일 뿐 row 편집 권한과 무관하므로, 읽기전용을 유지하면서도
액션 버튼은 동작한다.

## 6. 모델별 등록 (list_display / list_filter / search_fields)

> 아래는 의도(필수 컬럼)를 명시한 것이며, 정확한 컬럼 집합은 구현 시 모델 필드에
> 맞춰 확정한다. FK가 있는 list는 `list_select_related`로 N+1을 피한다.

### batches
- **BatchJob**: display = id, status, processing_state, total_count, completed_count,
  remote_enabled, created_at, completed_at; filter = status, processing_state,
  remote_enabled; search = id, claimed_by, remote_principal. **액션: 카운터 재계산.**
- **BatchItem**: display = job, row_order, detected_type, status, inchikey;
  filter = status, detected_type; search = inchikey, external_id, job__id;
  `list_select_related = ("job",)`.
- **BatchQuotaEvent**: display = kind, principal, ip_address, row_count, created_at;
  filter = kind; search = principal, ip_address.
- **ExportArtifact**: display = token(truncated), job, fmt, byte_size, row_count,
  created_at, expires_at; filter = fmt; search = token, job__id.

### remote
- **RemoteTask**: display = inchikey, state, priority, principal, attempt,
  next_run_at, lock_owner, updated_at; filter = state, principal; search = inchikey,
  lock_owner, principal. **액션: 선택된 DEFERRED/RETRY_WAIT → READY 재시도(안전 가드).**
- **RemoteProviderControl**: display = provider, state, blocked_reason,
  credential_version, updated_at; filter = state. **액션: provider auth 재활성화;
  체인지리스트 버튼: Remote 일시정지/재개 + 현재 상태 배너.**
- **RemoteSubmission**: display = provider, payload_hash(truncated), state,
  remote_query_id, reconcile_attempts, created_at; filter = state, provider;
  search = payload_hash, remote_query_id. **액션: UNKNOWN → FAILED(키 해제).**
- **RemoteQuery**: display = provider, remote_query_id, state, submitted_at,
  completed_at; filter = state, provider; search = remote_query_id.
- **RemoteQueryItem**: display = query, task, inchikey, apply_state, applied_at;
  filter = apply_state; search = inchikey.
- **RemoteSubmissionMember**: display = submission, provider, inchikey, active,
  released_at, release_reason; filter = active, provider; search = inchikey.
- **RemoteAttempt**: display = operation, classification, provider, http_status,
  request_inchikey, returned_inchikey, created_at; filter = operation,
  classification, provider; search = request_inchikey, returned_inchikey,
  remote_query_id. `show_full_result_count=False` 필수(대용량).
- **RemoteCircuit**: display = provider, state, consecutive_failures, opened_at,
  updated_at; filter = state.
- **RemoteRateBucket**: display = provider, bucket, capacity, tokens,
  refill_rate_per_sec, blocked_until.
- **RemoteBudgetLedger**: display = principal, units, reason, task, created_at;
  search = principal.
- **RemotePrincipalState**: display = principal, last_claimed_at, claim_count,
  updated_at; search = principal.
- **RemoteQueueControl**: display = provider, updated_at.

### classifications
- **ClassificationResult**: display = requested_inchikey, returned_inchikey, kind,
  source_type, source_priority, retrieved_at, created_at; filter = kind,
  source_type; search = requested_inchikey, returned_inchikey, remote_query_id.
  `show_full_result_count=False` 필수(대용량).
- **ClassyfireLive**: display = inchikey, source_type, source_priority,
  retrieved_at; filter = source_type; search = inchikey.
- **ClassyfireSeed**: display = inchikey, inchikey14, seed_version, seed_source_id;
  search = inchikey, inchikey14. `show_full_result_count=False`.
- **ClassificationHistory**: display = inchikey, source_type, source_priority,
  retrieved_at, archived_at; filter = source_type; search = inchikey.
- **SourceArtifact**: display = source_type, name, checksum(truncated), byte_size,
  imported_at; filter = source_type; search = name, checksum.

### operations
- **SeedImport**: display = id, import_status, filename, actual_row_count,
  duplicate_count, created_at, who; filter = import_status; search = filename, doi,
  record_id.
- **SystemEvent**: display = id, category, event_type, created_at; filter =
  category; search = event_type. (읽기전용; 서명/감사 무결성 보존.)
- **AuditSignature**: display = event, key_id, payload_sha256(truncated),
  created_at; search = event__id.

### compounds
- **CompoundStructure**: display = inchikey, canonical_smiles(truncated),
  created_at; search = inchikey.
- **CompoundUsage**: display = inchikey, seen_count, waiting_job_count, first_seen,
  last_seen; search = inchikey.

### chemont
- **ChemOntNode**: display = numeric_id, chemont_id, name, parent,
  ontology_version; search = chemont_id, name; `list_select_related = ("parent",)`.

### core
- **LoginThrottle**: display = key, failure_count, window_started_at, updated_at;
  search = key.

## 7. 운영 액션 매핑

| 위치 | 액션 | 재사용/구현 | 안전 가드 |
|------|------|-------------|-----------|
| RemoteProviderControl 체인지리스트 버튼 | Remote 일시정지 / 재개 (+현재상태 배너) | `pause_remote`/`resume_remote`/`is_remote_paused` | 전역 토글; POST + actor 기록 |
| RemoteProviderControl 선택 액션 | Provider auth 재활성화 | 신규 `reset_provider_auth(provider, actor)` (command와 공유, SystemEvent 추가) | 선택된 provider만 ENABLED로 |
| RemoteTask 선택 액션 | DEFERRED/RETRY_WAIT → READY 재시도 | `retry_failed_tasks`의 안전 로직 | in-flight(LOCKED/RUNNING/…)·DONE 제외 |
| BatchJob 선택 액션 | Job 카운터 재계산 | `recompute_job_counters(job_id)` + SystemEvent 래핑 | 멱등 재계산 |
| RemoteSubmission 선택 액션 (UNKNOWN만) | FAILED 처리(idempotency 키 해제) | 신규 `fail_unknown_submission` (`_mark_failed` 추출, 멤버 예약 해제 포함) | UNKNOWN 아닌 행은 건너뜀 |

**의도적 제외**: submission의 "accepted 확정"(remote_query_id 증거·검증 필요)은
admin이 아닌 `manage.py resolve_submission`에만 둔다. pause/resume는 전역 토글이라
행 선택 방식이 어색하므로 체인지리스트 상단 버튼 + 상태 배너로 구현한다.

모든 액션은 `request.user.get_username()`을 actor로 `SystemEvent`에 기록한다.

## 8. 성능 / 안전 세부

- 대용량 테이블(`ClassificationResult`, `RemoteAttempt`, `ClassyfireSeed`)은
  `show_full_result_count = False`로 전체 `COUNT(*)` 회피.
- FK list는 `list_select_related`로 N+1 회피.
- `date_hierarchy`는 대용량 테이블에서 별도 집계 쿼리를 유발하므로 기본 미사용.
- 액션의 actor 기록은 `request.user`에서 가져오고, 미인증/익명 경로는 admin이
  스태프 인증을 요구하므로 발생하지 않는다.

## 9. 테스트 전략 (pytest-django, 기존 컨벤션)

신규 `tests/test_admin_console.py` (`pytestmark = pytest.mark.django_db(transaction=True)`):
- **등록 검증**: 대상 모델이 `admin.site`에 등록됐고, 각 ModelAdmin의
  `has_add/change/delete_permission`이 모두 False.
- **체인지리스트 스모크**: `admin_client`로 주요 앱 체인지리스트가 200 응답(500 없음).
- **액션 동작**:
  - `recompute_job_counters` 액션이 카운터를 갱신하고 SystemEvent를 남긴다.
  - RemoteTask 재시도 액션이 DEFERRED/RETRY_WAIT만 READY로 바꾸고, RUNNING 등
    in-flight 행은 그대로 둔다.
  - `reset_provider_auth` 공유 함수가 AUTH_BLOCKED→ENABLED 전이 + SystemEvent 생성.
  - `fail_unknown_submission`이 UNKNOWN→FAILED + 활성 멤버 예약 해제 + SystemEvent.
  - pause/resume 버튼이 `is_remote_paused()` 상태를 토글.
- **불변성**: immutable 모델(ClassificationResult 등)은 admin change/delete 권한 없음.

## 10. 마이그레이션

모델 스키마 변경이 없으므로 DB 마이그레이션은 발생하지 않는다(admin 등록은 런타임
구성). 신규 공유 함수 리팩터도 스키마에 영향 없음.
