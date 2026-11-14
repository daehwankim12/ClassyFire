# Windows + Docker Desktop 운영 가이드 (ngrok 공개 배포)

> ClassyFire Batch Web 백엔드 전체 스택을 **Windows 10/11**에서 **Docker
> Desktop(WSL2 백엔드)** 으로 띄우고, **ngrok 공개 HTTPS URL** 로 서비스하는
> 방법을 처음부터 끝까지 설명합니다. (로컬 전용 모드는 다루지 않습니다 —
> 항상 ngrok 도메인으로 접근합니다.)
>
> 명령은 **WSL(Ubuntu) bash** 기준입니다. 별도 표기가 없으면 클론한 저장소
> 루트에서 실행하세요.

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
| `proxy` | `caddy:2-alpine` | 리버스 프록시 + 정적파일 + 보안헤더 | 80, 443 |
| `ngrok-agent` | `ngrok/ngrok:latest` | **공개 HTTPS 터널 (필수)** | 없음(아웃바운드) |

요청 흐름: **브라우저 → `https://<NGROK_DOMAIN>` → ngrok 엣지 → `ngrok-agent`
→ `proxy`(Caddy) → `web`(Gunicorn)**. 공개 접근은 전적으로 ngrok 터널을 통하며,
TLS는 ngrok이 종단합니다.

데이터는 **named volume** 에 저장되어 컨테이너를 지워도 보존됩니다:
`postgres_data`, `media_files`, `static_files`, `export_files`,
`source_artifacts`, `uploads`, `backup_data`, `caddy_data`, `caddy_config`.

---

## 1. 사전 준비물

### 1-1. 하드웨어 권장 사양
- CPU 4코어 이상, RAM 8GB 이상(시드 임포트 시 16GB 권장)
- 디스크 여유 공간
  - 코드 + 이미지: 약 3~5GB
  - 시드 데이터셋: 압축 파일 약 2GB + DB 적재분(압축 해제 시 그 이상)

### 1-2. 소프트웨어
1. **Windows 10 21H2 이상 / Windows 11**, 64-bit
2. **WSL2** 활성화 — 관리자 PowerShell에서:
   ```powershell
   wsl --install
   wsl --set-default-version 2
   ```
   재부팅 후 `wsl --status` 로 기본 버전이 2인지 확인.
3. **Docker Desktop for Windows** (최신 안정 버전)
   - 설치 중 *"Use WSL 2 instead of Hyper-V"* 선택.
   - **Settings → General → Use the WSL 2 based engine** 켜짐 확인.
   - **Settings → Resources → WSL Integration** 에서 사용할 배포판(Ubuntu)에
     토글을 켭니다.
   - WSL 터미널에서 `docker version`, `docker compose version` 동작 확인.
4. **ngrok 계정** — authtoken 발급 + 고정 도메인 1개 예약(아래 3-2).
5. **Git** (WSL 내부 git 권장)

### 1-3. (선택) WSL2 리소스 상향
`C:\Users\<사용자>\.wslconfig`:

```ini
[wsl2]
memory=12GB
processors=4
swap=4GB
```

저장 후 `wsl --shutdown` → Docker Desktop 재시작.

---

## 2. 소스 받기 — ⚠️ 줄바꿈(CRLF) 주의

이 저장소에는 `.gitattributes` 가 없습니다. Windows 기본값
(`core.autocrlf=true`)으로 클론하면 셸 스크립트(`deploy/backup/backup.sh`,
`restore.sh` 등)가 **CRLF로 변환되어 컨테이너 안에서 깨집니다**
(`/backup.sh: not found`, `\r` 오류). 이 스크립트들은 컨테이너에 **bind
mount** 로 직접 들어가므로 호스트 파일의 줄바꿈이 그대로 쓰입니다.

**권장: WSL2 파일시스템 안에서 클론**하면 줄바꿈·권한·성능 모두 자연스럽습니다.

```bash
# WSL(Ubuntu) 터미널에서
cd ~
git clone <레포-URL> ClassyFire
cd ClassyFire
```

이후 모든 `docker compose` 명령도 **이 WSL 터미널 안에서** 실행하세요. 윈도우
탐색기에서는 `\\wsl$\Ubuntu\home\<사용자>\ClassyFire` 로 접근합니다.

> Windows 드라이브(`C:\...`)에 꼭 둬야 한다면, 클론 **전에** `git config
> --global core.autocrlf false && git config --global core.eol lf` 를 설정하세요.
> 검증: `docker compose exec backup sh -c "head -1 /backup.sh | cat -A"` 출력
> 끝에 `^M` 이 보이면 아직 CRLF입니다.

---

## 3. 환경설정 파일 `.env` 만들기

```bash
cp .env.example .env
```

`.env` 를 **UTF-8(BOM 없음)** 에디터로 엽니다.

운영(prod) 설정은 다음 4개가 비어 있으면 **앱이 부팅을 거부**합니다:

| 변수 | 의미 | 비우면 |
|---|---|---|
| `DJANGO_SECRET_KEY` | Django 시크릿 키 | 부팅 실패 |
| `POSTGRES_PASSWORD` | DB 비밀번호 | 부팅 실패 |
| `NGROK_DOMAIN` | 공개 접속 호스트네임(=`ALLOWED_HOSTS`) | 부팅 실패 |
| `SHARED_ACCESS_PASSWORD_HASH` | 공유 접속 비밀번호의 Django 해시 | 부팅 실패 |

> ⚠️ **꼭 확인:** `.env.example` 에는 `REMOTE_DAILY_BUDGET_PER_PRINCIPAL=-1` 이
> 들어 있습니다. 운영 설정은 이 값이 **양수가 아니면 settings 로딩 단계에서
> 즉시 죽습니다**(`migrate` 가 여기서 exit 1). 이 줄을 `=100` 같은 양수로
> 바꾸거나 삭제(삭제 시 기본값 100)하세요. 원격 호출을 꺼도
> (`REMOTE_ENABLED=false`) **예산 값은 여전히 양수**여야 합니다.

### 3-1. 시크릿 생성 (컨테이너 안에서)
Windows에는 Python이 없을 수 있으니 컨테이너로 생성합니다. 먼저 이미지 빌드:

```bash
docker compose build web
```

**`DJANGO_SECRET_KEY`**:

```bash
docker compose run --rm --no-deps -e DJANGO_SETTINGS_MODULE= web \
  python -c "import secrets; print(secrets.token_urlsafe(64))"
```

**`SHARED_ACCESS_PASSWORD_HASH`** — `'내가정할_공유비밀번호'` 만 바꾸세요:

```bash
docker compose run --rm --no-deps -e DJANGO_SETTINGS_MODULE= web \
  python -c "from django.conf import settings; settings.configure(PASSWORD_HASHERS=['django.contrib.auth.hashers.PBKDF2PasswordHasher']); from django.contrib.auth.hashers import make_password; print(make_password('내가정할_공유비밀번호'))"
```

출력된 `pbkdf2_sha256$...` 전체를 `.env` 의 `SHARED_ACCESS_PASSWORD_HASH=` 뒤에
붙여넣습니다. 웹 로그인 시에는 **원본 비밀번호**(`내가정할_공유비밀번호`)를
입력합니다. `POSTGRES_PASSWORD` 는 임의의 강한 문자열로 직접 정하세요.

### 3-2. ngrok 설정
1. [ngrok.com](https://ngrok.com) 가입 → 대시보드에서 **authtoken** 복사.
2. 대시보드에서 **고정 도메인(dev domain)** 예약 (예:
   `<your-domain>.ngrok-free.dev`).
3. `.env` 에 입력:

```dotenv
NGROK_DOMAIN=<your-domain>.ngrok-free.dev
NGROK_AUTHTOKEN=<대시보드 authtoken>
REMOTE_DAILY_BUDGET_PER_PRINCIPAL=100
# 원격 ClassyFire 호출을 켜려면 true, 캐시만 쓰려면 false
REMOTE_ENABLED=true
```

> `NGROK_DOMAIN` 에는 스킴(`https://`)이나 경로 없이 **호스트네임만** 넣습니다.

---

## 4. 스택 기동

ngrok-agent 까지 포함해 **전체**를 띄웁니다:

```bash
docker compose up -d --build
```

- `--build` 는 최초 1회 또는 코드 변경 시 이미지를 빌드합니다(수 분 소요).
- `volume-init` → `migrate` 가 의존성으로 자동 선행 실행됩니다.

상태 확인:

```bash
docker compose ps
docker compose logs migrate --no-color   # Exited (0) 이어야 정상
docker compose logs -f ngrok-agent        # "started tunnel" / 엔드포인트 URL 확인
```

`migrate` 가 `Exited (0)`, `web`/`proxy`/`ngrok-agent` 가 `running` 이면 정상입니다.

---

## 5. 최초 기동 후 확인

### 5-1. 정적 파일(선택)
CSS/JS가 안 보이면 수집:

```bash
docker compose exec web python manage.py collectstatic --noinput
```

### 5-2. 헬스체크
**(A) 공개 경로 — ngrok 터널까지 정상인지** (호스트에서 바로):

```bash
curl -s https://<your-domain>.ngrok-free.dev/health/
```

→ `{"status": "ok"}` 면 브라우저 접속까지 OK.

**(B) 앱만 내부에서 확인** — 컨테이너 내부에서 `localhost:8000` 을 그냥 치면
`ALLOWED_HOSTS` 때문에 **400** 이 납니다(아래 9장 참고). Host 헤더를
`NGROK_DOMAIN` 으로 맞추고 `X-Forwarded-Proto: https` 도 보내야 합니다:

```bash
docker compose exec web python -c "import urllib.request as u; req=u.Request('http://localhost:8000/health/', headers={'Host':'<your-domain>.ngrok-free.dev','X-Forwarded-Proto':'https'}); r=u.urlopen(req); print(r.status, r.read())"
```

→ `200 b'{"status": "ok"}'`.

### 5-3. 접속 & 로그인
브라우저에서 **`https://<NGROK_DOMAIN>/`** 접속 → 첫 화면은 **공유 접속
로그인**. 3-1에서 정한 **원본 공유 비밀번호**를 입력하면 제출 화면으로 들어갑니다.

> 공개 URL에는 **포트를 붙이지 마세요**(`:8000` 등). ngrok 엣지는 443만 받습니다.

> 로그인은 되지만 화합물을 제출해도 캐시가 비어 있으면 대부분 `PENDING`/원격 대기로
> 빠집니다. **결과가 로컬에서 바로 나오게 하려면 이어서 6장(시드 임포트)을 수행하세요.**

### 5-4. (선택) Django 관리자 계정
```bash
docker compose exec web python manage.py createsuperuser
```
`/admin/` 에서 원격 큐·회로차단기 상태 등을 관리할 수 있습니다.

---

## 6. ChemOnt 온톨로지 임포트 (기본 절차)

분류 캐시는 시드 없이 **빈 상태로 시작**해, 워커가 캐시 미스를 원격 분류로 채우면서
쌓입니다(별도 시드 임포트 단계 없음). 다만 분류 라벨을 이름으로 표시하려면 ChemOnt
온톨로지 사전을 한 번 임포트해야 합니다. `chemont_dictionary.tsv` 는 용량 때문에
git·Docker 이미지에 포함되지 않으므로 호스트에 두기만 하면 됩니다(마운트는 이미
`docker-compose.yml` 의 `web` 서비스에 `./seed_cache:ro` 로 들어 있습니다).

### 6-1. ChemOnt 사전 배치
`chemont_dictionary.tsv` 를 리포지토리 루트의 `seed_cache/` 폴더에 둡니다(이 폴더는
`.gitignore` 대상이며, web 컨테이너가 자동으로 봅니다).

### 6-2. 임포트
```bash
# ChemOnt 온톨로지 임포트 — 분류 라벨 이름 표시에 필요
docker compose run --rm web python manage.py import_chemont \
  /app/seed_cache/chemont_dictionary.tsv --replace
```

- 경로는 **컨테이너 내부 경로**(`/app/seed_cache/...`)입니다. 호스트 경로가 아닙니다.
- `import_chemont` 는 위치 인자 `tsv_path` 가 **필수**입니다. 인자 없이 실행하면
  `the following arguments are required` 오류가 납니다.
- 분류 캐시(`classyfire_live`)는 워커(`runworker`)가 캐시 미스를 원격 분류로 채우며
  자동으로 쌓입니다 — 별도 임포트·활성화 단계가 없습니다.

---

## 7. 운영 명령 모음

```bash
# 로그
docker compose logs -f web worker ingest-worker ngrok-agent

# 상태
docker compose ps

# 마이그레이션(코드 업데이트 후)
docker compose run --rm migrate

# 백업 1회 수동 실행 / 무결성 스모크
docker compose exec web python manage.py run_backup
docker compose exec web python manage.py restore_smoke_test

# 만료 데이터 정리 1회
docker compose exec web python manage.py cleanup_expired

# 원격 일시정지/재개
docker compose exec web python manage.py admin_pause_remote
docker compose exec web python manage.py admin_resume_remote
```

`cleanup_expired`는 완료/실패한 웹 작업 페이지와 연결된 업로드 artifact를
기본 7일 후 정리하고, 만료된 export 파일도 기본 7일 후 정리합니다. InChIKey
분류 캐시(`classyfire_live`, `classification_history`)는
삭제하지 않습니다.

### 7-1. 코드 업데이트
```bash
git pull
docker compose build web worker ingest-worker
docker compose run --rm migrate
docker compose up -d
```

### 7-2. 전체 복원(데이터 손실 작업)
복원은 `deploy/backup/restore.sh` 가 수행합니다. 워커를 멈추고 진행하며, 복원
전 반드시 현재 상태를 한 번 더 백업하세요. 절차는
[docs/operations.md](operations.md) 참고(명령 형태는 Windows에서도 동일).

---

## 8. 정지 / 삭제

```bash
docker compose stop        # 컨테이너만 정지(데이터 보존)
docker compose down        # 컨테이너 제거(named volume=데이터는 보존)
docker compose down -v     # ⚠️ 데이터까지 전부 삭제(DB/업로드/내보내기/백업)
```

`down -v` 는 **모든 named volume을 삭제**합니다. 정말 초기화할 때만 사용하세요.

---

## 9. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 부팅 직후 `ImproperlyConfigured: A positive production remote budget...` (migrate exit 1) | `.env` 의 `REMOTE_DAILY_BUDGET_PER_PRINCIPAL` 이 `-1`. **양수로 변경**(3장 경고). |
| 부팅 직후 `DJANGO_SECRET_KEY/POSTGRES_PASSWORD/NGROK_DOMAIN/SHARED_ACCESS_PASSWORD_HASH` 관련 에러 | 4대 필수 변수 중 빈 값. **3장**. |
| 공개 URL `https://<도메인>/...` → **404 (endpoint offline)** | `ngrok-agent` 가 안 떠 있음. `docker compose up -d ngrok-agent` 후 `docker compose logs -f ngrok-agent` 로 터널 연결 확인. |
| 공개 URL 접속이 **멈춤(행)** | URL에 포트(`:8000` 등)를 붙임. ngrok 엣지는 443만 받음 → `https://<도메인>/` 로만 접속. |
| 컨테이너 내부 `http://localhost:8000/...` 호출이 **400 (DisallowedHost)** | `ALLOWED_HOSTS=[NGROK_DOMAIN, www.…]` 라 Host=`localhost` 거부. 앱 정상. 내부 점검은 **5-2 (B)** 의 Host 헤더 방식으로. |
| `ngrok-agent` 가 계속 재시작 + `ERR_NGROK_105` (`Your authtoken: ${NGROK_AUTHTOKEN}`) | `.env` 의 `NGROK_AUTHTOKEN` 이 비었거나 잘못됨 → **대시보드 authtoken을 그대로** 붙여넣으세요. ngrok는 authtoken을 환경변수로 읽고, 예약 도메인은 compose의 `--url=https://${NGROK_DOMAIN}` 로 전달됩니다(config 파일 미사용). |
| `proxy` 가 80/443 바인딩 실패 | 호스트의 IIS·"World Wide Web Publishing Service"·일부 VPN 등이 점유. **9-1** 로 회피(ngrok 전용이면 호스트 포트는 사실상 불필요). |
| `backup`/`restore` 가 `\r`·`not found` 로 실패 | `.sh` 가 CRLF. **2장**(WSL2에 클론 또는 `autocrlf=false`). |
| CSS 깨짐 | **5-1** `collectstatic`. |
| 빌드/마운트가 매우 느림 | 저장소가 `/mnt/c/...`(Windows 드라이브)에 있음. **2장**처럼 WSL2 파일시스템에 두기. |

### 9-1. 포트 80/443 충돌 회피
공개 접근은 ngrok 터널(프록시와 공유한 네트워크의 `127.0.0.1:80`)을
통하므로 **호스트 포트
80/443 publish는 사실상 필요 없습니다.** 충돌이 나면 저장소 루트에
`docker-compose.override.yml` 을 만들어 호스트 포트만 바꾸세요(원본 compose는
그대로):

```yaml
services:
  proxy:
    ports:
      - "8080:80"
      - "8443:443"
```

override 파일은 `docker compose up` 시 자동 병합됩니다. (공개 URL은 여전히
`https://<NGROK_DOMAIN>/` 이며 호스트 포트와 무관합니다.)

---

## 10. 참고 문서
- [README.md](../README.md) — 리눅스 Quick Start, 시드/백업/업데이트 개요
- [docs/operations.md](operations.md) — 워커 관리·백업/복원·시드 교체 런북
- [docs/user-guide.md](user-guide.md) — 화합물 제출·결과 해석·내보내기
- [docs/production-safety-gate.md](production-safety-gate.md) — 운영 배포 전 안전 게이트
- `.env.example` — 전체 환경변수 목록과 기본값
