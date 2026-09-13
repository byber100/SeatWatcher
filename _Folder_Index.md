# SeatWatcher 폴더 인덱스

- `README.md`: 외부 사용자와 방문자에게 프로젝트 목적, 지원 기능, 실행 방법, 주의사항을 소개하는 대표 문서
- `docs/`: 사용자와 ChatGPT가 개발 중 요구사항·판정 기준·구현 방향·검증 결과를 맞추는 협업 문서 공간
- `watcher.py`: 반복 감시 실행기와 알림 상태 관리. `watch_targets.local.json`이 있으면 공개 기본 설정보다 우선 사용
- `seatwatcher.py`: 단일 조회 CLI
- `rail_provider.py`: KORAIL 직통/환승 조회
- `bus_providers.py`: KOBUS/티머니/버스타고 조회
- `last_mile.py`: 장거리 도착 후 최종 이동 가능 여부 판정
- `kakao_notify.py`: Kakao 알림 연동
- `env_loader.py`: OS 환경변수 > `.env.local` > `.env` 순으로 로드하며 빈 템플릿 값은 무시
- `.env`: Git에 추적되는 빈 설정 템플릿. 실제 비밀값 저장 금지
- `.env.local`: 실제 로컬 비밀 설정 파일. Git 추적 금지
- `env.example.txt`: `.env.local`에 복사해 사용할 환경변수 키 예시
- `watch_targets.json`: Git에 추적되는 공개 기본 설정. 실제 개인 일정을 넣지 않음
- `watch_targets.local.json`: 실제 로컬 감시 설정. Git 추적 금지
- `requirements.txt`: Python 의존성
- `.vscode/`: VS Code 실행 설정
- `docs/*.gdoc`: Google Drive 포인터 파일. 문서 원문이 아니므로 Git 추적 제외

외부 소개는 `README.md`, 개발 협업의 세부 기준은 `docs/`에서 관리한다. 실제 인증정보, 런타임 토큰/상태, 개인 이동 일정은 Git 이력에 저장하지 않는다.

Git 작업 기준은 `main` ↔ `origin/main`이며 기본 순서는 `pull --ff-only → 로컬 수정 → 검증 → commit → push`다.
