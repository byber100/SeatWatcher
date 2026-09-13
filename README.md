# SeatWatcher

철도와 버스의 취소표·잔여석을 반복 조회하고, 실제로 이용 가능한 좌석이 생기거나 같은 편의 예매 가능한 잔여석·좌석 형태가 바뀌면 알림하는 개인용 모니터링 프로젝트입니다.

> SeatWatcher는 **조회와 알림**에 집중합니다. 자동 예매나 결제는 수행하지 않습니다.

## 주요 기능

- **KORAIL 철도 조회**
  - 직통 열차와 자동 환승을 각각 조회
  - 출발·도착 시각, 열차, 좌석 상태를 상세 표시
  - 직통 예약대기 상태 감지
  - 과도한 우회 환승 필터링
- **버스 조회**
  - KOBUS
  - 티머니 시외버스
  - 버스타고
- **좌석 상태 변화 감지**
  - 같은 편의 상태가 그대로면 polling마다 반복 알림하지 않음
  - 예매 가능한 상태에서 잔여석 수나 지정좌석·자유석·입석 등 이용 형태가 바뀌면 다시 알림
  - 매진으로 바뀐 사실은 알리지 않고, 매진 뒤 다시 예매 가능해지면 다시 알림
- **환승 후보 감시**
  - 직통 좌석 유무와 관계없이 환승 시간표를 계속 조회
  - 필요할 때 이용 가능한 환승을 알림 후보로 승격
- **Kakao 알림 연동**
  - 같은 polling cycle의 여러 변동을 한 메시지로 묶어 전송
  - 메시지에는 핵심 1~2건만 간단히 표시하고 `예매 확인` 버튼에서 전체 상세 확인
  - 상세 화면에서 코레일·고속버스·시외버스·버스타고 공식 예매 사이트로 이동
- **상세 터미널 출력**
  - 실제 조회 과정과 운행 후보를 디버깅하기 쉽게 자세히 표시

## 동작 방식

1. 로컬에서는 `watch_targets.local.json`에 실제 감시할 교통편과 시간대를 설정합니다.
2. 해당 파일이 없으면 공개 기본 설정인 `watch_targets.json`을 사용합니다.
3. SeatWatcher가 각 공급자의 운행 정보와 현재 좌석 상태를 반복 조회합니다.
4. 이용 가능한 새 후보와 같은 편의 잔여석·예매 가능 형태 변동을 판정합니다.
5. 매진 전환은 알리지 않고, 현재 예매 가능한 상태가 새로 생기거나 변했을 때 설정한 경우 Kakao로 알립니다.
6. 같은 cycle의 변동은 Kakao 메시지 하나로 묶고, `예매 확인` 버튼은 GitHub Pages의 상세 화면으로 연결합니다.
7. 이후에도 상태를 계속 저장해 같은 내용의 불필요한 반복 알림을 막고 재오픈을 감지합니다.

## PC 없이 자동 감지

최종 운영은 개인 PC를 켜 두는 방식이 아니라 **클라우드 Linux VM에서 SeatWatcher를 백그라운드 서비스로 실행**하는 구조입니다. VM은 웹서버로 공개하지 않고 KORAIL/버스 조회와 Kakao 발송만 수행합니다. 상세 웹 화면은 GitHub Pages가 담당합니다.

권장 무료 운영 기준은 **Oracle Cloud Infrastructure Always Free Compute**입니다. Ubuntu 24.04 이상에서 `VM.Standard.A1.Flex` 1 OCPU / 1 GB 또는 계정에 표시되는 Always Free x86 micro를 사용하고 저장소를 clone한 뒤 비공개 운영 파일을 직접 복사합니다. Oracle은 유휴 Always Free VM을 회수할 수 있으므로 절대적 SLA로 보지는 않습니다.

```text
.env.local
watch_targets.local.json
.runtime/kakao_tokens.json
# 기존 로컬 상태를 그대로 이어갈 때만 선택적으로 복사
.runtime/watch_state.json
```

그 다음 VM의 저장소 루트에서 실행합니다.

```bash
python3 deploy/cloud_vm_manage.py install
```

상태/로그 확인은 다음 명령을 사용합니다.

```bash
python3 deploy/cloud_vm_manage.py status
python3 deploy/cloud_vm_manage.py logs
```

서비스는 부팅 시 자동 시작되고 비정상 종료 시 자동 재시작됩니다. 애플리케이션용 인바운드 포트나 별도 웹서버는 필요하지 않습니다.

GitHub Pages는 저장소 `Settings > Pages`에서 `Deploy from a branch`, `main`, `/docs`를 한 번 지정하면 됩니다. 알림 상세 화면 주소는 기본적으로 `https://byber100.github.io/SeatWatcher/`를 사용합니다. Kakao Developers의 제품 링크 Web 도메인에도 `https://byber100.github.io`를 추가해야 `예매 확인` 버튼이 정상 이동합니다.

## 빠른 시작

### 1. 의존성 설치

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

### 2. 로컬 환경설정 준비

저장소의 `.env`는 **값이 비어 있는 템플릿**입니다. 실제 인증정보를 여기에 넣지 마세요.

```text
.env        -> Git에 추적되는 빈 템플릿
.env.local  -> 실제 로컬 인증정보, Git 추적 제외
```

`.env` 또는 `env.example.txt`를 참고해 `.env.local`을 만들고 필요한 값을 채웁니다.

환경변수 로딩 우선순위는 다음과 같습니다.

```text
OS 환경변수 > .env.local > .env
```

### 3. 감시 대상 설정

공개 저장소의 `watch_targets.json`은 개인 일정이 없는 기본 설정입니다. 실제 운영용 감시 조건은 같은 형식의 `watch_targets.local.json`에 저장하세요.

```text
watch_targets.json        -> Git 추적 공개 기본값
watch_targets.local.json  -> 실제 개인 감시 설정, Git 추적 제외
```

`watch_targets.local.json`이 존재하면 자동으로 우선 사용합니다.

### 4. 실행

```bash
.venv\Scripts\python watcher.py --watch
```

Kakao 알림까지 사용하려면:

```bash
.venv\Scripts\python watcher.py --watch --notify
```

실제 좌석 변동을 기다리지 않고 묶음 메시지와 `예매 확인` 링크를 테스트하려면:

```bash
.venv\Scripts\python watcher.py --test-alert
```

이 명령은 가짜 기차 1건 + 버스 1건을 Kakao로 한 번 보내고, 버튼 링크를 GitHub Pages 상세 화면으로 생성합니다. 실제 감시 상태 파일은 변경하지 않습니다.

단일 조회 확인에는 `seatwatcher.py`를 사용할 수 있습니다.

## 개발 Git 흐름

`main`은 GitHub의 `origin/main`을 추적합니다. 일반적인 변경은 다음 순서를 사용합니다.

```text
git pull --ff-only
→ 로컬 수정
→ Python/JSON/보안 검증
→ 로컬 git commit
→ 사용자가 원격 반영을 지시한 경우에만 git push origin main
```

실제 인증정보, 개인 이동 일정, 런타임 상태는 이 흐름에 포함하지 않습니다.

## 프로젝트 구조

```text
watcher.py                 반복 감시 실행기 / 상태 관리
alert_bundle.py            cycle 단위 묶음 알림 / Pages 링크 생성
seatwatcher.py             단일 조회 CLI
rail_provider.py           KORAIL 직통·환승 조회
bus_providers.py           KOBUS·티머니·버스타고 조회
last_mile.py               최종 이동 가능 여부 판정
kakao_notify.py            Kakao 알림 연동
env_loader.py              로컬 환경변수 로딩
watch_targets.json         공개 기본 감시 설정
watch_targets.local.json   실제 개인 감시 설정, Git 제외
docs/                      개발 협업 문서 + GitHub Pages 상세 화면(index.md)
deploy/cloud_vm_manage.py  Linux VM 설치 / systemd 서비스 관리
```

`docs/`는 프로젝트를 외부에 소개하기 위한 문서가 아니라, 개발 과정에서 요구사항과 구현 판단을 맞추기 위한 작업 공간입니다. 프로젝트 소개와 사용 안내는 이 README를 기준으로 합니다.

## 현재 상태

아직 개발 중인 프로젝트입니다. 공급자별 웹/API 구조 변경, 접속 제한, 예매 정책 변경 등에 따라 일부 조회 기능이 일시적으로 동작하지 않을 수 있습니다.

SeatWatcher는 조회 빈도를 무작정 높이기보다 오류·차단·NetFunnel 등의 신호를 확인하면서 보수적으로 조정하는 방향을 사용합니다.

## 주의사항

- 좌석 상태는 조회 시점의 정보이며 실제 예매 성공을 보장하지 않습니다.
- 예매 최종 동작은 사용자가 직접 수행합니다.
- 실제 계정 정보, 토큰, 개인 이동 일정은 Git에 커밋하지 마세요.
- 각 서비스의 이용약관과 정상적인 사용 범위를 지켜 사용해야 합니다.
- 이 프로젝트는 KORAIL, KOBUS, 티머니, 버스타고, Kakao와 공식적으로 제휴하거나 보증받은 서비스가 아닙니다.
