# Windows + Docker Desktop 운영 가이드

> ClassyFire Batch Web 백엔드 전체 스택을 **Windows 10/11**에서 **Docker
> Desktop(WSL2 백엔드)** 으로 띄우는 방법을 처음부터 끝까지 설명합니다.
> 리눅스용 `README.md` Quick Start 와 같은 내용을 다루되, Windows 특유의
> 함정(줄바꿈/CRLF, 포트 충돌, 시크릿 쿠키, 파일 공유)까지 모두 포함합니다.
>
> 명령은 모두 **PowerShell** 기준입니다. 별도 표기가 없으면 클론한 저장소
> 루트 디렉터리에서 실행하세요.

---

## 0. 이 스택이 띄우는 것

`docker compose up` 한 번으로 다음 컨테이너가 함께 뜹니다.

| 서비스 | 이미지/빌드 | 역할 | 호스트 포트 |
|---|---|---|---|
| `db` | `postgres:16-alpine` | PostgreSQL 16 (내부망 전용) | 없음 |
| `volume-init` | 로컬 빌드 | 볼륨 디렉터리 생성·권한 설정(1회성) | 없음 |
| `migrate` | 로컬 빌드 | `manage.py migrate` (1회성) | 없음 |
| `web` | 로컬 빌드 | Gunicorn (Django) — 프록시 뒤에서만 접근 | 없음(내부 8000) |
| `worker` | 로컬 빌드 | 원격 분류 워커(`runworker`) | 없음 |
| `ingest-worker` | 로컬 빌드 | 업로드 처리 워커(`process_jobs`) | 없음 |
| `cleanup` | 로컬 빌드 | 만료 데이터 정리(24h 루프) | 없음 |
| `backup` | `postgres:16-alpine` | DB·아티팩트 백업(24h 루프) | 없음 |
| `proxy` | `caddy:2-alpine` | 리버스 프록시 + 정적파일 + 보안헤더 | **80, 443** |
| `ngrok-agent` | `ngrok/ngrok:latest` | 공개 HTTPS 터널(선택) | 없음(아웃바운드) |

> 외부에 노출되는 포트는 `proxy`(80/443)뿐입니다. `db`/`web`/워커들은 호스트
> 포트를 열지 않고 Docker 내부망으로만 통신합니다.

데이터는 **named volume** 에 저장되어 컨테이너를 지워도 보존됩니다:
`postgres_data`, `media_files`, `static_files`, `export_files`,
`source_artifacts`, `uploads`, `backup_data`, `caddy_data`, `caddy_config`.

---

## 1. 사전 준비물

### 1-1. 하드웨어 권장 사양
- CPU 4코어 이상, RAM 8GB 이상(시드 임포트 시 16GB 권장)
- 디스크 여유 공간
  - 코드 + 이미지: 약 3~5GB
  - 시드 데이터셋 `FULL` 임포트 시: **추가 약 4GB 다운로드 + DB 적재분**
  - 시드 없이 캐시만 쓰면 디스크 부담은 작습니다.

### 1-2. 소프트웨어
1. **Windows 10 21H2 이상 / Windows 11**, 64-bit
2. **WSL2** 활성화
   - 관리자 PowerShell에서:
     ```powershell
     wsl --install
     wsl --set-default-version 2
     ```
   - 재부팅 후 `wsl --status` 로 기본 버전이 2인지 확인.
3. **Docker Desktop for Windows** (최신 안정 버전)
   - 설치 중 *"Use WSL 2 instead of Hyper-V"* 를 선택.
   - 설치 후 **Settings → General → Use the WSL 2 based engine** 가 켜져 있는지 확인.
   - **Settings → Resources → WSL Integration** 에서 사용할 배포판(예:
     Ubuntu)에 토글을 켭니다(WSL 내부에서 클론할 경우 필요).
   - `docker version`, `docker compose version` 이 PowerShell에서 동작하는지 확인.
4. **Git** (Git for Windows 또는 WSL 내부 git)

### 1-3. (선택) WSL2 리소스 상향
시드 임포트처럼 메모리를 많이 쓰는 작업을 한다면
`C:\Users\<사용자>\.wslconfig` 를 만들어 둡니다:

```ini
[wsl2]
memory=12GB
processors=4
swap=4GB
```

저장 후 `wsl --shutdown` → Docker Desktop 재시작.

---

## 2. 소스 받기 — ⚠️ 줄바꿈(CRLF) 주의

이 저장소에는 `.gitattributes` 가 없습니다. 따라서 Windows에서 기본값
(`core.autocrlf=true`)으로 클론하면 셸 스크립트(`deploy/backup/backup.sh`,
`restore.sh` 등)가 **CRLF로 변환되어 컨테이너 안에서 깨집니다**
(`/backup.sh: cannot execute: required file not found`, `\r` 오류 등).
이 스크립트들은 컨테이너에 **bind mount** 로 직접 들어가기 때문에, 이미지가
아니라 호스트 파일의 줄바꿈이 그대로 사용됩니다.

**권장 방법 — 둘 중 하나를 택하세요.**

### 방법 A (가장 안전): WSL2 파일시스템 안에서 클론
WSL 배포판(Ubuntu) 안에서 작업하면 줄바꿈·권한·성능 모두 자연스럽습니다.

```bash
# WSL(Ubuntu) 터미널에서
cd ~
git clone <레포-URL> ClassyFire
cd ClassyFire
```

이후 `docker compose` 명령도 **WSL 터미널 안에서** 실행하세요. (Docker Desktop
의 WSL Integration이 켜져 있으면 그대로 됩니다.) 윈도우 탐색기에서는
`\\wsl$\Ubuntu\home\<사용자>\ClassyFire` 로 접근할 수 있습니다.

### 방법 B: Windows에 클론하되 LF 강제
PowerShell에서 클론 **전에** autocrlf를 끄세요:

```powershell
git config --global core.autocrlf false
git config --global core.eol lf
git clone <레포-URL> ClassyFire
cd ClassyFire
```

이미 잘못 클론했다면 정규화로 복구:

```powershell
git config core.autocrlf false
git rm --cached -r .
git reset --hard
```

> 검증: 컨테이너 기동 후 `docker compose exec backup sh -c "head -1 /backup.sh | cat -A"`
> 출력 끝에 `^M` 이 보이면 아직 CRLF입니다.

---

## 3. 환경설정 파일 `.env` 만들기

```powershell
Copy-Item .env.example .env
```

`.env` 를 **VS Code 등 UTF-8(BOM 없음) 에디터**로 엽니다. (메모장은 BOM을
넣을 수 있으니 피하세요.)

운영(prod) 설정은 다음 4개가 비어 있으면 **앱이 부팅을 거부**합니다:

| 변수 | 의미 | 비우면 |
|---|---|---|
| `DJANGO_SECRET_KEY` | Django 시크릿 키 | 부팅 실패 |
| `POSTGRES_PASSWORD` | DB 비밀번호 | 부팅 실패 |
| `NGROK_DOMAIN` | 접속에 사용할 호스트네임(=`ALLOWED_HOSTS`) | 부팅 실패 |
| `SHARED_ACCESS_PASSWORD_HASH` | 공유 접속 비밀번호의 Django 해시 | 부팅 실패 |

> ⚠️ **꼭 확인:** `.env.example` 에는 `REMOTE_DAILY_BUDGET_PER_PRINCIPAL=-1` 이
> 들어 있습니다. 운영 설정은 이 값이 **양수가 아니면 기동 시 에러**
> (`A positive production remote budget is required`)를 냅니다. 이 줄을 지우거나
> (지우면 기본값 100 적용) `=100` 같은 양수로 바꾸세요. 원격 호출을 끄려면
> `REMOTE_ENABLED=false` 로 하되, **예산 값은 여전히 양수**여야 합니다.

### 3-1. 시크릿 생성 (컨테이너 안에서)
Windows에는 Python이 없을 수 있으니 컨테이너로 생성합니다. 먼저 이미지를
한 번 빌드해 둡니다(없으면 아래 명령이 자동 빌드):

```powershell
docker compose build web
```

**`DJANGO_SECRET_KEY`** 생성:

```powershell
docker compose run --rm --no-deps -e DJANGO_SETTINGS_MODULE= web `
  python -c "import secrets; print(secrets.token_urlsafe(64))"
```

**`SHARED_ACCESS_PASSWORD_HASH`** 생성 — `'내가정할_공유비밀번호'` 부분만 바꾸세요:

```powershell
docker compose run --rm --no-deps -e DJANGO_SETTINGS_MODULE= web `
  python -c "from django.conf import settings; settings.configure(PASSWORD_HASHERS=['django.contrib.auth.hashers.PBKDF2PasswordHasher']); from django.contrib.auth.hashers import make_password; print(make_password('내가정할_공유비밀번호'))"
```

출력된 `pbkdf2_sha256$...` 전체 문자열을 `.env` 의
`SHARED_ACCESS_PASSWORD_HASH=` 뒤에 그대로 붙여넣습니다. (`$` 가 포함되어 있어도
`.env` 파일에서는 그대로 둬도 됩니다.) 나중에 웹에서 로그인할 때는 **원본
비밀번호**(`내가정할_공유비밀번호`)를 입력합니다.

`POSTGRES_PASSWORD` 는 임의의 강한 문자열로 직접 정합니다.

### 3-2. 접속 방식 결정 — `NGROK_DOMAIN`

**시나리오 ① 로컬 전용(내 PC에서만 접속):**

```dotenv
NGROK_DOMAIN=localhost
NGROK_AUTHTOKEN=
REMOTE_ENABLED=false
REMOTE_DAILY_BUDGET_PER_PRINCIPAL=100
```

- 브라우저로 **`http://localhost/`** 에 접속합니다.
- `proxy`(Caddy)가 `X-Forwarded-Proto: https` 를 붙여주므로 Django의 HTTPS
  강제 리다이렉트 루프가 생기지 않습니다(평문 HTTP로도 정상 동작).
- `ALLOWED_HOSTS` 는 `localhost`, `www.localhost` 만 허용합니다. 따라서
  **`http://127.0.0.1/` 로 접속하면 400(DisallowedHost)** 이 납니다. 반드시
  `localhost` 로 접속하세요. (127.0.0.1 을 쓰고 싶다면 `NGROK_DOMAIN=127.0.0.1`
  로 설정.)
- 쿠키는 `Secure` 속성이라 HTTPS에서만 전송되지만, 최신
  Chrome/Edge/Firefox 는 `localhost` 를 "보안 컨텍스트"로 취급해 평문
  `http://localhost` 에서도 로그인 쿠키가 정상 동작합니다.

**시나리오 ② 공개 HTTPS(ngrok):**

```dotenv
NGROK_DOMAIN=your-name.ngrok-free.app
NGROK_AUTHTOKEN=<ngrok 대시보드의 authtoken>
REMOTE_ENABLED=true
REMOTE_DAILY_BUDGET_PER_PRINCIPAL=100
```

- [ngrok.com](https://ngrok.com) 가입 후 authtoken 발급, 대시보드에서 고정
  도메인(dev domain)을 예약해 `NGROK_DOMAIN` 에 넣습니다.
- 접속은 `https://<NGROK_DOMAIN>/`.

---

## 4. 스택 기동

### 4-1. 시나리오 ① 로컬 전용 — ngrok 제외하고 기동
`ngrok-agent` 는 토큰이 없으면 **무한 재시작(crash-loop)** 하므로, 로컬 전용
에서는 띄울 서비스를 명시적으로 지정합니다:

```powershell
docker compose up -d --build db migrate web worker ingest-worker proxy cleanup backup
```

- `--build` 는 최초 1회 또는 코드 변경 시 이미지를 빌드합니다(수 분 소요).
- `volume-init` 과 `migrate` 는 의존성으로 자동 선행 실행됩니다.

### 4-2. 시나리오 ② 공개(ngrok) — 전체 기동

```powershell
docker compose up -d --build
```

### 4-3. 상태 확인

```powershell
docker compose ps
docker compose logs -f web
```

`migrate` 가 `Exited (0)` 이고 `web`/`proxy` 가 `running`/`healthy` 면 정상입니다.

---

## 5. 최초 기동 후 확인

### 5-1. 정적 파일(선택, 권장)
정적 파일은 보통 볼륨 초기화 시 자동 채워지지만, CSS/JS가 안 보이면 수동으로
수집합니다:

```powershell
docker compose exec web python manage.py collectstatic --noinput
```

### 5-2. 헬스체크 & 접속
```powershell
# 컨테이너 내부 헬스 엔드포인트
docker compose exec web python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/health/').read())"
```
브라우저에서:
- 로컬 전용: `http://localhost/`
- ngrok: `https://<NGROK_DOMAIN>/`

첫 화면은 **공유 접속(shared-access) 로그인** 페이지입니다. 3-1에서 정한
**원본 공유 비밀번호**를 입력하면 제출 화면으로 들어갑니다.

### 5-3. (선택) Django 관리자 계정
```powershell
docker compose exec web python manage.py createsuperuser
```
이후 `/admin/` 에서 원격 큐, 회로차단기(circuit breaker) 상태 등을 관리할 수
있습니다.

---

## 6. 시드 데이터셋 임포트 (선택)

로컬 분류 캐시를 미리 채우면 원격 호출이 크게 줄어듭니다. Windows에서는
`make` 대신 컨테이너 안에서 관리 명령을 직접 실행합니다. (`FULL` 은 ~4GB
다운로드이므로 네트워크/디스크 여유를 확인하세요.)

```powershell
docker compose run --rm web python manage.py seed_download
docker compose run --rm web python manage.py seed_verify
docker compose run --rm web python manage.py seed_import --resume
docker compose run --rm web python manage.py seed_audit
docker compose run --rm web python manage.py seed_activate
```

- `seed_import --resume` 은 중단 후 재실행해도 중복 없이 이어집니다.
- 프로필은 `.env` 의 `SEED_PROFILE`(`FULL`|`LITE`|`CUSTOM`)로 정합니다.
  처음엔 가벼운 `LITE` 로 시작하는 것을 권장합니다.

---

## 7. 운영 명령 모음

모두 PowerShell, 저장소 루트에서:

```powershell
# 로그
docker compose logs -f web worker ingest-worker

# 상태/헬스
docker compose ps

# 마이그레이션(코드 업데이트 후)
docker compose run --rm migrate

# 백업 1회 수동 실행
docker compose exec web python manage.py run_backup

# 백업 무결성 스모크 테스트
docker compose exec web python manage.py restore_smoke_test

# 만료 데이터 정리 1회
docker compose exec web python manage.py cleanup_expired

# 원격 일시정지/재개(인증 차단 시 등)
docker compose exec web python manage.py admin_pause_remote
docker compose exec web python manage.py admin_resume_remote
```

### 7-1. 전체 복원(데이터 손실 작업)
복원은 `deploy/backup/restore.sh` 가 수행합니다. 워커를 멈추고 진행하세요.
자세한 절차는 [docs/operations.md](operations.md) 를 따르되, Windows에서도 명령
형태는 동일합니다(컨테이너 내부 실행). 복원 전 반드시 현재 상태를 한 번 더
백업하세요.

### 7-2. 코드 업데이트
```powershell
git pull
docker compose build web worker ingest-worker
docker compose run --rm migrate
docker compose up -d web worker ingest-worker proxy
```

---

## 8. 정지 / 삭제

```powershell
# 컨테이너만 정지(데이터 보존)
docker compose stop

# 컨테이너 제거(named volume = 데이터는 보존)
docker compose down

# ⚠️ 데이터까지 전부 삭제(DB/업로드/내보내기/백업 모두 사라짐)
docker compose down -v
```

`down -v` 는 **모든 named volume을 삭제**합니다. DB·업로드·백업이 전부
지워지므로 정말 초기화할 때만 사용하세요.

---

## 9. Windows 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `proxy` 가 80/443 바인딩 실패 | Windows의 IIS·"World Wide Web Publishing Service", 일부 VPN, 예전 Skype 등이 80/443 점유. 해당 서비스 중지하거나 **9-1** 처럼 호스트 포트를 바꾸세요. |
| `backup`/`restore` 컨테이너가 `\r` 또는 `not found`로 실패 | `.sh` 가 CRLF로 변환됨. **2장(방법 A/B)** 으로 LF 정규화. |
| `ngrok-agent` 가 계속 재시작 | `NGROK_AUTHTOKEN` 미설정. 로컬 전용이면 **4-1**처럼 ngrok을 빼고 기동. |
| 부팅 직후 `ImproperlyConfigured: A positive production remote budget...` | `.env` 의 `REMOTE_DAILY_BUDGET_PER_PRINCIPAL` 이 `-1`. 양수로 변경(3장 경고 참고). |
| 부팅 직후 `... DJANGO_SECRET_KEY/POSTGRES_PASSWORD/NGROK_DOMAIN/SHARED_ACCESS_PASSWORD_HASH` 관련 에러 | 4대 필수 변수 중 빈 값이 있음. **3장** 참고. |
| 브라우저 400 `DisallowedHost` | 접속 호스트가 `NGROK_DOMAIN` 과 불일치. 로컬은 `http://localhost/` 로(=`127.0.0.1` 아님). |
| 로그인 후 폼이 CSRF/세션 오류 | `127.0.0.1`·구형 브라우저로 평문 접속해 `Secure` 쿠키가 막힘. `localhost` 로 접속하거나 ngrok(HTTPS) 사용. |
| CSS 깨짐 | **5-1** `collectstatic` 실행. |
| 빌드/마운트가 매우 느림 | 저장소가 Windows 드라이브(`/mnt/c/...`)에 있음. **2장 방법 A**처럼 WSL2 파일시스템 안에 두면 빠릅니다. |
| `docker compose` 가 PowerShell에서 안 됨 | Docker Desktop 미실행 또는 WSL Integration 꺼짐. Docker Desktop 시작 후 Settings 확인. |

### 9-1. 80/443 포트를 바꾸기 (포트 충돌 시)
저장소 루트에 `docker-compose.override.yml` 을 만들면 원본 compose를 건드리지
않고 호스트 포트만 바꿀 수 있습니다:

```yaml
services:
  proxy:
    ports:
      - "8080:80"
      - "8443:443"
```

이후 로컬 접속 주소는 `http://localhost:8080/` 입니다. (`NGROK_DOMAIN=localhost`
면 Django는 포트를 무시하고 호스트네임만 비교하므로 그대로 동작합니다.)
override 파일은 `docker compose up` 시 자동으로 합쳐집니다.

---

## 10. 참고 문서
- [README.md](../README.md) — 리눅스 Quick Start, 시드/백업/업데이트 개요
- [docs/operations.md](operations.md) — 워커 관리·백업/복원·시드 교체 런북
- [docs/user-guide.md](user-guide.md) — 화합물 제출·결과 해석·내보내기
- [docs/production-safety-gate.md](production-safety-gate.md) — 운영 배포 전 안전 게이트
- `.env.example` — 전체 환경변수 목록과 기본값
