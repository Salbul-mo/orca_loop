# ORCA Planning Request — API v2 Phase 1 without customer reply

## Master directive

Plan only. Do not modify the target repository.

The user wants a Macro Blocking plan and a Micro Blocking plan for the parts of API v2 Phase 1 that can be implemented safely **without any further customer reply**.

I (the calling ChatGPT session) am acting as Master. Workers must provide planning/review evidence only; do not escalate ordinary engineering choices to the user if they can be resolved from the supplied System Design, current code, and the two v2 contracts already summarized by that design.

### Scope rule

Include work that is:
1. explicitly required by API v2 Phase 1;
2. determined by the supplied System Design and current repository state;
3. independent of unresolved customer questions;
4. reversible or internally controlled where later customer answers may affect future phases.

Exclude or gate work that depends on customer answers, especially:
- multi-Media assignment / registration-vs-assignment split (Phase 2 / question 1);
- hierarchical group path naming semantics (question 2) beyond the temporary Phase-1 compatibility rule already stated in the design;
- externalAccess / cross-network addressing policy (question 3);
- real-device confirmation (question 4);
- playback/export naming migration and recording schedule changes reserved for Phase 3;
- account management, fixed-admin policy, camera-tests, ONVIF/PTZ/imaging UI/features reserved for Phase 4;
- AI Server contract changes outside platform-poc.

### Planning requirements

Produce a plan that can be converted directly into:
- a Macro Blocking document: independently verifiable blocks, dependencies, entry/exit criteria, tests, and explicit customer-response gates;
- a Micro Blocking document: small implementation units with exact intent, likely files/components, prerequisites, acceptance criteria, and verification commands/tests.

Prioritize minimal-risk ordering:
- repair the Media v2 mock contract first where it is the test oracle;
- then protocol/security/path compatibility;
- then persistence/schema and contract DTO changes;
- then mutation/reconciliation semantics;
- then local managed integration and regression verification.
Do not mix future Phase 2/3/4 changes into these blocks.

The final planning evidence must distinguish:
- **NOW**: can implement without customer reply;
- **GATE**: must wait for customer reply or real-device verification;
- **DEFER**: explicitly belongs to later phase even if technically possible now.

Use the current repository as evidence. Avoid speculative refactors and broad REC→MEDIA renaming unless required by the external v2 wire contract.

## Authoritative System Design

The complete contents of the user's source document follow.

---

# API v2.0 1단계(Media v2.0 연동) — System Design

- 작성일: 2026-10-12 (rev.4 — 3차 검토 의견 반영, 16장)
- 상태: **승인 대기** (Phase 1 / 4)
- 브랜치: `feat/api-v2-media-integration`
- 상위 계획: `docs/reference/26_10_12_api_v2_적용_방향_아이서티_전달.md` 3.3절
- 기준 명세: `docs/컨텍스트라 Media Server_API v5.html`, `docs/컨텍스트라 Web Server_API v4.html` (둘 다 Version 2.0)
- 검증 수단: `mock/media-server` (커밋 `ca07935`). **스트리밍 계약은 아직 v1.1 상태라 B-00 보완 후에 쓸 수 있다(D-11)**

---

## 1. 배경과 문제

납품한 Web Server는 Streaming Server API v1.1 계약으로 Media Server와 연동한다. 아이서티가 v2.0을 전달했고,
현장 Media Server는 v2.0으로 일괄 전환된다(상위 계획 3.1-3). v1.1 Web Server는 v2.0 Media Server에 붙지 않는다.
직접 확인한 불일치는 다음과 같다.

| 구분 | v1.1 (현재 코드) | v2.0 |
|---|---|---|
| WSS 경로 | `/ws/v1` (`RecConnectionManager.java:1216,1250`) | `/ws` |
| WSS 인증서 | 장비 Root CA 앵커 + hostname 검증 (`RecControlTlsConfig`, `RecControlTrustProvider`) | 검증 생략. 암호화·토큰 인증은 유지 (Media API 3.2) |
| REST base | `/api/v1` | `/api` |
| 로그인 `controlIp` | body 무시, 관측한 상대 IP 저장 (`ManagedTokenService.java:409-411`) | body 값을 그대로 저장, `0.0.0.0` 거부 (Web API 3.3) |
| 비활성 계정 로그인 | 비밀번호 확인 전에 `AUTH_FAILED` (`ManagedTokenService.java:143`) | 비밀번호가 맞으면 `ACCOUNT_DISABLED`(403) |
| 준비 상태 | 동기화 중 오류가 나면 WSS를 닫고 재접속 (`RecConnectionManager.java:590-602`) | `status.get`이 READY가 될 때까지 WSS 유지·폴링, `SERVER_NOT_READY`는 연결 실패가 아님 (Web API 9.3, Media API 15장) |
| 카메라 설정 번호 | 없음 | `configRevision` 필수(create·update·sync, ≥1), list·get 응답에 포함 |
| ONVIF | `useOnvif` boolean (`CCTV_SOURCE.use_onvif`) | `onvifPort`, `onvifVideoSourceToken` |
| 그룹 삭제 | Web이 하위 카메라까지 soft delete하고 Media `camera-groups.delete`가 카메라를 지워 준다는 전제 (`CameraGroupService.java:415-460`, `RecMutationExecutor.java:358-361`) | Media가 소속 카메라가 있으면 `GROUP_HAS_CAMERAS`로 거부 |
| 카메라 이름 | 같은 그룹 안에서만 유일 (`V6`) | 전역 유일, 앞뒤 공백 제거·대소문자 무시 |
| 그룹 이름 | 형제 범위 유일 (`V6`) | Media는 자기가 아는 그룹끼리 이름 중복을 거부(`GROUP_NAME_CONFLICT`) |
| cameraId·groupId | 서버별 카운터 (`SERVER_RESOURCE_ID_COUNTER` PK `(server_pk, resource_kind)`), 할당 원장 유일 키도 서버별 | Web Server 전역 발급 |
| Media 발신 등록 | `/servers/{id}/camera-ids`·`/groups`·`/cameras` (`RecInventoryReportController`) | 경로 없음. 등록·배정은 Web 관리자 전용 |
| Media 반영 실패 시 Web 값 | 거부되면 `beforeJson`으로 카메라·그룹 설정을 되돌림 (`RecMutationCompletionService.java:69-75,119-130`) | 실패해도 Web 저장값 유지, 재시도 (Web API 9.3, Media API 4장 끝) |
| 그룹 생성·수정 확인 | `camera-groups.list`로 읽어 확인, 없으면 `READBACK_MISMATCH`·`RESOURCE_MISSING` (`RecMutationExecutor.java:373-430`) | list는 "자기 담당 카메라가 있는 그룹만" 보인다(Media API 5장) — 빈 그룹은 목록에 없을 수 있다 |

명세 대조에서 추가로 나온 Media 쪽 차이다.

| 구분 | v1.1 | v2.0 | 영향 |
|---|---|---|---|
| `streaming.server.status` 토큰 필드 | `mediaToken`, `mediaTokenIssuedAtWallMs` | `streamingToken`, `streamingTokenIssuedAtWallMs` | 코덱이 모르는 필드를 무시하므로(`RecMessageCodec.java:55`) 파싱은 성공하지만 값이 비어 들어온다 |
| 스트리밍 설정 `revision` | 없음. Web은 채널 변경에도 번호를 올린다(`RecStreamingConfigService.java:422` → `markPending`) | `settings.put`에 Web 번호를 싣고 `settings.get`이 돌려준다. 서버 설정이 바뀔 때만 오르고, 채널 상태는 번호 없이 `streaming.channels.list`로 대조한다(Web API 10장) | 채널 변경 시 번호 증가를 멈춘다 |
| `camera-groups.delete` 결과 | `{groupId, removedCameraCount, preservedRecording}` | `{groupId}` | 코덱이 빠진 primitive를 0/false로 두어 깨지지는 않는다 |
| `cameras/fetch`·bootstrap 카메라 | `origin`, `pending`, `useOnvif` 포함 | 셋 제거, `configRevision`, `onvifPort`, `onvifVideoSourceToken` 추가 | Media가 v2 형식으로 복원 |
| 대조 범위 | Media에 없는 카메라를 다시 만들지 않음(`RecMutationExecutor.java:254-258`) | 4가지 경우로 나눠 매번 전체를 맞춤(Web API 9.3 대조 표) | 6.3 |

## 2. 목표

1. v2.0 Media Server(실장비, 지금은 `mock/media-server`)에 로그인 → 부트스트랩 → WSS(`/ws`) → READY 확인 → 카메라·그룹·스트리밍 설정 반영까지 정상 동작한다.
2. Media가 호출하거나 받는 계약은 v2.0 문서와 필드·오류 코드까지 일치한다(상위 계획 3.1-1).
3. Web DB가 기준이다. Media 반영이 실패해도 Web 저장값은 유지되고, 재접속 직후와 주기 대조에서 번호(`configRevision`, 스트리밍 `revision`)로 판정해 다시 맞춘다.
4. 기존 기능(관제 화면, 이벤트, AI 연동, 녹화 스케줄)은 회귀 없이 유지한다.

## 3. 비목표

| 항목 | 이유 |
|---|---|
| 카메라 등록과 MEDIA·AI 배정 분리, 복수 MEDIA 배정 | 2단계. 질문 1 회신 후 |
| 그룹 계층·경로 이름(`본관 > 1층`) | 질문 2 회신 후. 1단계는 D-9의 지원 조건만 둔다 |
| 녹화 조회·재생·내보내기 명칭, 녹화 스케줄 | 3단계 |
| 운영자 그룹·권한, 관리자 고정, `camera-tests`, ONVIF·PTZ·이미징 | 4단계 |
| `externalAccess` | 질문 3 |
| orphan 카메라를 관리자가 확인해 정리하는 화면 | orphan은 1단계에서 로그로만 남긴다(D-5). 정리 화면은 2단계 카탈로그와 함께 |
| AI Server 쪽 변경 | 별도 저장소. AI 채널(Web→AI WSS)과 AI의 `/api/v1` 경로는 그대로 |
| 브라우저 로그인 화면의 `ACCOUNT_DISABLED` 표시 | Client 계정을 비활성화하는 화면이 없다(`DatabaseUserDetailsService.java:53`). 4단계 계정 관리와 함께 |
| Client 대상 REST의 v2 경로 노출 | 관제 화면은 자체 경로를 쓴다(상위 계획 3.1-2) |

## 4. 설계 결정 (승인 필요)

| ID | 결정 | 권장안 | 대안 | 권장 이유 |
|---|---|---|---|---|
| D-1 | REST 경로 전환 방식 | v2 경로 `/api/...`를 Media 호출 경로에 **추가**하고 `/api/v1/...`는 AI용으로 남긴다 | `/api/v1`을 지우고 `/api`로만 | `/auth/login`, `/tokens/introspect`, `/logs`는 AI도 호출하고, AI는 아직 `/api/v1`을 쓴다 |
| D-2 | `configRevision`의 원천 | 기존 `CAMERA.rec_assignment_revision`(r)을 쓰고, **Media와 주고받을 때는 `configRevision = r + 1`로 변환한다.** 변환은 한 곳에서만 한다. 저장값·미완료 저널은 바꾸지 않는다 | (가) 저장값 일괄 +1 마이그레이션 (나) 새 칼럼 | r은 카메라 변경마다 오르는 번호이고 저널 `target_revision`도 같은 값이다. r은 0부터 시작하므로 +1이면 명세의 "1 이상"을 만족한다. (가)는 미완료 저널의 `target_revision`과 어긋나 `REVISION_SUPERSEDED`(`RecMutationExecutor.java:206-207`)를 일으킨다. (나)는 증가 지점을 모두 새로 맞춰야 한다 |
| D-3 | 스키마 변경 방식 | Flyway `V19` 추가. `canonical-manifest.json`, `CanonicalSchemaContractDbIT`도 함께 갱신 | v2 기준 baseline 재작성 | 2단계에서 구조가 또 바뀐다. baseline은 2단계 끝에 한 번 |
| D-4 | 그룹 삭제 규칙 | 하위 트리에 살아 있는 카메라가 있으면 `409 GROUP_HAS_CAMERAS`로 거부. 없으면 빈 하위 그룹까지 함께 지운다(자식 먼저) | 직속 카메라만 검사 | DB는 트리를 허용한다. 하위에 카메라가 남은 채 지우면 Media가 거부한다. 2026-09-02 결정(카메라까지 연쇄 soft delete)을 v2 계약에 따라 뒤집는다 |
| D-5 | 대조 범위 (Web API 9.3 표) | 아래 4가지로 나눈다. **카탈로그에 아예 없는 Media 카메라(orphan)는 지우지 않는다** | 3번째 경우도 로그만 | 명세 9.3이 orphan 자동 삭제를 금지하고, 배정 해제가 확정된 카메라는 관리자 확인 없이 다시 해제하라고 정한다 |
| D-6 | v1.1 전용 코드 제거 | 제거: ① Media 발신 등록 경로(약 1,100줄 + 테스트) ② Media WSS용 REC Root CA 앵커와 설정 `platform.rec.ca-dir`, `platform.rec.verify-hostname`. AI 신뢰 앵커는 유지 | 비활성으로 남김 | v2 전용 원칙(상위 계획 3.1-3) |
| D-7 | 3.3 밖의 Media 차이 포함 | `streamingToken`, 스트리밍 `revision`(D-10), bootstrap·`cameras/fetch` 형식, `run-local-rec.sh`의 목업 전환 | 2단계로 미룸 | 빠지면 1단계 완료 조건(정상 연동)을 채우지 못한다 |
| D-8 | Media 반영 실패·미확인 시 Web 저장값 | **어떤 경로에서도 되돌리지 않는다.** ① 거부 응답: `RecMutationCompletionService`의 `beforeJson` 복원(카메라 `:69`, 그룹 `:125`)과 `cascadeRejected`(`:119`)를 없앤다. ② 재시작: `RecMutationRecoveryService`가 남은 `IN_FLIGHT`를 닫으면서 하던 UPDATE 복원(`:117-119`, `rollback`)을 없애고, 행은 `UNKNOWN`(결과 미확인)으로 닫는다. 실패·미확인은 `rec_last_sync_error`·`last_sync_error`와 미반영 상태로만 남기고, 대조(D-13)가 다시 맞춘다 | 지금처럼 되돌림 | 명세는 "실패해도 저장은 유지되며 원인은 로그에 남긴다"고 정한다. 거부 경로만 고치면 "저장 → 전송 → 응답 전 Web 종료 → 재시작"에서 여전히 이전 값으로 돌아간다. Web 쪽 검증 실패(이름 중복 등)는 지금처럼 저장 전에 거부한다 |
| D-9 | 그룹 이름 (질문 2 회신 전 **한시적** 지원 조건) | **같은 Media 서버 안에서 살아 있는 그룹 이름이 겹치지 않아야 한다.** 유일 키를 `(rec_server_pk, active_parent_key, active_group_name)`에서 `(rec_server_pk, active_group_name)`로 되돌리고, Web이 409로 거부한다. 새 그룹 자동 번호(`새 그룹 2`)도 서버 범위로 센다 | 형제 범위 유지 | Media는 자기 그룹끼리 이름 중복을 거부하고(`GROUP_NAME_CONFLICT`), 이 실패는 재시도해도 풀리지 않는다. 지금 화면은 그룹을 1단으로만 만들므로 루트 그룹에서는 기존 규칙과 결과가 같다. 2026-09-03의 형제 범위 완화(`본관 > 1층`, `별관 > 1층`)는 질문 2(나)가 오면 경로 이름으로 다시 연다. **v2 Web API의 전역 그룹 이름 규칙은 1단계에서 완료한 것으로 보고하지 않는다** |
| D-10 | 스트리밍 설정 번호 | Web 번호는 **서버 설정(`/streaming-config/server`, 로그인 시 bind IP 확정 변경)일 때만 올린다.** 채널 변경은 번호를 올리지 않고 `streaming.channels.list`로 대조한다. `settings.put`에 번호를 싣고, Media 번호가 더 크면(`applied:false` 또는 `settings.get` 값) Web 번호를 Media 번호 + 1로 올려 다시 보낸다 | 지금처럼 채널에도 증가 | Web API 10장·9.3. 카메라와 같은 백업 복구 규칙이 없으면 Web=7, Media=10에서 7을 계속 보내도 적용되지 않는다 |
| D-11 | 목업 보완을 선행 조건으로 | 1단계 첫 블록(B-00)에서 `mock/media-server`의 스트리밍 계약을 v2로 보완한다: `streamingToken`, `settings.get/put`의 `revision`(저장·반환·오래된 번호 `applied:false`). 빈 그룹을 목록에서 숨기는 동작을 환경변수로 켤 수 있게 해 O-2의 두 해석을 모두 시험한다 | 보완 없이 진행 | 지금 목업으로는 스트리밍 v2 계약을 검증할 수 없다(`mock/media-server/src/actions/streaming.js:138`). 앞선 smoke PASS는 이 계약을 다루지 않았다 |
| D-12 | `V19`가 지원하는 DB | ① 새로 만드는 DB(현장, 아이서티 합의) ② 기존 개발 DB — **단 사전 조건을 만족할 때만.** `V19`는 첫머리에서 조건을 검사하고, 어기면 이유를 담아 실패한다(7.3) | 새 DB만 지원 | 개발·시험 DB를 매번 새로 만들지 않아도 되고, 조건을 어기는 DB는 조용히 반쯤 적용되지 않고 바로 멈춘다 |
| D-13 | 대조 대상과 상태 해제 | ① **대상:** 연결이 READY인 Media 서버마다, 그 서버의 Web 리소스 전부(살아 있는 것 + 삭제가 완료되지 않은 것). 최신 저널이 `APPLIED`인 것만 고르던 규칙(`RecMutationOrchestrator.java:294-321`)과 저널 상태에서 서버 목록을 구하던 방식(`:221,234`)을 없앤다. `REJECTED`·`UNKNOWN`도 자동 대상이다. ② **중복 방지:** 진행 중 저널(`PENDING`·`IN_FLIGHT`)이 있는 리소스는 그 주기에 건너뛴다. 대조와 저널 실행은 지금처럼 서버별 단일 작업 차선에서만 돈다. ③ **해제:** Media 값이 Web 값과 일치함을 확인하면(번호 일치 또는 push 성공 응답) 카메라는 `ASSIGNED`·`rec_last_sync_error = NULL`, 그룹은 `APPLIED`·`last_sync_error = NULL`로 바꾼다. 이 갱신은 확인에 쓴 번호가 지금 저장값과 같을 때만 적용한다(사이에 들어온 사용자 변경을 지우지 않는다). ④ **삭제 행:** soft delete됐지만 완료되지 않은 카메라는 Media 목록에 있으면 `delete`, 없거나 `CAMERA_NOT_FOUND`면 완료(`DETACHED`, 원장 회수). 그룹은 D-16 | 지금처럼 `APPLIED`만 대조 | D-8로 실패가 Web 값을 되돌리지 않게 되면, 실패한 리소스야말로 다시 맞춰야 할 대상이다. 대조가 맞췄는데 화면이 계속 미반영으로 남는 일을 ③이 막는다 |
| D-14 | 번호 재발급과 저널 | Media 번호 m이 Web 송신 번호보다 크면(`applied:false` 또는 대조), **한 트랜잭션에서** ⓐ 저장값 r이 확인 시점 값과 같은지 검사(다르면 사용자 변경이 들어온 것이므로 재발급을 그만두고 그 변경의 저널에 맡긴다) ⓑ r을 m으로 올린다(다음 송신 번호 m + 1) ⓒ 그 리소스의 기존 미완료·거부 저널을 `SUPERSEDED`로 닫는다 ⓓ 지금 DB 값으로 desired를 만든 새 UPDATE 저널(`target_revision = m`)을 쓴다. 스트리밍 번호(D-10)도 같은 원칙이다 | 같은 저널 행의 번호만 갱신 | 같은 행을 고치면 그 행의 옛 스냅샷이 최신 사용자 값을 덮을 수 있다. 저널 유일 키 `(server_pk, kind, resource_id, target_revision)`와는 m이 이전 모든 target보다 크므로 충돌하지 않는다. ⓐ가 "이전 응답이 최신 변경을 덮지 않는다"를 보장한다 |
| D-15 | 수동 재시도 | `REJECTED` 행을 같은 스냅샷으로 `PENDING`으로 되돌리던 재시도(`CameraMutationService.java:181`)를 **지금 DB 값으로 새 저널을 만드는 방식**으로 바꾼다. 한 트랜잭션에서 ⓐ 리소스 번호를 1 올리고(카메라 `rec_assignment_revision`, 그룹 `sync_revision`, 확인 시점 값과 같을 때만) ⓑ 옛 행을 `SUPERSEDED`로 닫고 ⓒ 올린 번호를 `target_revision`으로 새 저널을 쓴다. D-8로 거부 뒤에도 저장 번호가 옛 저널의 `target_revision`과 같게 남으므로, 번호를 올리지 않으면 저널 유일 키 `UK_REC_MUTATION_JOURNAL_IDENTITY`(`V7__rec_mutation_journal.sql:32-33`)에 걸린다 | 옛 스냅샷 재실행 | 옛 스냅샷은 `useOnvif`를 담고 ONVIF 포트가 없으며, D-8 이후에는 Web 저장값이 곧 desired다. 이렇게 하면 `V19` 이전에 만들어진 `REJECTED` 행도 다시 실행될 일이 없다 |
| D-16 | 그룹의 Media 생명주기 | ① Web에서 그룹을 만들면 **Web에만** 만든다(push 없음). ② 그 서버에 그 그룹의 첫 카메라가 들어가기 직전(카메라 create·그룹 이동), 같은 작업 안에서 `camera-groups.create`(`GROUP_CONFLICT`면 `update`)를 먼저 보낸다. ③ 이름·정렬 변경은 그룹이 Media에 있을 때만 push한다. ④ 그 서버에서 그룹의 마지막 카메라가 빠지는 작업(삭제·이동)이 완료되면 groupId로 `camera-groups.delete`를 보낸다(`GROUP_NOT_FOUND`도 완료). ⑤ Web은 그룹마다 "Media에 있음/없음"을 기록하고(1단계는 그룹이 서버 하나에 속하므로 그룹 행의 칼럼, `V19`), 대조는 이 기록과 카메라 수로 판정한다(6.5). 목록 재조회로 존재를 판정하지 않는다 | 사용자 생성 시 바로 push | Media API 5장: "담당 카메라가 하나도 없는 그룹은 push받지 않는다", 마지막 카메라가 없어지면 delete를 push. 빈 그룹이 목록에서 숨겨질 수 있으므로 정리는 목록 탐색이 아니라 Web 기록과 groupId로 해야 한다 |

## 5. 제안 구조와 구성요소 책임

구조는 바꾸지 않는다. 기존 구성요소 안에서 계약을 v2로 옮긴다.

| 구성요소 | 변경 |
|---|---|
| `mock/media-server` | (B-00, D-11) 스트리밍 v2 계약, 빈 그룹 숨김 옵션, smoke 점검 추가 |
| `SecurityConfig`, `CanonicalBearerPolicy` | 정규(Bearer) 경계를 "`/api/v1/**` 또는 v2 Media 경로 8개(7.1)"로 확장. 접두사가 아니라 명시 목록이다 |
| Media 호출 컨트롤러 (`ManagedAuthController`, `ManagedTokenController`, `RecSnapshotController`, `RecLogController`) | 기존 `/api/v1` 매핑에 v2 경로를 함께 매핑(D-1) |
| `ManagedTokenService` | Media 로그인: body `controlIp` 필수·검증·저장. 비밀번호 확인 뒤 비활성 판정 → `ACCOUNT_DISABLED`. AI·Client 로그인은 그대로 |
| `ManagerErrorCode` | `ACCOUNT_DISABLED(403)`, `GROUP_HAS_CAMERAS(409)` 추가 |
| `RecConnectionManager` | Media 경로 `/ws`(AI `/ws/v1` 유지). Media 동기화 체인에 READY 대기 단계. 동기화 중 `SERVER_NOT_READY`는 READY 대기로 되돌리고 연결 유지. `HttpClient` 공급자를 actor별로 분리 |
| `RecManagedConfiguration`, `RecControlTlsConfig`, `RecControlTrustProvider` | Media WSS 전용 "인증서 검증 생략" 클라이언트(TLS 유지). 기존 공급자는 AI 전용(D-6) |
| `Rec*Contracts` | `configRevision`, `applied`, `onvifPort`, `onvifVideoSourceToken`, `streamingToken`, 스트리밍 `revision`, 그룹 삭제 결과 `{groupId}`. `useOnvif` 제거 |
| `RecMutationSnapshotFactory`, `RecMutationDesiredSnapshot` | desired 스냅샷에 ONVIF 필드. `configRevision`은 스냅샷에 넣지 않고 `target_revision`에서 변환(D-2) |
| `RecMutationExecutor` | push에 `configRevision` 포함, `applied:false` 처리(9장), 그룹 생성·수정 확인 방식 변경(6.5), 대조 4가지 경우(D-5) |
| `RecMutationCompletionService` | 거부 시 복원 제거, 실패 기록만(D-8). 그룹 삭제 완료 시 카메라 연쇄 완료 제거(D-4 이후 빈 그룹만 삭제된다). 성공 시 미반영 해제는 번호 일치 조건(D-13③) |
| `RecMutationRecoveryService` | 재시작 시 남은 `IN_FLIGHT`를 `UNKNOWN`으로 닫고 복원하지 않는다(D-8②) |
| `CameraMutationService` | 수동 재시도를 "지금 DB 값으로 새 저널"로(D-15). 번호 재발급 트랜잭션(D-14) |
| `RecMutationOrchestrator` | 대조 대상 선정(D-13①②), 카메라 대조 4가지 경우(D-5), 그룹 생명주기 대조(D-16), 번호 재발급 호출(D-14) |
| `RecStreamingConfigService`, mapper | 번호 증가 범위(D-10), `settings.put` 번호 송신, Media 번호가 클 때 재발급, 채널은 `channels.list` 대조 |
| `RecBootstrapService`, `RecSnapshotController` | bootstrap·`cameras/fetch` 카메라 형식 v2화, `configRevision = r + 1` |
| `CameraService`, `CameraGroupService`, mapper, `camera-settings.js`, `modal.jspf` | 카메라 이름 전역 유일, 그룹 이름 서버 범위(D-9), 그룹 삭제 거부(D-4), 그룹 생성은 Web에만(D-16①), ONVIF 포트 입력(기본 80) |
| `ResourceIdAllocator`, mapper | cameraId·groupId 전역 카운터 |
| `V19`, `canonical-manifest.json`, `CanonicalSchemaContractDbIT` | 7.3 |
| `run-local-rec.sh` | 띄우는 목업을 `mock/media-server`로 |

## 6. 종단 흐름

### 6.1 Media 기동 → READY

```text
Media  POST /api/auth/login {loginId, password, controlIp, controlPort, streamingBindIp}
Web    계정 조회 → 비밀번호 확인 → (비활성이면 403 ACCOUNT_DISABLED)
       → controlIp 검증(IP 리터럴, 0.0.0.0·:: 금지) → SERVER.control_ip 저장 → 토큰 발급
       → 커밋 후 endpointObserved(controlIp, controlPort)
Web    wss://{controlIp}:{controlPort}/ws (Media 전용 HttpClient: TLS, 인증서 검증 생략)
       Media가 아직 로그인 응답을 처리 못 했으면 503 → 기존 재시도 예산으로 재접속
Media  POST /api/servers/{id}/bootstrap → BOOTSTRAPPING → READY (실패하면 NOT_READY, WSS 유지)
Web    control.server.status → tokens.flushCache
       → status.get 폴링: BOOTSTRAPPING 1s 간격 / NOT_READY는 lastError 기록 후 backoff(1→2→5→10s 상한)
         / READY면 다음. 이 동안 WSS를 닫지 않는다
       → cameras.runtimeStatus.list → 대조(6.3) → 연결 상태 READY로 승격
```

### 6.2 관리자가 카메라를 바꿈

```text
화면 → CameraService.update → CAMERA.rec_assignment_revision r → r+1
     → 변경 저널(target_revision = r+1)
스케줄러 → camera-assignments.update {..., configRevision = target_revision + 1}
Media  applied:true  → 저널 완료, ASSIGNED (r == target_revision일 때만, D-13③)
       applied:false → Media 번호 m이 더 큼 → D-14 재발급
       거부(non-retryable) → Web 저장값 유지, rec_last_sync_error 기록, PENDING_UPDATE 유지(D-8)
                            → 다음 대조 주기가 자동 대상으로 다시 맞춤(D-13)
응답 전 Web 종료 → 재시작 시 저널 UNKNOWN, 저장값 유지(D-8②) → 대조가 번호로 판정
```

### 6.3 대조 — 카메라 (재접속 직후 + 약 60초 주기, Web API 9.3)

Media `camera-assignments.list`의 각 항목과 이 서버의 Web 카메라를 맞춘다. "카탈로그"는 1단계에서 `CAMERA` 행 전체(soft delete된 행 포함, 모든 서버)다. 삭제가 끝난 카메라도 `DETACHED`·`deleted_at_wall_ms`로 남는다(`RecCrudMutationMapper.java:110-115`).

| 경우 | 판정 | 처리 |
|---|---|---|
| Web에 살아 있고 이 서버 소속, Media 목록에 없음 | 미반영 | 그룹을 먼저 확인(6.5) 후 `create`(전체 설정, 비밀번호 포함) |
| 양쪽에 있음, `configRevision`이 `r + 1`과 다르거나 없음 | 값 다름 | `update`(전체 설정). Media 번호가 더 크면 9장 규칙 |
| 양쪽에 있음, 번호 같음 | 일치 | 그대로 |
| Media에 있음, Web에 그 cameraId 행이 있지만 이 서버에서 살아 있지 않음(soft delete·`PENDING_DELETE`·다른 서버 소속) | 배정 해제 확정 | `delete`(`stop_keep`, 녹화 파일 보존). 관리자 확인 없이 처리 |
| Media에 있음, Web에 그 cameraId 행이 전혀 없음 | orphan | **지우지 않는다.** `media_orphan_camera serverId cameraId` 경고 로그만(같은 항목은 상태가 바뀔 때만 다시 기록) |

### 6.4 대조 — 스트리밍 (D-10)

```text
settings.get.revision == Web revision → 그대로
                     < Web revision  → settings.put(전체, revision)
                     > Web revision  → Web revision := Media revision + 1 저장 → settings.put
streaming.channels.list 의 채널별 enabled ≠ Web 채널 desired → streaming.channels.setEnabled
```

### 6.5 그룹의 Media 생명주기와 대조 (D-16, O-2 해소)

Media가 빈 그룹을 목록에서 숨길 수 있으므로 **목록에 없음 = 실제로 없음**으로 보지 않는다. 그룹이 Media에 있는지는 Web 기록(`media_state`: `ABSENT`·`PRESENT`)으로 판단하고, 목록은 이름·정렬 비교와 Web이 모르는 그룹 탐지에만 쓴다.

| 상황 | 처리 |
|---|---|
| 사용자 그룹 생성 | Web에만 저장(`ABSENT`). push하지 않는다 |
| 카메라 create·그룹 이동 직전, 대상 그룹이 `ABSENT` | 같은 작업 안에서 `camera-groups.create` → 응답의 groupId·이름·정렬이 desired와 같으면 `PRESENT`. `GROUP_CONFLICT`면 `update`로 맞춘 뒤 `PRESENT` |
| 카메라 create·그룹 이동이 `GROUP_NOT_FOUND` (**`media_state`와 관계없이**) | Web 기록이 `PRESENT`여도 Media에 그룹이 없는 경우다(Media 재설치·설정 복구). 같은 작업 안에서 `camera-groups.create`(충돌이면 `update`) → `PRESENT` → 카메라 작업을 한 번 다시 보낸다. 다시 실패하면 그 결과를 실패로 기록하고 다음 대조에 맡긴다 |
| 사용자 그룹 이름·정렬 변경 | `PRESENT`일 때만 `update`. `GROUP_NOT_FOUND`면 `create`. 확인은 응답 일치 |
| 그 서버에서 그룹의 마지막 카메라가 빠짐(삭제·이동 완료) | groupId로 `camera-groups.delete` → 성공 또는 `GROUP_NOT_FOUND`면 `ABSENT`. `GROUP_HAS_CAMERAS`면 `PRESENT`로 두고 대조가 다시 시도 |
| 대조: `PRESENT`이고 그 서버에 살아 있는 카메라 0 | `delete`(위와 같은 판정) |
| 대조: `ABSENT`인데 그 서버에 살아 있는 카메라 ≥1 | `create`(충돌이면 `update`) → `PRESENT` |
| 대조: `PRESENT`이고 그 서버에 살아 있는 카메라 ≥1인데 목록에 없음 | 빈 그룹 숨김과 실제 부재를 목록만으로 구분할 수 없으므로 `create`를 보낸다. `GROUP_CONFLICT`면 있는 것이므로 `update`로 맞춘다. 어느 쪽이든 `PRESENT` 유지. 이어지는 카메라 대조는 위 `GROUP_NOT_FOUND` 규칙으로도 복구된다 |
| 대조: `PRESENT`이고 목록에 보이는데 이름·정렬이 다름 | `update` |
| 대조: 목록의 groupId가 Web에서 삭제됐거나 다른 서버 소속이거나 `ABSENT` | `cameraCount == 0`이면 `delete`. 카메라가 남아 있으면 카메라 대조가 먼저 정리한 뒤 다음 주기에 |
| 대조: 목록의 groupId가 Web에 전혀 없음 | orphan. 로그만 |
| Web 그룹 삭제(D-4, 카메라 0) | `PRESENT`면 groupId로 `delete` 후 완료, `ABSENT`면 push 없이 완료 |

### 6.6 그룹 삭제

```text
화면 → CameraGroupService.delete(groupId)
     하위 트리의 살아 있는 카메라 수 > 0 → 409 GROUP_HAS_CAMERAS (DB·저널 변경 없음)
     = 0 → 자식 먼저 soft delete + 지도 회수 + 그룹 DELETE 저널
         → media_state가 PRESENT면 camera-groups.delete push(GROUP_NOT_FOUND도 완료), ABSENT면 push 없이 완료
```

## 7. 계약

### 7.1 REST — Media가 호출하는 v2 경로

| v2 경로 (추가) | 기존 경로 (유지) | 인증 |
|---|---|---|
| `POST /api/auth/login` | `/api/v1/auth/login` | 없음 |
| `POST /api/auth/logout` | `/api/v1/auth/logout` | Bearer |
| `POST /api/auth/password` | `/api/v1/auth/password` | Bearer |
| `POST /api/tokens/introspect` | `/api/v1/tokens/introspect` | 지금과 같음(AI 시험용 개방, 10장) |
| `POST /api/servers/{serverId}/bootstrap` | `/api/v1/...` | Bearer, serverId 일치 |
| `POST /api/servers/{serverId}/cameras/fetch` | `/api/v1/...` | Bearer, serverId 일치 |
| `POST /api/servers/{serverId}/streaming-config` | `/api/v1/...` | Bearer, serverId 일치 |
| `POST /api/logs` | `/api/v1/logs` | 지금과 같음 |

브라우저용 세션 경로(`/api/servers` GET·POST, `/api/servers/{serverId}` DELETE, `/api/cameras/**`, `/api/camera-groups/**`, `/api/password/change`,
`/api/platform/status`, `/api/ingest/**`) 중 위 8개와 같은 경로·세그먼트 수를 가진 것은 없다. 게이트웨이(`deploy/nginx/manager.conf.template`)는 `/` 전체를 넘겨 변경이 없다.

제거: `/api/v1/servers/{serverId}/camera-ids`, `/groups`, `/cameras` (D-6).

로그인 요청(Media): `{loginId, password, controlIp, controlPort, streamingBindIp}`. `controlIp`는 필수, IPv4·IPv6 리터럴, `0.0.0.0`·`::` 금지, 최대 45자.
오류: 아이디 없음·비밀번호 오류 → `401 AUTH_FAILED` / 비밀번호 맞음 + 비활성 → `403 ACCOUNT_DISABLED` / 형식 오류 → `400 INVALID_PARAMS`.

bootstrap·`cameras/fetch` 카메라: Web API 5장·9장 예시와 같은 필드, `configRevision = r + 1`. `origin`, `pending`, `useOnvif`는 뺀다. bootstrap `streaming`은 v2 예시대로(`revision`, `updatedAtWallMs`, `server`, `channels`).

### 7.2 WSS — Web이 보내는 action (Media)

| action | 변경 |
|---|---|
| `status.get` | 새로 사용 |
| `camera-assignments.create`·`update` | `configRevision`(r+1), `onvifPort`(저장값), `onvifVideoSourceToken`(null 허용). `useOnvif` 제거. 결과 `applied`, `configRevision` |
| `camera-assignments.list`·`get` | `configRevision` 비교 |
| `camera-assignments.delete` | 대조의 "배정 해제 확정" 경우에도 사용 |
| `camera-groups.create`·`update` | 응답 일치로 확인, `GROUP_CONFLICT`·`GROUP_NOT_FOUND` 처리(6.5) |
| `camera-groups.delete` | 결과 `{groupId}`, 빈 그룹에만 |
| `streaming.server.status` | `streamingToken`, `streamingTokenIssuedAtWallMs` |
| `streaming.server.settings.put`·`get` | `revision` 송신·비교, 결과 `applied` |
| `streaming.channels.list`·`setEnabled` | 채널 대조(6.4) |
| `camera-assignments.sync` | 사용하지 않는다. 명세상 관리자가 명시적으로 쓰는 수동 도구다(Web API 9.3) |

### 7.3 DB (`V19`)

| 대상 | 변경 |
|---|---|
| `CCTV_SOURCE` | `use_onvif` 삭제. `onvif_port INT NOT NULL DEFAULT 80 CHECK 1..65535`, `onvif_video_source_token VARCHAR(64) NULL` 추가 |
| `CAMERA` | `UK_CAMERA_ACTIVE_NAME`을 `(active_camera_name)` 전역 유일로. `rec_assignment_revision`은 그대로(D-2) |
| `CAMERA_GROUP` | `UK_CAMERA_GROUP_ACTIVE_NAME`을 `(rec_server_pk, active_group_name)`으로(D-9). `media_state VARCHAR(10) NOT NULL DEFAULT 'ABSENT'`, `CHECK IN ('ABSENT','PRESENT')` 추가(D-16). 기존 살아 있는 그룹은 `PRESENT`로 초기화한다 — 실제와 다르면 대조가 `delete`(`GROUP_NOT_FOUND` = 완료)나 `create`로 바로잡는다 |
| ID 카운터 | 전역 카운터 `RESOURCE_ID_COUNTER(resource_kind PK, next_id)` 신설. 초기값은 종류별로 **다음 값들 중 최댓값**: 기존 서버별 `next_id`, 할당 원장의 최대 `resource_id` + 1, `CAMERA.rec_camera_id` 최댓값 + 1(카메라), `CAMERA_GROUP.rec_group_id` 최댓값 + 1(그룹). 할당 원장 없이 만들어진 리소스(fixture, `docs/02` §2.2)가 있어도 겹치지 않게 한다. 할당 원장 유일 키를 `(resource_kind, resource_id)` 전역으로. `SERVER_RESOURCE_ID_COUNTER`는 사용 중단 |
| 계약 파일 | `src/test/resources/schema/canonical-manifest.json`, `CanonicalSchemaContractDbIT`(마이그레이션 수·버전 포함) 갱신 |

**사전 조건 (D-12).** `V19`는 첫머리에서 아래를 검사하고, 하나라도 어기면 사유를 담아 실패한다. 실패하면 아무것도 바뀌지 않는다(같은 파일의 DDL 앞에서 멈춘다). 운영자용 같은 내용의 점검 SQL도 함께 둔다.

| 조건 | 이유 |
|---|---|
| 살아 있는 카메라 이름이 전역으로 겹치지 않는다(대소문자 무시) | 새 유일 키 |
| 같은 서버 안에서 살아 있는 그룹 이름이 겹치지 않는다 | D-9 유일 키 |
| 할당 원장에 서버만 다르고 `(resource_kind, resource_id)`가 같은 행이 없다. `CAMERA.rec_camera_id`, `CAMERA_GROUP.rec_group_id`도 서버 간에 겹치지 않는다 | 전역 ID 유일 키 |
| `REC_MUTATION_JOURNAL`에 `PENDING`·`IN_FLIGHT`·`UNKNOWN` 행이 없다 | 이 셋은 스케줄러·복구가 다시 다루는 상태라 옛 스냅샷(`useOnvif`, ONVIF 포트 없음)이 실행될 수 있다. `APPLIED`·`SUPERSEDED`는 기록일 뿐이다. `REJECTED`는 허용한다 — D-15로 수동 재시도가 옛 스냅샷을 실행하지 않고 지금 DB 값으로 새 저널을 만들기 때문이다 |

## 8. 상태와 부작용

| 대상 | 변화 |
|---|---|
| `SERVER.control_ip` | 로그인 body 값. 관측 IP가 Web 로컬 주소면 남기던 경고 로그는 유지 |
| `CAMERA.rec_assignment_revision` | 의미가 "Media에 보내는 번호 − 1"까지 넓어진다. 증가 지점은 그대로. 9장의 재발급에서만 Media 번호에 맞춰 건너뛴다 |
| `STREAMING_CONFIG.revision` | 채널 변경에서는 더 이상 오르지 않는다(D-10) |
| Media 반영 실패·미확인 | Web 값을 되돌리지 않는다(거부·재시작 모두). 카메라는 `PENDING_*` + `rec_last_sync_error`, 그룹은 `FAILED` + `last_sync_error`로 남고, 대조가 맞추면 번호 일치 조건으로 해제한다(D-8, D-13) |
| `REC_MUTATION_JOURNAL` | 재시작 복구는 `UNKNOWN`으로 닫는다. 번호 재발급·수동 재시도는 옛 행을 `SUPERSEDED`로 닫고 새 행을 쓴다(D-14, D-15) |
| `CAMERA_GROUP.media_state` | 그룹이 Media에 올라가 있는지. 첫 카메라 push 직전 `PRESENT`, 마지막 카메라가 빠진 뒤 delete 확인 시 `ABSENT`(D-16) |
| 연결 상태 | READY 대기 동안 `SYNCING`. 변경 저널·대조는 지금처럼 `NOT_READY`로 미뤄진다 |
| Media 쪽 | 대조가 "배정 해제 확정" 카메라를 해제한다(녹화 중지, 파일 보존). orphan은 그대로 둔다. 그룹 delete는 빈 그룹에만 |
| 로그 | `media_control_readiness_wait`(상태가 바뀔 때), `media_orphan_camera`·`media_orphan_group`(상태가 바뀔 때), `ACCOUNT_DISABLED` 로그인 거부 |

## 9. 오류 처리

| 상황 | 처리 |
|---|---|
| 동기화 중 `status.get`이 BOOTSTRAPPING·NOT_READY | WSS 유지, 폴링. 상한 없음(끊길 때만 재접속, Web API 5장) |
| 동기화 중 다른 action이 `SERVER_NOT_READY` | 체인을 READY 대기로 되돌린다. 연결 유지 |
| READY 이후 push가 `SERVER_NOT_READY` | 지금처럼 retryable → 저널 미완료로 재시도(`RecMutationExecutor.java:735-737`) |
| 카메라 `applied:false`, 또는 대조에서 Media 번호 m > r + 1 | D-14: 저장값이 확인 시점과 같을 때만 r := m, 기존 저널 `SUPERSEDED`, 지금 DB 값으로 새 UPDATE 저널(`target_revision = m`, 송신 m + 1). 저장값이 바뀌었으면 재발급하지 않고 새 사용자 변경의 저널에 맡긴다 |
| 스트리밍 `applied:false`, 또는 `settings.get` 번호가 더 큼 | 같은 원칙: 저장 번호가 확인 시점과 같을 때만 Media 번호 + 1로 저장하고 다시 put(D-10, D-14) |
| Web 재시작 시 `IN_FLIGHT` 저널 | `UNKNOWN`으로 닫고 저장값은 유지(D-8②). 다음 대조가 번호로 판정 |
| Media가 create·update 거부(non-retryable) | Web 값 유지, 실패 기록, 대조가 재시도(D-8). 같은 사유로 계속 거부되면 상태가 바뀔 때만 로그 |
| 그룹 `GROUP_CONFLICT`·`GROUP_NOT_FOUND` | 6.5 |
| 카메라 create·이동이 `GROUP_NOT_FOUND` | `media_state`와 관계없이 그룹 create(충돌이면 update) 후 카메라 작업을 한 번 재시도(6.5) |
| 대조 중 이름 충돌(`CAMERA_NAME_CONFLICT`·`GROUP_NAME_CONFLICT`) | 그 항목만 실패 기록, 나머지 계속. Web 쪽 유일 규칙(D-9, 전역 카메라 이름) 때문에 정상 경로에서는 생기지 않는다 |
| 그룹 삭제 요청에 카메라 남음 | `409 GROUP_HAS_CAMERAS`, `details.cameraCount` |
| 카메라 이름 전역 중복, 그룹 이름 서버 내 중복 | `409`(기존 중복 이름 예외 경로, 문구는 범위에 맞게 교체) |
| 로그인 `controlIp` 누락·형식 오류·`0.0.0.0` | `400 INVALID_PARAMS` |
| `V19` 사전 조건 위반 | 마이그레이션 실패, 앱 기동 중단. 사유에 위반 조건과 건수 |

## 10. 보안

- **Media WSS 인증서 검증 생략**은 명세(Media API 3.2)와 메일의 요구다. 범위를 Media WSS 하나로 제한하고, AI WSS·AI HTTP·이벤트 클립 중계의 신뢰 앵커는 그대로 둔다. TLS 암호화와 Bearer 인증은 유지한다. 같은 망에서 Media를 사칭하면 Web 서비스 토큰과 카메라 비밀번호(create·update에 평문)를 받을 수 있다. 폐쇄망 전제로 감수한다(R-1).
- `controlIp`를 body에서 받으므로 유효한 MEDIA 계정을 가진 쪽이 임의 주소를 신고할 수 있다. 계정 비밀번호가 신뢰 경계이고, 대상이 Web 자신의 로컬 주소면 기존 경고 로그가 남는다.
- `/api/v1`의 AI 시험용 무인증 개방은 바꾸지 않는다. v2 경로의 `/api/tokens/introspect`, `/api/logs`도 같은 규칙을 따른다.
- 로그 마스킹(`RecLogContracts`, `RecConnectionManager`의 sanitize)에 `streamingToken`을 추가한다.

## 11. 호환성

| 대상 | 영향 |
|---|---|
| v1.1 Media(실장비·`mock/streaming-server`) | 붙지 않는다. 의도된 결과(상위 계획 3.1-3) |
| AI Server | 영향 없음(D-1, AI WSS 그대로) |
| 관제 화면 | 카메라 모달 ONVIF 칸, 그룹 삭제 확인창 문구, 중복 이름 오류 문구 변경. 그룹 이름은 같은 서버 안에서 겹칠 수 없다(D-9). 그 밖의 동작은 같다 |
| Media 반영 실패 표시 | 지금은 거부되면 화면 값이 이전으로 돌아갔다. 앞으로는 입력한 값이 남고 미반영 상태로 표시된다(D-8) |
| DB | `V19` 전진 전용. 현장은 새 DB, 개발 DB는 사전 조건 충족 후(D-12) |
| 설정 키 | `platform.rec.ca-dir`, `platform.rec.verify-hostname` 제거. 배포 스크립트·문서 참조 정리 |
| 로컬 개발 | `run-local-rec.sh`가 `mock/media-server`를 띄운다. `PLATFORM_REC_PLAINTEXT_LOOPBACK=true`는 그대로 필요 |

## 12. 검증 전략

| ID | 검증 | 수단 |
|---|---|---|
| V-0 | 목업 선행 조건 | B-00 후 `mock/media-server` smoke에 스트리밍 계약 점검(`streamingToken`, `revision` 저장·반환·`applied:false`)과 빈 그룹 숨김 옵션 점검이 들어가 PASS. **이 점검 전의 smoke PASS는 스트리밍 v2 계약 검증으로 쓰지 않는다** |
| V-1 | 단위 회귀 | `./gradlew test jsUnitTest precompileJsp` — 기준선 대비 새 실패 0 |
| V-2 | DB·마이그레이션 | `./gradlew dbIntegrationTest` — `V19` 적용, `canonical-manifest.json`·`CanonicalSchemaContractDbIT` 갱신, 전역 이름·서버 내 그룹 이름·전역 ID 유일, 사전 조건 위반 시 실패(조건별 IT) |
| V-3 | 로그인 | `controlIp` 저장·거부, `ACCOUNT_DISABLED` 순서 |
| V-4 | READY 대기 | `RecControlSimulator`로 BOOTSTRAPPING → NOT_READY → READY, 동기화 중 `SERVER_NOT_READY`에 연결 유지 |
| V-5 | 번호·대조 | `configRevision = r + 1` 변환, 카메라·스트리밍 `applied:false` 재발급, 대조 4가지 경우(특히 orphan 비삭제), 채널 변경 시 번호 불변 |
| V-6 | 실패·재시작 시 유지 | Media 거부 후, 그리고 "저장 → 전송 → 응답 전 종료 → 재시작" 후에도 Web 카메라·그룹 값이 desired로 남는지(D-8 ①②) |
| V-12 | 대조 상태 전이 | `REJECTED`·`UNKNOWN` 리소스가 자동 대조 대상이 되고, 맞춘 뒤 미반영 표시가 풀리는지. Media는 이미 반영됐는데 Web이 미반영으로 남아 있던 경우 번호 일치만으로 풀리는지. 진행 중 저널이 있는 리소스는 건너뛰는지. 대조 도중 사용자 변경이 들어오면 해제가 적용되지 않는지(D-13) |
| V-13 | 번호 재발급 | 저장 4·Media 10 → 저장 10, 옛 저널 `SUPERSEDED`, 새 저널 송신 11, `applied:true`. 재발급 직전 사용자 변경이 들어오면 재발급하지 않는지(D-14) |
| V-14 | 수동 재시도·옛 행 | `REJECTED` 재시도가 번호를 1 올리고 그 번호로 지금 DB 값의 새 저널을 만드는지(유일 키 충돌 없음, 카메라·그룹 모두). `V19` 이전 형식의 `REJECTED` 행이 실행되지 않는지. `V19`가 `PENDING`·`IN_FLIGHT`·`UNKNOWN` 행에서 실패하는지(D-12, D-15) |
| V-15 | 그룹 생명주기 | 빈 그룹 숨김 on/off 양쪽에서: 그룹 생성 시 push 없음, 첫 카메라 직전 create, 마지막 카메라 이동·삭제 뒤 delete, `GROUP_CONFLICT`·`GROUP_NOT_FOUND` 처리, 기존 그룹 `PRESENT` 초기화 후 대조 복원, **`PRESENT` 기록인데 Media 그룹이 없는 경우(목업 재시작) 카메라 create의 `GROUP_NOT_FOUND` → 그룹 create → 카메라 재시도로 복구**(D-16) |
| V-16 | 전역 카운터 초기화 | 원장 없이 만들어진 리소스가 있는 DB에서 초기값이 기존 최대 ID보다 큰지 |
| V-7 | 그룹 확인 | 빈 그룹 숨김 on/off 양쪽에서 생성·수정·`GROUP_CONFLICT`·`GROUP_NOT_FOUND` 처리 |
| V-8 | TLS 분리 | Media 공급자는 자체서명·이름 불일치 인증서를 받고, AI 공급자는 거부 |
| V-9 | 경로·보안 | v2 8개 경로가 Bearer 체인, 브라우저 경로는 세션 체인 |
| V-10 | 종단(로컬) | 앱 + `mock/media-server`(managed): 로그인 → `/ws` → READY(대기·실패 구간 포함) → 카메라 생성·수정·삭제, 그룹 삭제 거부, 목업 재시작 후 대조 복원, orphan 비삭제, 실시간 재생 |
| V-11 | 실장비 | 질문 4 회신 후. 그 전에는 1단계를 "mock 기준 PASS"로만 보고한다 |

## 13. 위험

| ID | 위험 | 대응 |
|---|---|---|
| R-1 | Media WSS 인증서 검증 생략으로 사칭 위험 | 명세 요구. 범위 한정·문서화 |
| R-2 | 실장비 동작이 목업과 다를 수 있음(READY 전이, `applied:false` 조건, 빈 그룹 목록 표시) | 6.5는 두 해석 모두에서 동작하도록 설계. V-11 전에는 완료로 보지 않음 |
| R-3 | 기존 DB가 `V19` 사전 조건을 어김(이름 중복, 서버 간 ID 중복, 미완료 저널) | D-12: 검사 후 즉시 실패. 점검 SQL로 미리 확인. 현장은 새 DB |
| R-4 | 대조가 "배정 해제 확정" 카메라를 해제해 녹화가 멈춤 | Web에서 이미 삭제가 확정된 카메라만 해당. 파일 보존(`stop_keep`). orphan은 건드리지 않음 |
| R-5 | 연결 동기화 체인 변경이 기존 재시도 예산(v1.1 장비의 10.1초 로그아웃 대응)과 얽힘 | READY 대기는 WSS가 열린 뒤의 단계라 업그레이드 재시도와 분리. 기존 테스트 유지 |
| R-6 | 변경 범위가 넓다(추정: `useOnvif`만 main·test 31개 파일, `/ws/v1` 테스트 7개 파일, 여기에 보안·연결·대조·완료 처리·JS·JSP·migration) | Macro에서 독립 검증 가능한 블록으로 나눈다 |
| R-7 | D-8로 Media가 계속 거부하는 값이 Web에 남아 화면과 실제 동작이 어긋날 수 있음 | 미반영 상태와 사유를 화면에 보이는 기존 표시를 유지. 대조가 주기적으로 재시도 |
| R-9 | 그룹 `media_state` 기록이 실제와 어긋남(초기화 직후, 수동으로 Media를 건드린 경우) | 대조가 기록과 카메라 수로 판정하고 `GROUP_NOT_FOUND`·`GROUP_CONFLICT`를 정상 결과로 처리해 다음 주기에 맞춘다 |
| R-10 | D-13으로 대조 대상이 늘어 주기마다 작업량이 커짐 | 일치하면 push하지 않는다(번호 비교). 진행 중 저널은 건너뛴다 |
| R-8 | D-9가 2026-09-03에 연 "다른 상위 그룹 아래 같은 이름"을 다시 막음 | 지금 화면은 1단 그룹이라 실제 영향은 루트 그룹 간 중복뿐이다. 질문 2(나) 회신 시 경로 이름으로 다시 연다 |

## 14. 열린 질문

| ID | 질문 | 처리 |
|---|---|---|
| O-1 | 그룹 계층·경로 이름 | 질문 2 회신 후. 1단계는 D-9 |
| O-2 | `camera-groups.list`가 빈 그룹을 보이는지 | D-16·6.5는 그룹 존재를 목록이 아니라 Web 기록으로 판단하므로 두 해석 모두에서 동작한다. 목업 옵션(D-11)으로 둘 다 시험하고, 실장비로 확정 |
| O-3 | AI가 v2 경로로 언제 옮기는지 | AI 저장소 일정. 옮긴 뒤 `/api/v1` 제거는 별건 |
| O-4 | 중복 이름 오류 문구 | Macro에서 교체안 제시 |
| O-5 | orphan을 화면에 보일지 | 1단계는 로그만. 정리 화면은 2단계 카탈로그와 함께 |

## 15. 기준선

| 검사 | 기준 커밋 | 결과 |
|---|---|---|
| `./gradlew test jsUnitTest precompileJsp` | `b6b1c62` (2026-10-12 이 작업 중 실행) | PASS — test 1751(실패 0, 건너뜀 3: AI 샘플 파일 부재), JS 659, JSP 0 errors |
| `./gradlew dbIntegrationTest` | `b6b1c62` (같은 날) | PASS — 259(실패 0) |
| `mock/streaming-server` smoke | `ca07935` | PASS — 43/43 |
| `mock/media-server` smoke + managed 점검 | `ca07935` | PASS — 계약 17, managed 9, 경계 9. **카메라·그룹·준비 상태만 다루며 스트리밍 v2 계약은 포함하지 않는다** |

`b6b1c62..ca07935`의 차이는 `mock/media-server` 추가뿐이다(`git diff --stat b6b1c62..HEAD -- . ':!mock'` 출력 없음). Java 기준선은 현재 HEAD에도 유효하다.

## 16. 개정 이력

### rev.2 (2026-10-12) — 검토 의견 반영

검토 의견 7건과 추가 지적 2건을 소스·명세로 다시 확인했고 모두 사실이었다. 전부 수용했다.

| 의견 | 확인 근거 | 반영 |
|---|---|---|
| 1. 자동 삭제가 명세와 충돌 | Web API 9.3 대조 표: 카탈로그에 없는 cameraId는 "관리자 확인 전에는 삭제하지 않는다", 배정 해제 확정은 관리자 확인 없이 해제 | D-5, 6.3, R-4 |
| 2. 실패 시 Web 값 유지 누락 | `RecMutationCompletionService.java:69-75`(카메라 `beforeJson` 복원), `119-130`(그룹), `cascadeRejected` | D-8, 5장, 8장, V-6, R-7 |
| 3. 목업으로 스트리밍 계약 검증 불가 | `mock/media-server/src/actions/streaming.js:138-139`(`mediaToken`), `settings.get`에 `revision` 없음 | D-11, V-0, 15장 주석 |
| 4. 스트리밍 번호 정책 불완전 | Web API 10장 "채널 송출 상태는 번호를 올리지 않고"; `RecStreamingConfigService.java:422` → `RecStreamingConfigMapper.xml:121-128`(`revision + 1`) | D-10, 6.4, 9장 |
| 5. O-2 결론 불성립 | `RecMutationExecutor.java:373-430`: 생성 후 목록 재조회로만 확인, 목록에 없으면 수정 `RESOURCE_MISSING` | 6.5 신설, O-2 수정, V-7 |
| 6. `V19` 적용 조건 부족 | 할당 원장 `UNIQUE (server_pk, resource_kind, resource_id)`; 일괄 +1이면 `RecMutationExecutor.java:206-207` `REVISION_SUPERSEDED` | D-2를 "저장값 + 1 변환"으로 바꿔 데이터 변경 없앰, D-12와 7.3 사전 조건 |
| 7. 그룹 이름 지원 조건 필요 | Media API 5장 `GROUP_NAME_CONFLICT`, `V6` 형제 범위 | D-9, R-8 |
| 근거 절 9.4 → 9.3 | Web API "동기화 9.3" 절에 대조 규칙이 있다 | 본문 인용 전부 9.3 |
| `canonical-manifest.json`, `CanonicalSchemaContractDbIT` | `src/test/resources/schema/canonical-manifest.json`, `src/test/java/.../schema/CanonicalSchemaContractDbIT.java` 존재 | D-3, 7.3, V-2 |

의견 6은 검토가 제시한 "일괄 +1 + 저널 처리" 대신 D-2에서 번호 변환으로 풀었다. 저장값을 바꾸지 않으므로 미완료 저널과 어긋날 일이 없다. 다만 미완료 저널의 스냅샷 형식 문제는 남으므로 사전 조건(미완료 행 없음)을 둔다.

### rev.3 (2026-10-12) — 2차 검토 의견 반영

5건 모두 소스·명세로 확인했고 사실이었다. 전부 수용했다. D-9가 한시적 조건이라는 지적도 받아들여 표기했다.

| 의견 | 확인 근거 | 반영 |
|---|---|---|
| 1. 시작 시 복구가 여전히 복원 | `RecMutationRecoveryService.java:108-119`: `IN_FLIGHT` UPDATE를 `REJECTED`로 바꾸며 `rollback` | D-8②, 5장, 9장, V-6 |
| 2. 거부 후 대조 재시도 상태 전이 부족 | `RecMutationOrchestrator.java:294-321`(최신 저널 `APPLIED`만 대상), `:221,234`(저널 상태에서 서버 목록) | D-13 신설, 6.2, 8장, V-12, R-10 |
| 3. 번호 재발급 시 저널 처리 누락 | `RecMutationExecutor.java:206-207` `REVISION_SUPERSEDED`; 저널 상태에 `SUPERSEDED` 있음(`V7` `CK_REC_MUTATION_JOURNAL_STATE`) | D-14 신설(확인 시점 번호 비교, 옛 행 `SUPERSEDED`, 지금 DB 값으로 새 행), 9장, V-13 |
| 4. 옛 스냅샷 차단 불충분, 카운터 초기화 | `CameraMutationService.java:181`(`REJECTED` → `PENDING`, 같은 스냅샷), `docs/02` §2.2(원장 없는 리소스) | D-15 신설, 7.3 사전 조건을 상태별로, 카운터 초기값 계산식, V-14, V-16 |
| 5. 빈 그룹 배포·정리 규칙 | Media API 5장 "담당 카메라가 하나도 없는 그룹은 push받지 않는다", 마지막 카메라가 없어질 때 delete | D-16 신설, 6.5 재작성, 7.3 `media_state`, V-15, R-9 |

### rev.4 (2026-10-12) — 3차 검토 의견 반영

2건 모두 소스로 확인했고 사실이었다. 전부 수용했다.

| 의견 | 확인 근거 | 반영 |
|---|---|---|
| 1. 수동 재시도의 새 저널 번호 규칙 없음 | `V7__rec_mutation_journal.sql:32-33` `UNIQUE (server_pk, resource_kind, resource_id, target_revision)`. D-8로 거부 뒤에도 저장 번호가 옛 `target_revision`과 같다 | D-15: 같은 트랜잭션에서 번호 +1(확인 시점 값 일치 조건) 후 그 번호로 새 저널. V-14 |
| 2. `PRESENT` 기록인데 Media 그룹이 없는 경우 복구 경로 없음 | 6.5는 `ABSENT`일 때만 그룹 create. Media 재설치 후에는 `PRESENT`여도 그룹이 없을 수 있다 | 6.5: 카메라 작업의 `GROUP_NOT_FOUND` → 그룹 create → 카메라 재시도(`media_state` 무관), 대조에 `PRESENT`·카메라 ≥1·목록에 없음 행 추가. 9장, V-15 |

