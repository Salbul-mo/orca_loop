# Web GPT Worker 자동 응답 수집 장애: 실제 증거와 DOM 진단

작성: 2026-10-02

## 확인한 실제 상태

- 실제 자동 전송 및 ChatGPT 답변 생성: **성공**. 작업 ID `56ad9cc5-b88e-41bf-add7-c5e50a4ea55c`의 웹 화면에 정답 `ORCA_WEB_GPT_PROBE_OK`가 표시됐다.
- Bridge 작업: **cancelled**. Python probe에서 설정한 60초 안에 Bridge가 `completed` 상태를 받지 못해 취소했다.
- 이 작업의 실제 대화 ID: `6abf4b2c-94c4-83e8-b20a-12cb6fe8e7c7`. 두 번째로 사용자가 제공한 팝업의 Chat ID와 일치한다.
- 해당 대화의 Bridge journal: `conversation_title` **2개만** 존재. `user_message`, `assistant_message`, `turn_end` 0개. 다른 탭을 보는 문제만은 아니다.
- 확장 팝업: `ChatGPT tab`, `Recording this chat`, `Picked up`, `Sent to app` 모두 정상. 그러나 `App processed`는 원본 COS 전용 `sessionId`를 기준으로 하며 독립 `remote-mcp`의 `/activity`에는 해당 필드가 없어 **영구 대기처럼 보이는 구형 표시**였다. 실제 Web GPT 완료 여부는 Bridge job journal로 판정해야 한다.

## 안전하게 추가한 진단 기능

실제 `remote-mcp/extension/popup.html`, `popup.js`, `popup.css` 수정:
- 팝업 제목을 `remote-mcp companion`으로 변경.
- 구형 단계명 `App processed`를 `Desktop app session`으로 변경하고, 독립 Bridge 환경에서는 `n/a`로 표시. `Sent to app`이 정상일 경우 `Reaching the app`은 Bridge 연결·전달 성공을 표시한다. **Web GPT 결과 성공으로 과장하지 않는다.**
- 팝업 `Advanced → Inspect page structure` 버튼을 추가했다. 클릭 시 현재 활성 ChatGPT 탭의 DOM 선택자와 명시적 역할·메시지 ID 노드 수, `.markdown` 수, 최근 8개 turn의 태그·구조별 노드 수만 수집. 원문 텍스트, DOM HTML, 실제 message ID, token, 대화 URL은 수집하지 않는다. 결과는 `Copy counts` 버튼으로 복사.
- 실제 자동 수집 로직은 DOM 증거 없이 임의로 완화하지 않았다. 기존 `content.js`가 유저 메시지 DOM의 `[data-message-id]`에 의존하고, assistant 본문은 Fiber 전용인 점이 확인됐다. 새 ChatGPT 렌더러가 무엇을 노출하는지 구조 진단으로 먼저 확인해야 한다.

검증: `node --check extension/popup.js`, `npm run typecheck`, `npm run build` 모두 PASS. 관련 Vitest 6개 파일 26개 테스트 PASS. Chrome의 실제 팝업 진단 실행은 **사용자가 확장을 재로드한 후 확인 필요**. Windows 서비스는 재시작하지 않았다.

## 다음 진단 절차

1. 사용자 Chrome의 `chrome://extensions`에서 `remote-mcp companion` 재로드.
2. 기존에 **정답이 이미 표시된 정확한 Worker 대화 ID** `6abf4b2c-94c4-83e8-b20a-12cb6fe8e7c7`를 열고, **그 탭을 새로고침**하여 새 content script를 적용한다.
3. 그 탭에서 확장 아이콘 → `Advanced` → `Inspect page structure` → `Copy counts` 순으로 눌러 생성된 JSON 구조만 제공한다. `--probe`를 다시 수행하지 않는다.
4. 결과를 보고 `legacyTurnSections`, `anyConversationTurn`, `explicitMessageIds`, `explicitUserNodes`, `explicitAssistantNodes`, `markdownBlocks`, `sampleTurnShapes` 기준으로 실제 렌더러에 맞는 최소한의 user/assistant/turn 수집 보완을 구현한다.
5. 그다음 별도 새 probe로 Browser→Bridge→Orca provider 응답 완료까지 검증한다. 실제 Orca CLI 사전 검사와 전체 루프는 이와 별개의 미해결 제약이다.

중요: 이미 `cancelled`된 기존 작업을 답변이 생성됐다는 이유로 수동으로 성공 처리하거나 새로운 작업과 매칭하지 않는다.
