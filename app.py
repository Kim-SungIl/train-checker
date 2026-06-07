"""SRT / KTX 잔여석 조회 + 알림 웹앱.

- 로그인 정보는 메모리에만 보관(토큰 키), 디스크 저장 안 함.
- SRT: 동대구 -> 수서
- KTX: 동대구 -> 수원 / 서울
공식 API가 없어 비공식 라이브러리(SRTrain, korail2)를 사용한다.
"""
import secrets
import time
import threading
from contextlib import suppress

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from SRT import SRT
from korail2 import Korail, TrainType, NoResultsError

app = FastAPI(title="SRT/KTX 잔여석 조회")

# ── 세션 저장소 (메모리) ─────────────────────────────────────────────
# token -> {"client": <SRT|Korail>, "kind": "srt"|"korail", "ts": last_used}
SESSIONS: dict[str, dict] = {}
SESSION_TTL = 60 * 60 * 3  # 3시간 미사용 시 만료
_lock = threading.Lock()

# 고정 경로
SRT_DEP, SRT_ARR = "동대구", "수서"
KTX_DEP = "동대구"
KTX_ALLOWED_ARR = {"수원", "서울"}


def _gc_sessions():
    now = time.time()
    with _lock:
        for tok in [t for t, s in SESSIONS.items() if now - s["ts"] > SESSION_TTL]:
            SESSIONS.pop(tok, None)


def _get_session(token: str, kind: str):
    _gc_sessions()
    s = SESSIONS.get(token)
    if not s or s["kind"] != kind:
        raise HTTPException(status_code=401, detail="로그인이 만료되었습니다. 다시 로그인해 주세요.")
    s["ts"] = time.time()
    return s["client"]


# ── 요청 모델 ────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    id: str
    pw: str


class SearchReq(BaseModel):
    token: str
    date: str   # YYYYMMDD
    time: str   # HHMMSS
    arr: str | None = None  # KTX 전용 (수원/서울)


# ── 로그인 ───────────────────────────────────────────────────────────
@app.post("/api/login/srt")
def login_srt(req: LoginReq):
    try:
        client = SRT(req.id.strip(), req.pw)
    except Exception:
        raise HTTPException(status_code=400, detail="SRT 로그인에 실패했습니다. 아이디/비밀번호를 확인해 주세요.")
    token = secrets.token_urlsafe(24)
    with _lock:
        SESSIONS[token] = {"client": client, "kind": "srt", "ts": time.time()}
    return {"token": token}


@app.post("/api/login/korail")
def login_korail(req: LoginReq):
    try:
        client = Korail(req.id.strip(), req.pw)
        if not getattr(client, "logined", True):
            raise RuntimeError("login failed")
    except Exception:
        raise HTTPException(status_code=400, detail="코레일 로그인에 실패했습니다. 아이디/비밀번호를 확인해 주세요.")
    token = secrets.token_urlsafe(24)
    with _lock:
        SESSIONS[token] = {"client": client, "kind": "korail", "ts": time.time()}
    return {"token": token}


@app.post("/api/logout")
def logout(body: dict):
    with _lock:
        SESSIONS.pop(body.get("token", ""), None)
    return {"ok": True}


# ── 조회 ─────────────────────────────────────────────────────────────
@app.post("/api/search/srt")
def search_srt(req: SearchReq):
    client: SRT = _get_session(req.token, "srt")
    try:
        trains = client.search_train(
            SRT_DEP, SRT_ARR, date=req.date, time=req.time, available_only=False
        )
    except Exception as e:
        # 세션 만료 등 재로그인 시도 1회
        with suppress(Exception):
            client.login()
            trains = client.search_train(
                SRT_DEP, SRT_ARR, date=req.date, time=req.time, available_only=False
            )
            return {"dep": SRT_DEP, "arr": SRT_ARR, "trains": [_srt_row(t) for t in trains]}
        raise HTTPException(status_code=502, detail="SRT 조회 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.")
    return {"dep": SRT_DEP, "arr": SRT_ARR, "trains": [_srt_row(t) for t in trains]}


@app.post("/api/search/korail")
def search_korail(req: SearchReq):
    arr = (req.arr or "").strip()
    if arr not in KTX_ALLOWED_ARR:
        raise HTTPException(status_code=400, detail="도착역은 수원 또는 서울만 가능합니다.")
    client: Korail = _get_session(req.token, "korail")
    try:
        trains = client.search_train(
            KTX_DEP, arr, date=req.date, time=req.time,
            train_type=TrainType.KTX, include_no_seats=True,
        )
    except NoResultsError:
        trains = []
    except Exception:
        with suppress(Exception):
            client.login()
            try:
                trains = client.search_train(
                    KTX_DEP, arr, date=req.date, time=req.time,
                    train_type=TrainType.KTX, include_no_seats=True,
                )
            except NoResultsError:
                trains = []
            return {"dep": KTX_DEP, "arr": arr, "trains": [_ktx_row(t) for t in trains]}
        raise HTTPException(status_code=502, detail="KTX 조회 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.")
    return {"dep": KTX_DEP, "arr": arr, "trains": [_ktx_row(t) for t in trains]}


# ── 행 직렬화 ────────────────────────────────────────────────────────
def _hhmm(s: str) -> str:
    return f"{s[0:2]}:{s[2:4]}" if s and len(s) >= 4 else s


def _srt_row(t):
    return {
        "name": t.train_name,
        "no": t.train_number,
        "dep_time": _hhmm(t.dep_time),
        "arr_time": _hhmm(t.arr_time),
        "general": t.general_seat_available(),
        "special": t.special_seat_available(),
        "standby": t.reserve_standby_available(),
        "general_label": t.general_seat_state,
        "special_label": t.special_seat_state,
    }


def _ktx_row(t):
    return {
        "name": t.train_type_name,
        "no": t.train_no,
        "dep_time": _hhmm(t.dep_time),
        "arr_time": _hhmm(t.arr_time),
        "general": t.has_general_seat(),
        "special": t.has_special_seat(),
        "standby": t.has_waiting_list() if hasattr(t, "has_waiting_list") else False,
        "general_label": "예약가능" if t.has_general_seat() else "매진",
        "special_label": "예약가능" if t.has_special_seat() else "매진",
    }


# ── 정적 파일 ────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
