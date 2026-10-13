# 웹 GPT 연결 실측: 이전 작업 초안 재사용 의심

일자: 2026-10-02

## 최신 증거

새 작업: `62e9ff60-c30b-48bf-92e9-0ff10307e13b`

브라우저 보고: `ChatGPT refused the inserted text (editor=DIV, writable=true, connected=true, draftPresent=true, draftMatches=false, expectedLength=166, actualLength=166, matchingPrefix=17, matchingSuffix=114, visualMatches=false, zeroWidthCount=0)`.

기대 작업 지시문 포맷: `ORCA_WEB_JOB_ID=<UUID>\nROLE=planner\n\n<읽기 전용 시험 지시문>`

`ORCA_WEB_JOB_ID=`는 16자이고 UUID는 36자다. 전체 166자 중 UUID 뒤는 114자. 이전 실패 작업 ID `6a5d073f-b1c1-4367-8a86-8283e58d04d0`과 현재 작업 ID `62e9ff60-c30b-48bf-92e9-0ff10307e13b`는 첫 UUID 글자 `6`만 일치하므로, 두 작업 지시문의 정규화 문자열은 정확히 접두 17자·접미 114자 일치한다. **이전 실패 작업의 초안이 새 탭에 남거나 복원됐을 가능성이 높다.** 단, 브라우저는 초안 전문을 수집/출력하지 않으므로 실제 초안의 정확한 UUID까지 독립적으로 입증한 것은 아니다.

## 보안 및 진행 원칙

- 다른 UUID를 허용하도록 비교를 완화하지 않는다. 잘못된 작업 지시문을 새 작업 ID로 전송하면 실행-결과의 출처가 오염된다.
- 원인이 입증되지 않은 상태에서 모든 사용자 초안을 지우거나 ChatGPT 사이트 저장소 전체를 초기화하지 않는다.
- 우선 이전에 생성한 Orca 전용 테스트용 ChatGPT 탭을 모두 닫고, 새 일반 대화 탭에 복원된 Orca 테스트 초안이 있다면 사용자가 그 초안만 직접 지운다. 잠시 기다린 뒤 페이지를 새로고침해 빈 입력창 유지 여부를 확인하고 로컬 probe를 한 번 실행한다.
- 자동 복구를 구현하려면 Bridge가 같은 역할·지시문을 가진 **종료된 이전 작업**의 UUID를 인증된 확장에 증명하는 식의 좁은 권한 확인이 필요하다. 단순히 다른 UUID가 보인다는 이유만으로 기존 초안을 지우는 것은 여러 동시 작업이나 사용자 작성 중 초안을 훼손할 수 있다.
- 현 시점에는 Chrome+ChatGPT 자동 전송 성공 미확인, Orca의 실제 CLI preflight도 별도로 BLOCKED 상태.
