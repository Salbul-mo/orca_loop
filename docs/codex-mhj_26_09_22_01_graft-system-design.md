# Task Report: Graft 도입

**Current Phase:** 4. Code Implementation
**Status:** Implemented; see the usage and validation report

2026-09-22 사용자 지시 "단계 건너뛰고 즉시 도입해봐"에 따라 단계별 승인 절차를 생략했다. 구현 및 검증 결과는 `codex-mhj_26_09_22_07_graft-usage.md`를 참조한다. 아래 내용은 최초 설계 기록이다.

## 1. Context and Objective

- Goal: 이 저장소의 코드 탐색에 Graft를 시범 도입하고, 실제 Python 구조 분석 및 검색 결과를 검증한다.
- Scope: 개발 보조용 선택적 CLI, 저장소별 캐시, 사용 안내 및 로컬 검증. 이번 도입에서 worker 자동 실행이나 Coordinator 계약은 변경하지 않는다.
- 기준: Python 표준 라이브러리 기반 harness 실행은 Graft 설치 여부에 영향받지 않아야 한다.

## 2. Deliverables (Current Phase Focused)

### 도입 방식

1. 구현 단계에서 npm 배포 정보와 CLI 도움말을 확인하고 검증할 버전을 고정한다. 전역 설치 대신 격리된 도구 설치 위치를 사용한다.
2. 구조 분석만 실행한다. LLM을 호출하는 deep 분석은 범위에 포함하지 않는다.
3. 자동 init 대신 필요한 CLI 명령을 직접 구성한다. 기존 AGENTS.md와 사용자 전역 MCP 및 hook 설정을 자동 변경하지 않는다.
4. 새로 만드는 저장소 내 안내 파일과 실행 보조 파일 및 캐시의 최상위 디렉터리에는 codex-mhj_26_09_22_<sequence>_ 접두사를 적용한다. Graft 내부 생성 파일명은 도구 형식이므로 해당 디렉터리 안에서 유지한다. 이 해석은 설계 승인 범위에 포함한다.
5. 캐시 위치는 Graft의 --dir 지원을 실제 검증한 뒤 확정한다. .gitignore 자동 변경 여부도 확인하고 필요한 정확한 경로만 제외한다.
6. Python 코드의 함수 검색, 파일 API 조회, 호출 관계 조회를 실행하고 원본 코드와 대조한다. 검색 보조 정보는 테스트 통과나 검토 승인 근거를 대신하지 않는다.

### 후속 통합 경계

이번 시범 도입 이후 worker 자동 연동이 필요하면 별도 범위로 설계한다. 현재 run_loop.py의 입력 수집과 transport.py의 StagedInput 및 inputs.sha256가 후보 연결 지점이다.

- planner 및 implementer부터 선택적으로 적용한다.
- plan_reviewer의 계획 문서 중심 판정 원칙을 유지한다.
- code_reviewer는 frozen diff를 근거로 판정하며, 조회 컨텍스트는 동일한 snapshot_digest의 코드에서 생성되어야 한다.
- readonly.py는 .git을 제외한 복사본을 잠근다. reviewer에서 Graft 재색인이나 자동 갱신을 실행하는 방식을 사용하지 않는다.
- 사전 생성 결과를 읽기 전용 입력으로 전달하는 방식은 별도 검증한다.

## 3. Validation and Risks

- PASS: git status --short --branch에서 main 및 clean 상태 확인.
- PASS: transport.py, config.py, generation.py 및 run_loop.py의 입력 수집과 읽기 전용 복사본 연결 경로 확인.
- PASS: Node.js 및 npm 실행 파일 발견. graft 실행 파일은 현재 PATH에서 발견되지 않음.
- NOT RUN: Graft 설치, 구조 분석, 검색, Windows 호환성 검증 및 harness 회귀 테스트.
- Risk: tree-sitter 의존성의 Windows 설치 및 동작은 실제 설치 전에는 보장하지 않는다.
- Risk: Graft 자동 갱신과 .gitignore 변경은 읽기 전용 정책 및 snapshot에 영향을 줄 수 있다.
- Risk: 현재 저장소에서의 탐색 시간과 토큰 절감은 측정 전이며 도입 효과를 단정하지 않는다.

## 4. Approval Status

- [ ] Current phase approved
- [ ] Revision requested
- [ ] Permission granted to proceed to the next phase

사용자의 후속 명시 지시로 단계별 승인 절차를 생략하고 직접 구현했다.
