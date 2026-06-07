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
from SRT.errors import SRTLoginError, SRTResponseError
from SRT.passenger import Adult
from SRT.seat_type import SeatType
from korail2 import Korail, TrainType, NoResultsError, AdultPassenger, ReserveOption, SoldOutError

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


class ReserveReq(BaseModel):
    token: str
    date: str        # YYYYMMDD
    time: str        # HHMMSS (검색 시작 시각)
    train_no: str    # 선점할 열차 번호
    dep_time: str    # HH:MM (열차 식별 보조)
    arr: str | None = None  # KTX 전용
    adults: int = 1  # 어른 인원


# ── 로그인 ───────────────────────────────────────────────────────────
@app.post("/api/login/srt")
def login_srt(req: LoginReq):
    try:
        client = SRT(req.id.strip(), req.pw)
    except (SRTLoginError, SRTResponseError) as e:
        # SRT 가 주는 실제 사유를 그대로 노출 (예: "존재하지않는 회원입니다.")
        reason = str(e).strip() or "아이디/비밀번호를 확인해 주세요."
        raise HTTPException(status_code=400, detail=f"SRT 로그인 실패: {reason} (SRT 회원 가입 여부·아이디 형식을 확인해 주세요. SRT와 코레일은 별도 회원입니다.)")
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
        raise HTTPException(
            status_code=400,
            detail="코레일 로그인 실패: 아래를 확인해 주세요. ① 코레일(KTX) 회원인지 — SRT와 코레일은 별도 회원입니다. "
                   "② 아이디 형식(회원번호 8~10자리 / 이메일 / 휴대폰). ③ 코레일이 자동화 도구를 일시 차단(MACRO)했을 수 있어, "
                   "이 경우 잠시 후 재시도하거나 코레일톡 공식 앱을 이용해 주세요.",
        )
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


# ── 예매(좌석 선점) ──────────────────────────────────────────────────
# 결제는 하지 않는다. 선점만 하고, 사용자가 시간 내 직접 결제.
def _clamp_adults(n: int) -> int:
    return max(1, min(9, int(n or 1)))


@app.post("/api/reserve/srt")
def reserve_srt(req: ReserveReq):
    client: SRT = _get_session(req.token, "srt")
    adults = _clamp_adults(req.adults)
    try:
        trains = client.search_train(
            SRT_DEP, SRT_ARR, date=req.date, time=req.time, available_only=False
        )
    except Exception:
        raise HTTPException(status_code=502, detail="열차 정보를 다시 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")
    target = next(
        (t for t in trains if t.train_number == req.train_no and _hhmm(t.dep_time) == req.dep_time),
        None,
    )
    if target is None:
        raise HTTPException(status_code=409, detail="해당 열차를 찾지 못했습니다. 새로 조회 후 다시 시도해 주세요.")
    if not target.seat_available():
        raise HTTPException(status_code=409, detail="방금 좌석이 매진되었습니다. 다른 열차를 시도해 주세요.")
    try:
        res = client.reserve(target, passengers=[Adult(adults)], special_seat=SeatType.GENERAL_FIRST)
    except Exception:
        raise HTTPException(status_code=409, detail="좌석 선점에 실패했습니다. 좌석이 사라졌거나 미결제 예약이 이미 있을 수 있습니다.")
    return _reserve_result("SRT", target, adults,
                           number=getattr(res, "reservation_number", None),
                           cost=getattr(res, "total_cost", None),
                           pay_date=getattr(res, "payment_date", None),
                           pay_time=getattr(res, "payment_time", None))


@app.post("/api/reserve/korail")
def reserve_korail(req: ReserveReq):
    arr = (req.arr or "").strip()
    if arr not in KTX_ALLOWED_ARR:
        raise HTTPException(status_code=400, detail="도착역은 수원 또는 서울만 가능합니다.")
    client: Korail = _get_session(req.token, "korail")
    adults = _clamp_adults(req.adults)
    try:
        trains = client.search_train(
            KTX_DEP, arr, date=req.date, time=req.time,
            train_type=TrainType.KTX, include_no_seats=True,
        )
    except NoResultsError:
        trains = []
    except Exception:
        raise HTTPException(status_code=502, detail="열차 정보를 다시 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")
    target = next(
        (t for t in trains if t.train_no == req.train_no and _hhmm(t.dep_time) == req.dep_time),
        None,
    )
    if target is None:
        raise HTTPException(status_code=409, detail="해당 열차를 찾지 못했습니다. 새로 조회 후 다시 시도해 주세요.")
    if not target.has_seat():
        raise HTTPException(status_code=409, detail="방금 좌석이 매진되었습니다. 다른 열차를 시도해 주세요.")
    try:
        res = client.reserve(target, passengers=[AdultPassenger(adults)], option=ReserveOption.GENERAL_FIRST)
    except SoldOutError:
        raise HTTPException(status_code=409, detail="방금 좌석이 매진되었습니다. 다른 열차를 시도해 주세요.")
    except Exception:
        raise HTTPException(status_code=409, detail="좌석 선점에 실패했습니다. 좌석이 사라졌거나 미결제 예약이 이미 있을 수 있습니다.")
    pay_limit = None
    if getattr(res, "buy_limit_date", None) and getattr(res, "buy_limit_time", None):
        d, t = res.buy_limit_date, res.buy_limit_time
        pay_limit = f"{d[4:6]}/{d[6:8]} {t[0:2]}:{t[2:4]}"
    return _reserve_result("KTX", target, adults,
                           number=getattr(res, "rsv_id", None) or getattr(res, "journey_no", None),
                           cost=getattr(res, "price", None),
                           pay_limit=pay_limit)


def _reserve_result(kind, train, adults, number=None, cost=None,
                    pay_date=None, pay_time=None, pay_limit=None):
    name = getattr(train, "train_name", None) or getattr(train, "train_type_name", "")
    no = getattr(train, "train_number", None) or getattr(train, "train_no", "")
    if pay_limit is None and pay_date and pay_time:
        pay_limit = f"{pay_date[4:6]}/{pay_date[6:8]} {pay_time[0:2]}:{pay_time[2:4]}"
    return {
        "ok": True,
        "kind": kind,
        "train": f"{name} {no}",
        "dep_time": _hhmm(train.dep_time),
        "arr_time": _hhmm(train.arr_time),
        "adults": adults,
        "number": number,
        "cost": cost,
        "pay_limit": pay_limit,
    }


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
