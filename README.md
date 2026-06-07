# 🚄 SRT · KTX 잔여석 조회

SRT·KTX 예약 가능 좌석을 조회하고, 자동 새로고침으로 자리가 나면 브라우저 알림을 띄우는 웹앱입니다.

- **SRT**: 동대구 → 수서
- **KTX**: 동대구 → 수원 / 서울

공식 오픈 API가 없어 회원 로그인 기반 비공식 라이브러리([SRTrain](https://github.com/ryanking13/SRT), [korail2](https://github.com/carpedm20/korail2))를 사용합니다. **예매는 하지 않고 조회·알림만** 합니다.

## 동작 방식

- 입력한 로그인 정보는 **서버 메모리에만** 잠시 보관되며 디스크에 저장되지 않습니다(미사용 3시간 후 만료).
- 프론트엔드(`static/index.html`)에서 날짜·시각을 고르고 `조회`를 누르면 백엔드(`app.py`)가 해당 노선의 열차/좌석 상태를 반환합니다.
- `자동 새로고침`을 켜면 주기적으로 조회하고, 매진→예약가능 전환이 감지되면 알림음 + 브라우저 Notification을 띄웁니다.

## 로컬 실행

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

브라우저에서 http://localhost:8000 접속.

## 배포 메모

이 앱은 백엔드가 필수라 GitHub Pages(정적 호스팅)로는 동작하지 않습니다.
Python 웹앱을 구동할 수 있는 호스트(예: Render, Railway, Fly.io 또는 사내 k8s)가 필요합니다.
시작 명령은 다음과 같습니다.

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

## 면책

개인 학습/편의 목적의 도구입니다. 비공식 API는 운영사 정책 변경으로 예고 없이 동작하지 않을 수 있습니다.
