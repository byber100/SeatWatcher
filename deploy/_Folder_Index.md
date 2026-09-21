# SeatWatcher deploy 인덱스

- `cloud_vm_manage.py`: Ubuntu 계열 클라우드 VM에서 Python 가상환경 설치, 비공개 운영 파일 존재 확인, systemd 서비스 등록·시작·중지·재시작·로그 확인·업데이트를 수행한다.

이 폴더에는 배포/운영 자동화만 둔다. 실제 인증정보와 개인 감시 일정은 저장하지 않는다. 현재 운영 알림은 Pushover만 사용한다.

- `control_plane_manage.py`: Google Drive 모바일 제어면용 `.control-venv`, rclone, Playwright와 `seatwatcher-control.service` 설치·운영
- `control_requirements.txt`: 모바일 제어면 전용 Python 의존성(openpyxl, Playwright)
- `korail_headless_check.py`: 현재 운영 철도 target을 코레일 공식 웹 Chromium headless로 재현해 네트워크/화면 결과를 `.runtime/control/diagnostics`에 기록
