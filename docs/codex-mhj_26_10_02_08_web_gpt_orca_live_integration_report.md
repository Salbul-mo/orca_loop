# Web GPT를 Orca Master·Worker로 사용하는 실행 경로와 실측 결과

작성: 2026-10-02 (KST)  
대상: `C:\Users\mhj\Desktop\mhj_workspace\personal\orca_harness`  
웹 에이전트 호스트: `C:\Users\mhj\Desktop\mhj_workspace\personal\remote-mcp`

## 1. 구현 상태

기존 Orca coordinator와 테스트·리뷰·인간 승인 경로는 그대로 사용한다. 오직 **Master와 모든 Worker의 모델 실행 공급자**를 `web_gpt`로 바꾼다. `remote-mcp`는 독립적인 웹 ChatGPT 작업 실행기이며, 원본 Chat On Steroids 애플리케이션을 실행할 필요는 없다.

- 기존에 있던 `AgentProvider.WEB_GPT` 및 `tools/web_gpt_provider.py` 연동을 재사용했다.
- `configs/web-gpt-master.json`: Master 공급자 사전 설정.
- `tools/run_web_gpt_loop.py`: `--master-config` 및 네 Worker 슬롯 전부에 `web_gpt`를 지정하는 얇은 실행 진입점. 나머지 작업 상태·권한·테스트 검증은 `run_loop.py`에 위임한다.
- `--check-config`: Orca 프로세스, 브라우저 또는 토큰 없이 Master + 네 Worker 설정을 검증한다.
- `--probe`: 실제 브라우저에 읽기 전용 단발 Planner 작업을 제출하고 정확한 응답을 검사한다. 브라우저 탭을 새로 만들므로 실제 Chrome 확장 로드가 필요하다.
- `run_loop.py`: Orca 실행 파일이 사전 검사 시 없으면 런타임 `FAIL`이 아닌 `BLOCKED`로 보고한다.
- `tests/test_web_gpt_launch.py`: 설정·모의 Orca 사전 검사·미설정 토큰·실제 WorkerRunner 산출물 SHA-256 경계·Orca CLI 부재 확인을 검증한다.
- `remote-mcp/extension/chatgpt-dom.js`: 기존 `#prompt-textarea` 외에도 **form 내부에서 고유한 편집 요소임이 확인되는 경우만** 대체 composer 선택자를 허용한다. 다수의 후보가 있으면 오입력 위험 때문에 거절한다.
- `remote-mcp/extension/content.js`: 백그라운드 탭 초기화를 고려하여 입력창 대기 시간을 12초에서 35초로 늘리고, 실패 시 URL·대화 내용·토큰을 노출하지 않는 최소 진단 정보를 반환한다.

## 2. 실제 검증과 미완료 항목을 구분

| 확인 대상 | 실행 결과 |
|---|---|
| `run_web_gpt_loop.py --check-config` | PASS, Master + 네 Worker 모두 `web_gpt` |
| 임시로 별도 생성한 깨끗한 Git 저장소에서 Orca 드라이런 | **BLOCKED**: 해당 서비스 실행 계정의 PATH에 진짜 Orca CLI `orca` 없음 |
| 모의 Orca CLI 상태·터미널과 실제 `run_loop.main(... --dry-run)` 연결 | PASS (테스트 전용 mock, 실제 Orca CLI 아님) |
| 모의 Bridge를 사용한 `worker_runner.py → web_gpt_provider.py → 산출물/worker_done` | PASS (모의 HTTP 서비스, 실제 브라우저 아님) |
| 이전 버전 remote-mcp를 통한 실제 브라우저 Planner 단발 테스트 | **FAIL**: `ChatGPT never exposed a usable composer for bootstrap` |
| 확장 코드의 보수적 composer 선택 및 진단 개선 | 코드·구문 검사 및 DOM 단위 테스트 PASS. **설치된 확장 재로드 후 실브라우저 재시험 필요** |
| Orca 전체 회귀 테스트 | 문서 최종 작성 전 130개 통과; 최신 결과는 아래 검사 명령으로 확인 |
| remote-mcp 관련 검사 | TypeScript 타입 검사·빌드 PASS, 대상 7개 테스트 파일 59개 PASS |

첫 실브라우저 테스트의 작업 ID: `8ee85b4b-e8f9-4e7c-87e5-d28d3619e093` (실패 상태). 이 결과를 성공이나 전체 루프 기동으로 해석해서는 안 된다.

추가 장애: 사용자 컴퓨터에는 Orca 데스크톱 애플리케이션 `Orca.exe` 버전 **1.4.218.0**이 설치되어 있지만, 서비스 실행 계정에서 해당 GUI 앱에 `status --json` 명령을 전달하는 방식은 정상 CLI 응답을 반환하지 않았다. 코드에 고정된 Orca CLI 확인 버전은 **1.4.159**다. Orca 앱 파일이 있다는 이유만으로 CLI 사용 가능 여부 또는 버전 호환성을 인정하지 않는다.

## 3. 순서대로 실사용 검증

### A. Chrome 확장 새 코드 적용

1. 브라우저 `chrome://extensions`에서 이 프로젝트의 `remote-mcp companion`을 **재로드**한다.
2. 원본 COS 확장은 동시에 활성화하지 않는다.
3. 이 Chrome 프로필로 `https://chatgpt.com/`의 **새 대화**를 열고 입력창이 실제로 렌더링되는지 확인한다. 로그인·보안 확인 페이지가 나오면 먼저 정상 로그인한다.
4. 일반 PowerShell에서 Bridge 응답을 확인한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8765/hello -Headers @{'x-extension-protocol'='9'}
```

실행 중인 `remote-mcp` 서비스와 원본 COS 앱은 같은 포트를 점유하지 않도록 한다. 확장 코드만 변경했으므로 **서비스 자체는 재시작하지 않았다**.

### B. Orca와 무관한 웹 GPT 단발 실제 시험

PowerShell에 `remote-mcp\.env`의 `MCP_WEB_AGENT_CONTROL_TOKEN`과 같은 토큰을 다음 환경 변수로 설정한다. 값을 채팅·출력·저장소·로그에 노출하지 않는다.

```powershell
$env:ORCA_WEB_AGENT_BRIDGE_URL = 'http://127.0.0.1:8765'
$env:ORCA_WEB_AGENT_TOKEN = '<remote-mcp에서 설정한 토큰>'
cd C:\Users\mhj\Desktop\mhj_workspace\personal\orca_harness
.\.venv\Scripts\python.exe tools\run_web_gpt_loop.py --check-config
.\.venv\Scripts\python.exe tools\run_web_gpt_loop.py --probe
```

`--probe`는 실제 새 ChatGPT 탭에서 `ORCA_WEB_GPT_PROBE_OK`가 반환되어야 PASS다. 실패 시 remote-mcp의 해당 작업 에러 메시지를 확인한다. 인증 페이지와 composer 선택자 문제를 구분할 수 있도록 확장 오류 메시지를 개선했다.

### C. 실제 Orca CLI 및 Coordinator 준비

Orca 데스크톱 GUI와 **터미널 제어 JSON을 지원하는 Orca CLI**는 구분한다. 정상 CLI를 찾아 `ORCA_CLI_COMMAND`로 지정한 뒤 아래 명령이 JSON 상태를 반환하는지 확인한다.

```powershell
$env:ORCA_CLI_COMMAND = '<검증된 실제 Orca CLI 실행 파일 절대 경로>'
& $env:ORCA_CLI_COMMAND status --json
```

상태의 `runtime`과 `graph`가 모두 ready여야 한다. `runtime.appVersion`이 현재 코드의 고정 버전 `1.4.159`와 다를 경우 바로 값을 바꾸지 말고 응답 스키마·Orca CLI와 Harness 버전 호환성을 먼저 검증한다. 실제 Orca 안에서 Coordinator Terminal handle도 준비해야 한다.

### D. 임시 테스트 Git 저장소로 전체 루프 실행

사용자 데이터나 회사 프로젝트가 아닌 **깨끗한 별도 테스트 저장소**와 기존 Orca Coordinator Terminal을 사용한다.

```powershell
cd C:\Users\mhj\Desktop\mhj_workspace\personal\orca_harness

.\.venv\Scripts\python.exe tools\run_web_gpt_loop.py `
  --request 'C:\path\to\isolated-test-repo\request.md' `
  --worktree 'C:\path\to\isolated-test-repo' `
  --coordinator-handle '<Orca 실제 Coordinator 터미널 handle>' `
  --dry-run
```

사전 검사 PASS 후 `--dry-run`을 제거해 실제 루프를 실행한다. `ORCA_WEB_AGENT_TOKEN`과 Bridge URL은 위 B에서 지정한 PowerShell 세션의 환경 변수로 전달된다. `--test-policy`는 실제 테스트가 필요하면 명시적으로 제공한다. 프리셋은 **읽기 전용 권한이나 호스트 파일 수정 방지를 자동 보장하지 않는다**. 현재 remote-mcp에는 대화별 호스트 파일/프로세스 권한 경계가 완성되지 않았으므로 실무·회사 저장소에 바로 적용하지 않는다.

## 4. 재현 가능한 회귀 검사

```powershell
# orca_harness
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest tests -q

# remote-mcp
npm run typecheck
npm run build
.\node_modules\.bin\vitest.cmd run test\dom-composer.test.ts test\web-agent.test.ts test\generic-agents.test.ts test\agents-tool.integration.test.ts test\bridge-extension.test.ts test\bridge.integration.test.ts test\bridge-compaction.test.ts
```

기존 작업 트리, `.env`, 토큰, 이미 실행 중인 Windows 서비스, Chrome 사용자 세션은 보존한다. Git 커밋·푸시나 운영 서비스 재시작은 이 단계에서 수행하지 않았다.

## 5. 확장 재로드 후 실제 브라우저 재시험 (2026-10-02)

사용자의 첫 재로드 후 PowerShell 시험에서 신규 Planner 작업 ID `e91b44a1-3bb3-4a64-a621-59de8a3ebc0c`가 발급되었다. Bridge 저장 상태를 확인한 결과, 해당 작업은 `failed`, 명령은 `acked`이고 정확한 오류는 `ChatGPT refused the inserted text`였다. 즉, 전 단계의 입력창 탐색 실패는 넘어섰지만 텍스트 삽입에 실패했다.

이에 따라 `remote-mcp/extension/chatgpt-dom.js`의 입력 로직을 보강했다. 기본 `document.execCommand`가 실패하고 기존 입력값이 빈 상태임이 확인되는 경우에만 실제 textarea의 네이티브 setter 또는 정확한 form 내부 contenteditable에 범위가 제한된 보조 삽입을 시도한다. 메서드 실패·초안이 존재하는 경우 무단 덮어쓰지 않으며, `content.js`의 후속 재검증 및 실제 ChatGPT 전송 수락 확인은 유지한다. `#prompt-textarea`가 비편집 wrapper인 버전은 단일 내부 편집 호스트가 명확할 때만 선택하도록 했다. 오류가 재발하면 편집기 종류·편집 가능 여부 등 개인정보가 아닌 최소 진단 정보만 보고하도록 했다.

신규 DOM 단위 테스트와 Bridge/MCP 관련 테스트 7개 파일, **42개 통과** 및 TypeScript 타입 검사 통과. 단, 이 재수정 이후 실제 Chrome+ChatGPT의 수락 확인은 아직 수행되지 않았다. 수정된 확장을 **다시 재로드**한 후, 사용자의 로컬 PowerShell에서 `& "C:\Users\mhj\Desktop\mhj_workspace\personal\orca_harness\tools\probe_remote_mcp.ps1"`로 재시험한다. 토큰을 채팅·로그에 출력하지 않는다.

## 6. 두 번째 확장 재로드 후 실제 전송 단계 점검

실제 작업 `6a5d073f-b1c1-4367-8a86-8283e58d04d0`은 `failed`이며 Bridge 오류는 `ChatGPT did not accept the bootstrap send`였다. 이는 신규 ChatGPT 탭 생성과 편집창 입력에 이어, 최종 제출 수락 단계에서 정지한 것이다. 실패한 탭에서 사용자가 수동으로 제출해도 이미 실패한 Bridge job과 자동 연결되지 않는다.

`remote-mcp/extension/chatgpt-dom.js`의 전송 컨트롤을 현재/이전 ChatGPT `send-button`/`composer-send-button` 및 Send/전송/보내기 레이블로 찾도록 넓혔으며, **현재 입력창을 포함하는 동일 form**만 대상으로 한다. 명명된 버튼이 없을 때에만 동일 form의 고유한 `button[type=submit]`을 선택한다. 중복·비활성·form 없음은 실패 폐쇄하며, 기존의 신뢰 불가능한 synthetic Enter 이벤트를 제거했다. `content.js`는 재실패 시 개인 내용 없이 `control=ready|disabled|missing|ambiguous|form-missing` 및 입력 초안 존재 여부만 오류로 반환한다.

확장 JavaScript 구문 검사 및 TypeScript 타입 검사 PASS, 관련 Vitest 8개 파일·45개 테스트 PASS, 빌드 PASS. 브라우저에서의 실제 전송/응답/Orca 전체 루프 완료는 **아직 재검증되지 않음**. Chrome 확장을 다시 재로드한 뒤, 로컬 PowerShell의 `tools/probe_remote_mcp.ps1`로 새 작업을 생성해야 한다. 이전 실패 탭은 재사용하지 않는다.

## 7. 최신 Chrome 재시험: 삽입 후 오진(false negative) 보강

사용자가 제시한 화면의 `ORCA_WEB_JOB_ID=6a5d073f-b1c1-4367-8a86-8283e58d04d0`는 이전 실패 작업이다. 자동 생성되는 일반 Worker 탭은 `active:false`로 백그라운드에서 열리므로, 계속 표시 중인 예전 탭의 상태로 신규 작업의 성공·실패를 판단할 수 없다.

최신 별도 작업 `176de2fb-f186-4ba1-9b51-179117024418`는 `failed`: `ChatGPT refused the inserted text (editor=DIV, writable=true, connected=true, draftPresent=true)`. 이는 입력값이 존재하더라도 `insertPrompt()`가 false를 반환한 사례이며, 실제 제출까지는 도달하지 못했다. 명확한 예외 발생 지점은 실브라우저 로그만으로 확인되지 않았다.

`remote-mcp/extension/chatgpt-dom.js`는 editor `input` 이벤트 처리 중 예외가 발생해도 **현재 연결된 입력 요소에 작업 내용 전체가 정확히 있는 경우만** true로 인정한다. 부분 삽입은 false로 처리한다. `remote-mcp/extension/content.js`도 삽입 함수 false 시 현재 입력 요소의 전체 내용이 작업 메시지와 정확히 일치하는 경우에만 후속 미세 작업·일치성 확인을 진행한다. 초안 또는 작업 내용을 로그에 기록하지 않고 `draftMatches` 불리언만 진단 정보에 추가했다.

삽입 예외/부분 삽입 단위 테스트를 추가했으며 확장 관련 테스트 8개 파일, 47개 테스트 및 TypeScript 타입 검사 PASS. 새 코드의 실제 Chrome 동작 검증은 아직 필요하다. Chrome에서 확장 재로드 뒤 기존 실패 작업 탭을 닫고 로컬 단발 `probe_remote_mcp.ps1`을 새로 실행한다. **이번 테스트의 콘솔에 나온 신규 job ID**를 기준으로 성공 여부를 확인한다.

## 8. 최신 브라우저 시험과 초안 비교 진단 (2026-10-02 15:00 이후)

사용자가 실행한 작업 `7942dab9-7dc7-43d4-8e2d-2ee6c8330e0c`는 `failed`이고 Bridge가 저장한 정확한 오류는 `ChatGPT refused the inserted text (editor=DIV, writable=true, connected=true, draftPresent=true, draftMatches=false)`였다. 새로 만든 ChatGPT 페이지에 초안은 존재하나 전송 전에 확장이 원본 작업과 다르다고 판정한 상태다. `draftMatches=false`만으로 **원본 내용이 잘린 것인지, DOM 특유의 보이지 않는 문자가 들어간 것인지 단정할 수 없다**.

`remote-mcp/extension/chatgpt-dom.js`에 공통 `compactDraft`/`composerMatches`를 도입해 삽입 직후와 bootstrap 전송 직전, 그리고 명시적 `send(boot.text)`의 진입·클릭 전에서 동일한 검증을 수행한다. 인쇄 가능한 실제 문자는 전체 일치를 요구하며, 원본 메시지에 없는 경우에 한해서만 편집기 포맷용 zero-width 문자와 기존 비교가 이미 무시하던 표시 공백을 정규화한다. 기존 사용자가 작성한 초안은 계속 보호하고 접두사만 일치하거나 다른 문자가 섞인 메시지는 절대 자동 전송하지 않는다.

실제 일치가 아닌 경우 Chrome 확장은 다음 오류에 예상/실제 정규화 길이, 일치 접두사·접미사 길이, 화면 표시 문자열 일치 여부, zero-width 문자 개수 등 **내용이 아닌 구조적 숫자/불리언**만 전송한다. 브라우저가 제공하는 `innerText`가 더 비슷하더라도 `textContent`에 추가 인쇄 가능 문자가 있으면 자동 전송하도록 완화하지 않았다. 위 증거를 확보해야 원인별 다음 수정을 결정할 수 있다.

테스트: extension `node --check` PASS, TypeScript `npm run typecheck` PASS, `npm run build` PASS, 관련 Vitest 9개 파일 **50 passed**(전체 프로젝트 모든 테스트 실행은 아님). `test/dom-send-submit.test.ts`는 정확한 대상 메시지에 대해서만 동일 composer form의 전송 버튼을 클릭하며 한 글자라도 다른 초안이면 클릭하지 않는 경계를 검증했다. 수정 후 실제 Chrome E2E 결과는 아직 없음. 새로운 작업 테스트 이전에 Chrome 확장 reload가 필요하며 **실패한 이전 작업 탭에서 전송을 누르면 새 작업 결과가 연결되지 않음**.
