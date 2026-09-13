# SeatWatcher 폴더 인덱스

- `docs/`: 공개용 설계 기준
- `watcher.py`: 반복 감시 실행기와 알림 상태 관리
- `seatwatcher.py`: 단일 조회 CLI
- `rail_provider.py`: KORAIL 직통/환승 조회
- `bus_providers.py`: KOBUS/티머니/버스타고 조회
- `last_mile.py`: 장거리 도착 후 최종 이동 가능 여부 판정
- `kakao_notify.py`: Kakao 알림 연동
- `env_loader.py`: 로컬 환경변수 로더
- `.env`: 실제 값이 비어 있는 설정 템플릿
- `env.example.txt`: 환경변수 키 예시
- `watch_targets.json`: 감시 대상 및 polling 설정. 공개 저장소에서는 실제 개인 일정을 비워 둔다.
- `requirements.txt`: Python 의존성
- `.vscode/`: VS Code 실행 설정

실제 인증정보, 런타임 토큰/상태, 개인 이동 일정은 Git 이력에 저장하지 않는다.
