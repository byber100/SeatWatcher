# SeatWatcher 폴더 인덱스

- `README.md`: 외부 사용자와 방문자에게 프로젝트 목적, 지원 기능, 실행 방법, 주의사항을 소개하는 대표 문서
- `docs/`: 사용자와 ChatGPT가 개발 중 요구사항·판정 기준·구현 방향·검증 결과를 맞추는 협업 문서 공간. GitHub Pages 배포 루트와 분리
- `deploy/`: 클라우드 Linux VM 설치 및 systemd 서비스 운영 도구
- `watcher.py`: 반복 감시 실행기, 잔여석/예매 형태 변동 상태 관리, polling 스케줄링, Pushover 단독 운영/테스트 CLI. `watch_targets.local.json`이 있으면 공개 기본 설정보다 우선 사용
- `alert_bundle.py`: 같은 cycle의 변동을 Pushover 소리 정책별 묶음과 512자 이하 Pages 축약 링크 단위로 분리하며 GitHub Pages 상세 payload를 생성
- `seatwatcher.py`: 단일 조회 CLI
- `rail_provider.py`: KORAIL 익명 읽기 전용 직통/환승을 1순위로 조회하고, 직통 KORAIL 장애 시 NAVER degraded fallback과 1시간 재시험 cooldown을 적용. 비공개 target별 `direct_only`, `train_type_prefix`, `arrival_before` 필터 지원
- `bus_providers.py`: KOBUS/티머니/버스타고 조회. 신갈(용인)→동대구는 티머니 경로를 지원
- `last_mile.py`: 장거리 도착 후 최종 이동 가능 여부 판정
- `kakao_notify.py`: 과거 Kakao 연동 보존 모듈. 현재 운영 알림 경로에서는 호출하지 않음
- `pushover_notify.py`: Pushover Message API 전송, `예매 확인` URL 버튼 처리. 유음 방지를 위해 sound는 `vibrate`/`none`만 허용
- `env_loader.py`: OS 환경변수 > `.env.local` > `.env` 순으로 로드하며 빈 템플릿 값은 무시
- `.env`: Git에 추적되는 빈 설정 템플릿. 실제 비밀값 저장 금지
- `.env.local`: 실제 로컬 비밀 설정 파일. Git 추적 금지
- `env.example.txt`: `.env.local`에 복사해 사용할 환경변수 키 예시
- `watch_targets.json`: Git에 추적되는 공개 기본 설정. 실제 개인 일정을 넣지 않음
- `watch_targets.local.json`: 실제 로컬 감시 설정과 중요 직통 Pushover 진동 정책. Git 추적 금지
- `requirements.txt`: Python 의존성
- `.vscode/`: VS Code 실행 설정
- `docs/*.gdoc`: Google Drive 포인터 파일. 문서 원문이 아니므로 Git 추적 제외

외부 소개는 `README.md`, 개발 협업의 세부 기준은 `docs/`에서 관리한다. 실제 인증정보, 런타임 토큰/상태, 개인 이동 일정은 Git 이력에 저장하지 않는다.

Git 작업 기준은 `main` ↔ `origin/main`이다. 기능 검증이 끝나면 로컬 `main`에 커밋하며, GitHub push는 사용자가 원격 반영을 명시했을 때만 수행한다.
