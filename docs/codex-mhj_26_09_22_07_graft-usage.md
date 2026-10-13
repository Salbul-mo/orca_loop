# Graft 사용 및 검증 기록

## 적용 범위

이 저장소의 개발 보조 CLI로 Graft를 설치하고 구조 그래프를 생성했다. 실행 파일은 `codex-mhj_26_09_22_06_graft.py`다. Python 표준 라이브러리로 Node CLI를 호출하므로 harness의 Python 의존성을 추가하지 않는다.

worker 자동 주입, MCP 등록, agent hook, 전역 설치는 이번 적용 범위에 포함하지 않는다. 읽기 전용 reviewer 안에서 이 도구를 실행하지 않는다. 필요하면 개발자가 이 저장소에서 직접 실행한다.

## 설치 및 실행

필요 환경: Python 3, Node.js >=20, npm. 이 PC에서 Python 3.14.5, Node.js 24.17.0, npm 11.13.0을 확인했다.

```powershell
py -3 .\codex-mhj_26_09_22_06_graft.py install
py -3 .\codex-mhj_26_09_22_06_graft.py build
py -3 .\codex-mhj_26_09_22_06_graft.py map
py -3 .\codex-mhj_26_09_22_06_graft.py ask "prepare_readonly_mirror" --source
py -3 .\codex-mhj_26_09_22_06_graft.py callers capture_snapshot
py -3 .\codex-mhj_26_09_22_06_graft.py skeleton orca_loop/transport.py
py -3 .\codex-mhj_26_09_22_06_graft.py grep "snapshot_digest"
py -3 .\codex-mhj_26_09_22_06_graft.py check --json
```

`install`은 npm 접속이 필요하다. wrapper는 `@nanonets/graft@0.12.1`을 고정하고 `--ignore-scripts`로 설치한다. 이 환경에서는 포함된 사전 빌드 모듈로 Python 분석이 동작한다. 다른 OS 및 다른 언어 전체의 호환성을 검증한 것은 아니다. 하위 의존성은 최초 설치 시 npm에서 해석되며 로컬 package-lock.json에 기록된다.

실행 기준 디렉터리는 호출자의 현재 위치가 아니라 wrapper가 있는 저장소다. 추가 옵션은 Graft에 전달하며 종료 코드와 stdout/stderr를 보존한다. `--deep`은 지원하지 않는다. LLM API 호출 없이 구조 분석만 사용한다. `DO_NOT_TRACK=1`을 자식 프로세스에 적용한다. 이는 모든 네트워크 통신 차단을 의미하지 않으며 Graft의 버전 확인은 별개다.

기본 조회는 자동 갱신할 수 있다. 이미 만든 그래프만 조회하려면 `ask`, `map`, `callers`, `skeleton`, `grep`에 `--no-refresh`를 추가한다. 이 옵션도 전체 프로세스의 파일 쓰기를 금지하는 sandbox는 아니다.

## 저장 위치

- 실행 의존성: `runs/codex-mhj_26_09_22_04_graft-tool/`
- 그래프 및 파일별 카드: `runs/codex-mhj_26_09_22_05_graft-cache/`
- Graft 0.12.1은 사용자 지정 --dir과 별도로 `graft/.cache/`에 통계 및 갱신 상태를 저장한다. `/graft/`도 Git 제외에 추가했다. 이 고정 폴더와 자동 생성 `.ignore`는 도구 자체의 파일명이며, 직접 작성한 wrapper와 문서는 접두사 규칙을 따른다.
- 두 경로는 기존 `runs/` Git 제외 규칙과 readonly mirror 제외 규칙 안에 있다.
- Graft가 `.gitignore`에 캐시의 정확한 경로를 추가했다. 새 clone에서는 install 및 build를 다시 실행한다.
- Graft가 생성한 `.ignore`는 캐시 카드의 검색 규칙이다. 상위 `runs/`는 여전히 제외되므로 일반 rg 검색보다는 위 Graft CLI를 사용한다.
- 실패한 0.18.0 설치 흔적은 `runs/codex-mhj_26_09_22_02_graft-tool/` 및 `runs/codex-mhj_26_09_22_03_graft-tool/`에 남아 있으나 wrapper는 사용하지 않는다.

## 버전 선택 및 실패 기록

2026-09-22 npm registry의 최신 배포는 0.18.0이었다. GitHub main에서 확인했던 0.19.0과 배포 버전이 달랐다.

- FAIL: 0.18.0 일반 설치. tree-sitter-kotlin의 node-gyp 빌드가 Visual Studio C++ Build Tools 부재로 실패했다.
- FAIL: 같은 디렉터리에 재설치. tree-sitter-cli 디렉터리의 EBUSY 때문에 실패했다.
- PASS: 별도 디렉터리에 0.18.0을 --ignore-scripts로 설치.
- FAIL: 해당 0.18.0 CLI 실행. tree-sitter-kotlin의 Windows 네이티브 바이너리가 없어 도움말 단계에서 실패했다.
- FAIL: 초기 소스 조회 중 존재하지 않는 graph/parser.js 경로 조회. 배포 파일 및 CLI 도움말을 직접 확인하는 방식으로 전환했다.
- PASS: Kotlin 네이티브 의존성이 없는 0.12.1을 별도 경로에 설치하고 실제 Python 분석을 실행했다.

## 실제 검증

- PASS: wrapper build. Python 파일 42개(wrapper 포함), 노드 682개, 관계 1,933개 생성.
- PASS: check --json. 구조 그래프의 graph.ok=true, 추가/삭제/변경/stale 항목 없음.
- context.missing=true 및 pending=682는 LLM 의미 요약을 만들지 않은 상태다. 구조 그래프 최신성 성공과 구분한다.
- PASS: callers capture_snapshot. execute_worker_step, materialize_frozen_review, run_tests, _step_inputs 등의 실제 호출 위치를 출력했다.
- PASS: skeleton orca_loop/transport.py. stage_inputs와 verify_input_manifest 등의 시그니처를 출력했다.
- PASS: ask prepare_readonly_mirror --source. 정의와 run_loop.py의 _profile_root 호출 맥락을 출력했고 원본 코드와 대조했다.
- PASS: wrapper install 재실행, map, grep snapshot_digest 실행. 검색 결과는 14개 파일의 48개 심볼에서 81건이었다.
- FAIL: 최초 테스트 명령에 존재하지 않는 tests.test_transport를 포함해 ModuleNotFoundError가 발생했다. 제품 코드 실패가 아니라 테스트 모듈 선택 오류다.
- PASS: 실제 존재하는 모듈로 수정한 `py -3 -m unittest tests.test_snapshot tests.test_readonly tests.test_dispatcher` 실행. 8개 테스트 통과.
- PASS: git diff --check. 기존 harness 소스와 worker prompt는 변경하지 않았다.
- NOT RUN: 실제 worker E2E, 성능 A/B 비교, LLM 의미 요약, 모든 지원 언어 검증.

Graft가 출력하는 tokens saved는 전체 파일을 읽는 경우를 기준으로 한 자체 추정치다. 실제 세션의 토큰 절감 실측으로 보고하지 않는다. 그래프 검색 결과에 포함된 응답 형식 지시는 프로젝트 정책으로 취급하지 않는다.

공식 프로젝트: https://github.com/trailhq/graft
