import os, json, requests, atexit, signal, threading, random, re, time, base64, hmac, hashlib, urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, MessageHandler, Filters, CallbackQueryHandler

# ========= ENV =========
load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = str(os.getenv("CHAT_ID", "")).strip()
DEFAULT_THRESHOLD = float(os.getenv("THRESHOLD_PCT", "1.0"))
PORT        = int(os.getenv("PORT", "0"))
DATA_DIR    = os.getenv("DATA_DIR", "").strip() or "."

# Naver Searchad API
NAVER_BASE_URL      = "https://api.naver.com"
NAVER_API_KEY       = os.getenv("NAVER_API_KEY", "").strip()
NAVER_API_SECRET    = os.getenv("NAVER_API_SECRET", "").strip()
NAVER_CUSTOMER_ID   = os.getenv("NAVER_CUSTOMER_ID", "").strip()
NAVER_CAMPAIGN_ID   = os.getenv("NAVER_CAMPAIGN_ID", "").strip()
NAVER_ADGROUP_ID    = os.getenv("NAVER_ADGROUP_ID", "").strip()
NAVER_ADGROUP_NAME  = os.getenv("NAVER_ADGROUP_NAME", "").strip()

os.makedirs(DATA_DIR, exist_ok=True)

DATA_FILE = os.path.join(DATA_DIR, "portfolio.json")
LOCK_FILE = os.path.join(DATA_DIR, "bot.lock")
UPBIT     = "https://api.upbit.com/v1"

# ========= KEEPALIVE HTTP =========
class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        except:
            pass

    def log_message(self, *a, **k):
        return

def _start_keepalive():
    if PORT <= 0:
        return
    def _run():
        try:
            httpd = HTTPServer(("", PORT), _Ok)
            httpd.serve_forever()
        except:
            pass
    threading.Thread(target=_run, daemon=True).start()

# ========= SINGLE INSTANCE LOCK =========
def _pid_alive(pid:int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except:
        return False

def _acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old = int((f.read() or "0").strip())
            if old and _pid_alive(old):
                print(f"[LOCK] Another bot instance is running (pid={old}). Exit.")
                raise SystemExit(0)
        except:
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_lock)

def _release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except:
        pass

def _setup_signals():
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: (_release_lock(), exit(0)))
        except:
            pass

_acquire_lock()
_setup_signals()

# ========= STATE LOAD/SAVE =========
def _default_state():
    return {
        "coins": {},
        "default_threshold_pct": DEFAULT_THRESHOLD,
        "pending": {},
        "naver": {
            "auto_enabled": False,   # 시간표 자동 변경 on/off
            "schedules": [],         # [{time:"HH:MM", bid:int}, ...]
            "last_applied": "",      # 마지막 적용 키
            "last_known_bid": None,  # 마지막 조회 입찰가
            "adgroup_id": None,      # 캐시된 nccAdgroupId
            "abtest": None,          # 입찰추정 설정
        },
        "modes": {},                # chat_id -> "coin" / "naver"
    }

def load_state():
    if not os.path.exists(DATA_FILE):
        return _default_state()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except:
        return _default_state()

    d.setdefault("coins", {})
    d.setdefault("default_threshold_pct", DEFAULT_THRESHOLD)
    d.setdefault("pending", {})
    naver = d.setdefault("naver", {})
    naver.setdefault("auto_enabled", False)
    naver.setdefault("schedules", [])
    naver.setdefault("last_applied", "")
    naver.setdefault("last_known_bid", None)
    naver.setdefault("adgroup_id", None)
    naver.setdefault("abtest", None)
    d.setdefault("modes", {})

    # 옛 필드 마이그레이션
    changed = False
    for m, info in d["coins"].items():
        info.setdefault("triggers", [])
        info.setdefault("prev_price", None)
        for k in ("target_price", "stop_price"):
            if info.get(k):
                try:
                    v = float(info[k])
                    if v not in info["triggers"]:
                        info["triggers"].append(v)
                        changed = True
                except:
                    pass
                info[k] = None

    if changed:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)

    return d

def save_state():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

state = load_state()

if float(state.get("default_threshold_pct", DEFAULT_THRESHOLD)) != float(DEFAULT_THRESHOLD):
    state["default_threshold_pct"] = float(DEFAULT_THRESHOLD)
    save_state()

# ========= MODE / KEYBOARD =========
def get_mode(cid):
    return state.setdefault("modes", {}).get(str(cid), "coin")

def set_mode(cid, mode):
    state.setdefault("modes", {})[str(cid)] = mode
    save_state()

def MAIN_KB(cid=None):
    mode = get_mode(cid) if cid is not None else "coin"
    if mode == "naver":
        return ReplyKeyboardMarkup(
            [
                ["광고상태", "광고시간", "광고설정"],
                ["입찰추정", "광고자동", "도움말"],
                ["메뉴"],
            ],
            resize_keyboard=True,
        )
    else:
        return ReplyKeyboardMarkup(
            [
                ["보기", "상태", "도움말"],
                ["코인", "가격", "임계값"],
                ["평단", "수량", "지정가"],
                ["메뉴"],
            ],
            resize_keyboard=True,
        )

def mode_inline_kb():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("네이버 광고", callback_data="mode_naver"),
            InlineKeyboardButton("코인 가격알림", callback_data="mode_coin"),
        ]]
    )

COIN_MODE_KB = ReplyKeyboardMarkup(
    [["추가", "삭제"], ["취소"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)
CANCEL_KB = ReplyKeyboardMarkup(
    [["취소"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

def coin_kb(include_cancel=True):
    syms = [m.split("-")[1] for m in state["coins"].keys()] or ["BTC", "ETH", "SOL"]
    rows = [syms[i:i+3] for i in range(0, len(syms), 3)]
    if include_cancel:
        rows.append(["취소"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

# ========= UTIL =========
def only_owner(update):
    return (not CHAT_ID) or (str(update.effective_chat.id) == CHAT_ID)

def krw_symbol(sym):
    s = sym.upper().strip()
    return s if "-" in s else "KRW-" + s

def fmt(n):
    try:
        x = float(n)
        return f"{x:,.0f}" if abs(x) >= 1 else f"{x:,.6f}".rstrip("0").rstrip(".")
    except:
        return str(n)

def get_ticker(market):
    r = requests.get(f"{UPBIT}/ticker", params={"markets": market}, timeout=5)
    r.raise_for_status()
    return r.json()[0]

def get_price(market):
    return float(get_ticker(market)["trade_price"])

def norm_threshold(th):
    if th is None:
        return float(state.get("default_threshold_pct", DEFAULT_THRESHOLD))
    try:
        return float(th)
    except:
        return float(state.get("default_threshold_pct", DEFAULT_THRESHOLD))

def status_emoji(info, cur):
    avg = float(info.get("avg_price", 0.0))
    qty = float(info.get("qty", 0.0))
    if qty <= 0:
        if avg <= 0:
            return "⚪️"
        return "🟡"
    if avg <= 0:
        return "⚪️"
    return "🔴" if cur > avg else "🔵"

def reply(update, text, kb=None):
    cid = update.effective_chat.id
    update.message.reply_text(text, reply_markup=(kb or MAIN_KB(cid)))

def send_ctx(ctx, text):
    if not CHAT_ID:
        return
    try:
        cid = int(CHAT_ID)
    except:
        cid = CHAT_ID
    ctx.bot.send_message(chat_id=cid, text=text, reply_markup=MAIN_KB(cid))

def pretty_sym(sym: str) -> str:
    sym = sym.upper()
    market = "KRW-" + sym
    info = state["coins"].get(market, {})
    try:
        cur = get_price(market)
    except:
        cur = 0.0
    e = status_emoji(info, cur) if info else "⚪️"
    return f"{e} {sym} {e}"

# ========= 코인 정렬/포맷 =========
def sorted_coin_items():
    items = []
    for m, info in state["coins"].items():
        try:
            t = get_ticker(m)
            cur = float(t.get("trade_price", 0.0))
            vol = float(t.get("acc_trade_price_24h", 0.0))
        except:
            cur = 0.0
            vol = 0.0

        avg = float(info.get("avg_price", 0.0))
        qty = float(info.get("qty", 0.0))

        if qty > 0:
            group = 1
            primary = -(avg * qty)
        elif avg > 0:
            group = 2
            primary = -vol
        else:
            group = 3
            primary = -vol

        items.append((group, primary, m, info, cur))

    items.sort(key=lambda x: (x[0], x[1], x[2]))
    return items

def format_triggers(info):
    trigs = info.get("triggers", [])
    return "없음" if not trigs else " | ".join(fmt(t) for t in sorted(set(trigs)))

def status_line(mkt, info, cur):
    sym  = mkt.split("-")[1]
    th   = norm_threshold(info.get("threshold_pct", None))
    lastp= info.get("last_notified_price", None)
    return (
        f"{pretty_sym(sym)} | "
        f"평단가:{fmt(info.get('avg_price',0))}  "
        f"수량:{info.get('qty',0)}  "
        f"임계:{th}  "
        f"마지막통지:{fmt(lastp) if lastp else '없음'}  "
        f"트리거:[{format_triggers(info)}]"
    )

def view_block(mkt, info, cur):
    sym = mkt.split("-")[1]
    avg = float(info.get("avg_price", 0.0))
    qty = float(info.get("qty", 0.0))
    buy_amt = avg * qty
    pnl_p = 0.0 if avg == 0 else (cur/avg - 1) * 100
    pnl_w = (cur - avg) * qty
    th    = norm_threshold(info.get("threshold_pct", None))
    trig  = format_triggers(info)
    head  = f"{pretty_sym(sym)}"
    line1 = f"{sym}  평단가:{fmt(avg)}  보유수량:{qty}  매수금액:{fmt(buy_amt)}"
    line2 = (
        f"현재가:{fmt(cur)}  평가손익({pnl_p:+.2f}%)  "
        f"평가금액:{fmt(pnl_w)}  임계:{th}  트리거:[{trig}]"
    )
    return head + "\n" + line1 + "\n" + line2

# ========= HOTEL (호텔 명령: 슬래시 없이 '호텔') =========
REVIEWS = [
    [
        "{휴가기간|일주일|며칠|주말} 동안 맡겼는데 너무 좋았어요!",
        "시설도 깔끔하고 아이가 노는 영상을 자주 보내주셔서 안심됐어요.",
        "사장님이 세심하게 챙겨주셔서 다음에도 꼭 맡길 거예요."
    ],
    [
        "{한 달|휴가기간|며칠|일주일} 동안 맡겼는데 완전 만족이에요!",
        "사진이랑 영상으로 아이 소식을 자주 보내주셔서 마음이 놓였어요.",
        "시설도 깨끗하고 분위기도 좋아서 또 이용하려구요."
    ],
    [
        "{며칠|휴가기간|연휴|주말} 동안 맡겼는데 정말 잘 지냈어요.",
        "하루에도 몇 번씩 사진과 영상 보내주셔서 걱정이 싹 사라졌어요.",
        "사장님이 너무 친절해서 믿음이 가는 곳이에요."
    ],
]

def _expand_braces(text: str) -> str:
    def repl(match):
        options = match.group(1).split("|")
        return random.choice(options).strip()
    return re.sub(r"{([^}]+)}", repl, text)

def build_random_hotel_review() -> str:
    line1 = _expand_braces(random.choice(REVIEWS)[0])
    line2 = _expand_braces(random.choice(REVIEWS)[1])
    line3 = _expand_braces(random.choice(REVIEWS)[2])
    return "\n".join([line1, line2, line3])

# ========= HELP =========
HELP = (
    "📖 도움말\n"
    "• 모든 명령은 한글로, 슬래시(/) 없이 입력합니다.\n"
    "\n"
    "📊 코인 기능\n"
    "• 보기 : 보유 현황 (보유 코인 매수총액 순 정렬)\n"
    "• 상태 : 전체 설정 확인\n"
    "• 코인 : 코인 추가/삭제\n"
    "• 가격 : 현재가 조회\n"
    "• 평단 : 평단가 설정\n"
    "• 수량 : 보유 수량 설정\n"
    "• 임계값 : 기본/개별 변동 임계값 설정\n"
    "• 지정가 : 트리거(지정가) 추가/삭제/목록/초기화\n"
    "\n"
    "📢 네이버 광고 기능 (플레이스#1_광고그룹#1)\n"
    "• 광고상태 : 현재 입찰가 / 자동 변경 설정 / 시간표 / 입찰추정 상태 조회\n"
    "• 광고설정 X : 입찰가를 X원으로 즉시 변경 (예: '광고설정 300')\n"
    "• 광고시간 : 'HH:MM/입찰가' 형식으로 자동 변경 시간표 설정 (예: 08:00/300 18:00/500)\n"
    "• 광고자동 : 자동 변경 켜기/끄기\n"
    "• 입찰추정 : 검색어/시작가/쿨타임/최대가를 받아 1순위 추정 입찰가 자동 탐색\n"
    "\n"
    "🏨 호텔 기능\n"
    "• 호텔 : 두젠틀 후기용 3줄 랜덤 문장 생성\n"
    "\n"
    "🔧 메뉴\n"
    "• 메뉴 : 인라인 버튼으로 '네이버 광고 / 코인 가격알림' 모드 전환"
)

# ========= PENDING =========
def set_pending(cid, action, step="symbol", data=None):
    p = state["pending"].setdefault(str(cid), {})
    p.update({"action": action, "step": step, "data": data or {}})
    save_state()

def clear_pending(cid):
    state["pending"].pop(str(cid), None)
    save_state()

def get_pending(cid):
    return state["pending"].get(str(cid))

# ========= COIN ACTION HELPERS =========
def ensure_coin(m):
    c = state["coins"].setdefault(
        m,
        {
            "avg_price":0.0,
            "qty":0.0,
            "threshold_pct":None,
            "last_notified_price":None,
            "prev_price":None,
            "triggers":[]
        }
    )
    c.setdefault("triggers", [])
    c.setdefault("prev_price", None)
    return c

def act_add(update, symbol):
    m = krw_symbol(symbol)
    ensure_coin(m)
    save_state()
    reply(update, f"추가 완료: {pretty_sym(m.split('-')[1])}")

def act_del(update, symbol):
    m = krw_symbol(symbol)
    if m in state["coins"]:
        state["coins"].pop(m)
        save_state()
        reply(update, f"삭제 완료: {pretty_sym(m.split('-')[1])}")
    else:
        reply(update, "해당 코인이 없습니다.")

def act_price(update, symbol):
    m = krw_symbol(symbol)
    try:
        p = get_price(m)
        reply(update, f"{pretty_sym(m.split('-')[1])} 현재가 {fmt(p)} 원")
    except:
        reply(update, "가격 조회 실패")

def act_setavg(update, symbol, value):
    m = krw_symbol(symbol)
    c = ensure_coin(m)
    c["avg_price"] = float(value)
    save_state()
    reply(update, f"{pretty_sym(m.split('-')[1])} 평단 {fmt(value)} 원")

def act_setqty(update, symbol, value):
    m = krw_symbol(symbol)
    c = ensure_coin(m)
    c["qty"] = float(value)
    save_state()
    reply(update, f"{pretty_sym(m.split('-')[1])} 수량 {value}")

def act_setrate_default(update, value):
    state["default_threshold_pct"] = float(value)
    save_state()
    reply(update, f"기본 임계값 {value}%")

def act_setrate_symbol(update, symbol, value):
    m = krw_symbol(symbol)
    c = ensure_coin(m)
    c["threshold_pct"] = float(value)
    save_state()
    reply(update, f"{pretty_sym(m.split('-')[1])} 개별 임계값 {value}%")

# ========= TRIGGERS =========
def _trigger_list_text(c):
    trigs = c.get("triggers", [])
    if not trigs:
        return "트리거: 없음"
    lines = [f"{i+1}. {fmt(v)}" for i, v in enumerate(sorted(trigs))]
    return "트리거 목록\n" + "\n".join(lines)

def trigger_add(symbol, mode, value):
    m = krw_symbol(symbol)
    c = ensure_coin(m)
    if mode == "direct":
        target = float(value)
    else:
        if mode == "cur_pct":
            base = get_price(m)
        else:
            base = float(c.get("avg_price", 0.0))
            if base <= 0:
                raise ValueError("평단가가 없습니다.")
        pct = float(value)
        target = base * (1 + pct/100.0)
    c["triggers"].append(float(target))
    save_state()
    return target

def trigger_delete(symbol, indices):
    m = krw_symbol(symbol)
    c = ensure_coin(m)
    trigs = sorted(list(c.get("triggers", [])))
    kept = [v for i, v in enumerate(trigs, start=1) if i not in indices]
    c["triggers"] = kept
    save_state()
    return len(trigs) - len(kept)

def trigger_clear(symbol):
    m = krw_symbol(symbol)
    c = ensure_coin(m)
    n = len(c.get("triggers", []))
    c["triggers"] = []
    save_state()
    return n

# ========= NAVER API HELPERS =========
def naver_enabled():
    return bool(
        NAVER_API_KEY and NAVER_API_SECRET and NAVER_CUSTOMER_ID and
        (NAVER_ADGROUP_ID or NAVER_ADGROUP_NAME)
    )

def _naver_signature(timestamp, method, uri):
    msg = f"{timestamp}.{method}.{uri}"
    digest = hmac.new(
        NAVER_API_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")

def _naver_request(method, uri, params=None, body=None):
    if not naver_enabled():
        raise RuntimeError("NAVER API 미설정")
    ts = str(int(time.time() * 1000))
    sig = _naver_signature(ts, method, uri)
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Timestamp": ts,
        "X-API-KEY": NAVER_API_KEY,
        "X-Customer": NAVER_CUSTOMER_ID,
        "X-Signature": sig,
    }
    url = NAVER_BASE_URL + uri
    if method == "GET":
        return requests.get(url, headers=headers, params=params, timeout=5)
    elif method == "PUT":
        return requests.put(url, headers=headers, params=params, json=body, timeout=5)
    else:
        raise ValueError("Unsupported method")

def _naver_get_adgroup_id():
    nav = state.setdefault("naver", {})

    if NAVER_ADGROUP_ID:
        nav["adgroup_id"] = NAVER_ADGROUP_ID
        save_state()
        return NAVER_ADGROUP_ID

    if nav.get("adgroup_id"):
        return nav["adgroup_id"]

    if not NAVER_ADGROUP_NAME:
        return None

    params = {}
    if NAVER_CAMPAIGN_ID:
        params["nccCampaignId"] = NAVER_CAMPAIGN_ID

    try:
        r = _naver_request("GET", "/ncc/adgroups", params=params)
    except Exception as e:
        print("[NAVER] adgroups 조회 실패:", e)
        return None

    if r.status_code != 200:
        print("[NAVER] adgroups 조회 실패:", r.status_code, r.text)
        return None

    try:
        groups = r.json()
    except:
        return None

    for g in groups:
        if g.get("name") == NAVER_ADGROUP_NAME:
            nav["adgroup_id"] = g.get("nccAdgroupId")
            save_state()
            return nav["adgroup_id"]

    print("[NAVER] 대상 광고그룹 이름 없음:", NAVER_ADGROUP_NAME)
    return None

def naver_get_bid():
    adgroup_id = _naver_get_adgroup_id()
    if not adgroup_id:
        return None
    r = _naver_request("GET", f"/ncc/adgroups/{adgroup_id}")
    if r.status_code != 200:
        print("[NAVER] adgroup 조회 실패:", r.status_code, r.text)
        return None
    data = r.json()
    bid = data.get("bidAmt")
    nav = state.setdefault("naver", {})
    nav["last_known_bid"] = bid
    save_state()
    return bid

def naver_set_bid(new_bid: int):
    adgroup_id = _naver_get_adgroup_id()
    if not adgroup_id:
        return False, "대상 광고그룹(ID)을 찾지 못했습니다. .env 설정을 확인하세요."

    r = _naver_request("GET", f"/ncc/adgroups/{adgroup_id}")
    if r.status_code != 200:
        return False, f"현재 설정 조회 실패 (code {r.status_code})"

    body = r.json()
    old_bid = body.get("bidAmt")

    try:
        new_bid = int(new_bid)
    except:
        return False, "입찰가는 숫자만 가능합니다."

    if old_bid == new_bid:
        nav = state.setdefault("naver", {})
        nav["last_known_bid"] = old_bid
        save_state()
        return False, f"이미 {new_bid}원으로 설정되어 있습니다."

    body["bidAmt"] = new_bid

    r2 = _naver_request("PUT", f"/ncc/adgroups/{adgroup_id}", body=body)
    if r2.status_code != 200:
        return False, f"변경 실패 (code {r2.status_code})"

    res = r2.json()
    applied = res.get("bidAmt")
    nav = state.setdefault("naver", {})
    nav["last_known_bid"] = applied
    save_state()

    if applied == new_bid:
        return True, f"입찰가가 {old_bid} → {applied}원으로 변경되었습니다."
    else:
        return False, "API 응답이 예상과 다릅니다."

def send_naver_status(update):
    if not naver_enabled():
        reply(
            update,
            "네이버 광고 API 정보가 설정되지 않았습니다.\n"
            ".env에 NAVER_API_KEY / NAVER_API_SECRET / NAVER_CUSTOMER_ID / "
            "NAVER_CAMPAIGN_ID / NAVER_ADGROUP_ID / NAVER_ADGROUP_NAME 을 확인하세요."
        )
        return

    nav = state.setdefault("naver", {})
    auto = "켜짐" if nav.get("auto_enabled") else "꺼짐"
    schedules = nav.get("schedules") or []

    lines = ["📢 네이버 광고 상태"]
    lines.append(f"- 자동 변경: {auto}")
    if schedules:
        lines.append("- 시간표:")
        for s in schedules:
            lines.append(f"  · {s['time']} → {s['bid']}원")
    else:
        lines.append("- 시간표: 없음 (광고시간 명령으로 설정)")

    current = naver_get_bid()
    if current is not None:
        try:
            current_int = int(current)
        except:
            current_int = current
        lines.append(f"- 현재 입찰가: {current_int}원")
    else:
        lines.append("- 현재 입찰가: 조회 실패")

    last = nav.get("last_applied") or "없음"
    lines.append(f"- 마지막 자동 적용: {last}")

    ab = nav.get("abtest") or {}
    if ab.get("status") == "running":
        lines.append(
            f"- 입찰추정: 진행 중 (키워드 '{ab.get('keyword','')}', "
            f"현재 {ab.get('current_bid')}원, 간격 {ab.get('interval')}초)"
        )

    reply(update, "\n".join(lines))

# ========= NAVER 시간표 자동 변경 LOOP =========
def naver_schedule_loop(context):
    if not naver_enabled():
        return

    nav = state.setdefault("naver", {})
    if not nav.get("auto_enabled"):
        return

    schedules = nav.get("schedules") or []
    if not schedules:
        return

    now = datetime.now()
    current_hm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")

    for s in schedules:
        t = s.get("time")
        bid = s.get("bid")
        if not t:
            continue
        if current_hm == t:
            key = f"{today} {t} {bid}"
            if nav.get("last_applied") == key:
                continue
            success, msg = naver_set_bid(int(bid))
            nav["last_applied"] = key
            save_state()
            try:
                if success:
                    send_ctx(context, f"✅ [네이버 광고 자동 변경]\n{msg}")
                else:
                    send_ctx(context, f"⚠️ [네이버 광고 자동 변경 실패]\n{msg}")
            except:
                pass

# ========= NAVER 입찰추정 (자동 1순위 탐색) =========
def detect_ad_position(html: str, marker: str):
    # 매우 단순한 휴리스틱:
    # - marker 문자열 위치 기준으로, 그 앞에 나오는 data-cr-rank="N" 중 마지막 값을 순위로 사용
    # - 네이버 페이지 구조 변경 시 오동작할 수 있음 (실사용 시 확인 필요)
    if not marker:
        return None
    idx = html.find(marker)
    if idx < 0:
        return None
    pos = None
    for m in re.finditer(r'data-cr-rank="(\\d+)"', html):
        i = m.start()
        r = int(m.group(1))
        if i < idx:
            pos = r
        else:
            break
    if pos is None:
        return 1
    return pos

def start_naver_abtest(cid, keyword, marker, start_bid, max_bid, step, interval):
    nav = state.setdefault("naver", {})
    nav["abtest"] = {
        "chat_id": cid,
        "keyword": keyword,
        "marker": marker,
        "current_bid": int(start_bid),
        "max_bid": int(max_bid),
        "step": int(step),
        "interval": int(interval),
        "last_check": 0,
        "phase": "set",      # set -> check 반복
        "status": "running",
    }
    save_state()

def naver_abtest_loop(context):
    nav = state.setdefault("naver", {})
    ab = nav.get("abtest")
    if not ab or ab.get("status") != "running":
        return

    cid = ab.get("chat_id")
    now = time.time()
    interval = int(ab.get("interval", 60))
    step = int(ab.get("step", 10))
    cur_bid = int(ab.get("current_bid", 0))
    max_bid = int(ab.get("max_bid", 0))
    keyword = ab.get("keyword", "")
    marker = ab.get("marker", "")
    phase = ab.get("phase", "set")

    if not (cid and keyword and cur_bid > 0 and step > 0):
        ab["status"] = "stopped"
        save_state()
        return

    # 1) 입찰 설정 단계
    if phase == "set":
        success, msg = naver_set_bid(cur_bid)
        if not success:
            ab["status"] = "stopped"
            save_state()
            try:
                context.bot.send_message(
                    chat_id=cid,
                    text=f"⚠️ [입찰추정 종료] 입찰 설정 실패: {msg}",
                    reply_markup=MAIN_KB(cid),
                )
            except:
                pass
            return

        ab["phase"] = "check"
        ab["last_check"] = now
        save_state()
        try:
            context.bot.send_message(
                chat_id=cid,
                text=f"🔧 [입찰추정] {cur_bid}원으로 설정. {interval}초 후 노출 위치 확인.",
                reply_markup=MAIN_KB(cid),
            )
        except:
            pass
        return

    # 2) 순위 확인 단계
    if phase == "check":
        last = float(ab.get("last_check", 0))
        if now - last < interval:
            return

        html = ""
        try:
            url = (
                "https://search.naver.com/search.naver"
                "?where=nexearch&sm=tab_hty.top&query="
                + urllib.parse.quote(keyword)
            )
            r = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5,
            )
            html = r.text
        except Exception as e:
            print("[NAVER] 검색 결과 조회 실패:", e)

        pos = detect_ad_position(html, marker) if html else None

        if pos == 1:
            ab["status"] = "done"
            save_state()
            try:
                context.bot.send_message(
                    chat_id=cid,
                    text=(
                        f"✅ [입찰추정 완료]\\n"
                        f"키워드 '{keyword}' 1순위 추정 입찰가: {cur_bid}원\\n"
                        f"(검색 페이지 구조/상황에 따라 실제와 다를 수 있습니다.)"
                    ),
                    reply_markup=MAIN_KB(cid),
                )
            except:
                pass
            return

        # 1순위가 아니거나 위치 판단 실패 → 입찰 인상 후 재시도
        next_bid = cur_bid + step
        if max_bid and next_bid > max_bid:
            ab["status"] = "done"
            save_state()
            try:
                context.bot.send_message(
                    chat_id=cid,
                    text=(
                        f"⚠️ [입찰추정 종료]\\n"
                        f"최대 입찰가 {max_bid}원을 초과하여 중단했습니다.\\n"
                        f"현재 {cur_bid}원까지 올렸지만 1순위로 추정되지 않습니다."
                    ),
                    reply_markup=MAIN_KB(cid),
                )
            except:
                pass
            return

        ab["current_bid"] = next_bid
        ab["phase"] = "set"
        ab["last_check"] = now
        save_state()
        try:
            context.bot.send_message(
                chat_id=cid,
                text=f"ℹ️ [입찰추정] 1순위 아님 → {next_bid}원으로 재시도합니다.",
                reply_markup=MAIN_KB(cid),
            )
        except:
            pass

# ========= VIEW / STATUS =========
def send_view(update):
    if not state["coins"]:
        reply(update, "등록된 코인이 없습니다. ‘코인 → 추가’로 등록하세요.")
        return
    lines = ["📊 보기"]
    for _, _, m, info, cur in sorted_coin_items():
        lines.append(view_block(m, info, cur))
    reply(update, ("\n".join(lines))[:4000])

def send_status(update):
    g = norm_threshold(state.get("default_threshold_pct", DEFAULT_THRESHOLD))
    header = (
        f"⚙️ 상태(전체 설정)\n"
        f"- 기본 임계값: {g}%\n"
        f"- 등록 코인 수: {len(state['coins'])}\n"
    )
    if not state["coins"]:
        reply(update, header + "- 코인 없음")
        return
    rows = []
    for _, _, m, info, cur in sorted_coin_items():
        rows.append(status_line(m, info, cur))
    reply(update, (header + "\n".join(rows))[:4000])

# ========= TRIGGER 메뉴 키보드 =========
def trigger_menu_kb():
    return ReplyKeyboardMarkup(
        [["추가", "삭제"], ["목록", "초기화"], ["취소"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def trigger_add_mode_kb():
    return ReplyKeyboardMarkup(
        [["직접가격", "현재가±%", "평단가±%"], ["취소"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

# ========= INLINE MODE HANDLER =========
def on_mode_select(update, context):
    q = update.callback_query
    cid = q.message.chat_id
    data = q.data

    if not ((not CHAT_ID) or (str(cid) == CHAT_ID)):
        q.answer()
        return

    if data == "mode_naver":
        set_mode(cid, "naver")
        q.answer("네이버 광고 모드로 전환되었습니다.")
        q.message.reply_text("네이버 광고 모드입니다.", reply_markup=MAIN_KB(cid))
    elif data == "mode_coin":
        set_mode(cid, "coin")
        q.answer("코인 가격알림 모드로 전환되었습니다.")
        q.message.reply_text("코인 가격알림 모드입니다.", reply_markup=MAIN_KB(cid))
    else:
        q.answer()

# ========= TEXT HANDLER =========
def on_text(update, context):
    if not only_owner(update):
        return

    text = (update.message.text or "").strip()
    cid  = update.effective_chat.id

    # 호텔
    if text == "호텔" or text.startswith("/호텔") or text.lower().startswith("/hotel"):
        update.message.reply_text(build_random_hotel_review())
        return

    pend = get_pending(cid)
    if pend:
        action = pend.get("action")
        step   = pend.get("step")
        data   = pend.get("data", {})

        if text == "취소":
            clear_pending(cid)
            reply(update, "취소되었습니다.")
            return

        # 코인 추가/삭제 모드 선택
        if action == "coin" and step == "mode":
            if text not in ["추가","삭제"]:
                reply(update,"‘추가/삭제’ 중 선택하세요.", kb=COIN_MODE_KB)
            else:
                next_action = "coin_add" if text == "추가" else "coin_del"
                set_pending(cid, next_action, "symbol", {})
                reply(update, f"{text}할 코인을 선택하거나 직접 입력하세요.", kb=coin_kb())
            return

        # 코인 추가/삭제 실행
        if action in ["coin_add","coin_del"] and step == "symbol":
            symbol = text.upper()
            if action == "coin_add":
                act_add(update, symbol)
            else:
                act_del(update, symbol)
            clear_pending(cid)
            return

        # 가격/평단/수량/개별 임계값: 심볼 입력 단계
        if step == "symbol" and action in ["price","setavg","setqty","setrate_coin"]:
            symbol = text.upper()
            data["symbol"] = symbol
            if action == "price":
                act_price(update, symbol)
                clear_pending(cid)
            else:
                set_pending(cid, action, "value", data)
                label = {
                    "setavg":"평단가(원)",
                    "setqty":"수량",
                    "setrate_coin":"임계값(%)"
                }[action]
                reply(update, f"{symbol} {label} 값을 숫자로 입력하세요.", kb=CANCEL_KB)
            return

        # 값 입력 단계
        if step == "value" and action in ["setavg","setqty","setrate_coin"]:
            v = text.replace(",", "")
            try:
                float(v)
            except:
                reply(update,"숫자만 입력하세요. 취소는 ‘취소’", kb=CANCEL_KB)
                return
            symbol = data.get("symbol","")
            if action == "setavg":
                act_setavg(update, symbol, v)
            elif action == "setqty":
                act_setqty(update, symbol, v)
            elif action == "setrate_coin":
                act_setrate_symbol(update, symbol, v)
            clear_pending(cid)
            return

        # 지정가(트리거)
        if action == "trigger":
            if step == "symbol":
                data["symbol"] = text.upper()
                set_pending(cid, "trigger", "menu", data)
                reply(update, "동작을 선택하세요.", kb=trigger_menu_kb())
                return

            if step == "menu":
                if text not in ["추가","삭제","목록","초기화","취소"]:
                    reply(update, "‘추가/삭제/목록/초기화/취소’ 중 선택하세요.", kb=trigger_menu_kb())
                    return
                sym = data["symbol"]

                if text == "목록":
                    m = krw_symbol(sym); c = ensure_coin(m)
                    reply(update, _trigger_list_text(c), kb=trigger_menu_kb())
                    return

                if text == "초기화":
                    n = trigger_clear(sym)
                    reply(update, f"트리거 {n}개 삭제됨.", kb=trigger_menu_kb())
                    return

                if text == "삭제":
                    m = krw_symbol(sym); c = ensure_coin(m)
                    if not c.get("triggers"):
                        reply(update, "등록된 트리거가 없습니다.", kb=trigger_menu_kb())
                        return
                    set_pending(cid, "trigger", "delete_select", data)
                    reply(update, _trigger_list_text(c)+"\n삭제할 번호를 입력(예: 1 또는 1,3)", kb=CANCEL_KB)
                    return

                if text == "추가":
                    set_pending(cid, "trigger", "add_mode", data)
                    reply(update, "입력 방식을 선택하세요.", kb=trigger_add_mode_kb())
                    return

            if step == "delete_select":
                nums = []
                for part in text.replace(" ","").split(","):
                    if part.isdigit():
                        nums.append(int(part))
                if not nums:
                    reply(update, "번호를 올바르게 입력하세요. 예: 1 또는 1,3", kb=CANCEL_KB)
                    return
                cnt = trigger_delete(data["symbol"], set(nums))
                clear_pending(cid)
                reply(update, f"{cnt}개 삭제 완료.")
                return

            if step == "add_mode":
                if text not in ["직접가격","현재가±%","평단가±%"]:
                    reply(update,"‘직접가격/현재가±%/평단가±%’ 중 선택하세요.", kb=trigger_add_mode_kb())
                    return
                data["mode"] = (
                    "direct"  if text == "직접가격" else
                    "cur_pct" if text == "현재가±%" else
                    "avg_pct"
                )
                set_pending(cid, "trigger", "add_value", data)
                msg = "가격(원)을 입력하세요." if data["mode"]=="direct" else "변화율(%)을 입력하세요. 예: 5 또는 -5"
                reply(update, msg, kb=CANCEL_KB)
                return

            if step == "add_value":
                v = text.replace("%","").replace(",","")
                try:
                    float(v)
                except:
                    reply(update,"숫자만 입력하세요.", kb=CANCEL_KB)
                    return
                try:
                    trg = trigger_add(data["symbol"], data["mode"], float(v))
                except ValueError as e:
                    reply(update, f"오류: {e}", kb=CANCEL_KB)
                    return
                clear_pending(cid)
                reply(update, f"트리거 등록: {data['symbol'].upper()} {fmt(trg)}원")
                return

        # 네이버: 수동 입찰 변경
        if action == "naver_manual" and step == "value":
            v = text.replace(",", "")
            try:
                bid = int(v)
            except:
                reply(update, "숫자만 입력하세요. 취소는 ‘취소’", kb=CANCEL_KB)
                return
            success, msg = naver_set_bid(bid)
            clear_pending(cid)
            reply(update, f"✅ {msg}" if success else f"⚠️ {msg}")
            return

        # 네이버: 시간표 설정
        if action == "naver_schedule" and step == "input":
            raw = text.replace("\n", " ").strip()
            parts = [p for p in raw.split() if p]
            schedules = []
            ok = True
            for part in parts:
                try:
                    t_str, bid_str = part.split("/", 1)
                    t_str = t_str.strip()
                    bid = int(bid_str.replace(",", "").strip())
                    datetime.strptime(t_str, "%H:%M")
                    schedules.append({"time": t_str, "bid": bid})
                except:
                    ok = False
                    break
            if not ok or not schedules:
                reply(update, "형식이 올바르지 않습니다. 예: 08:00/300 18:00/500", kb=CANCEL_KB)
                return
            nav = state.setdefault("naver", {})
            nav["schedules"] = schedules
            nav.setdefault("auto_enabled", False)
            nav["last_applied"] = ""
            save_state()
            clear_pending(cid)
            status = "켜짐" if nav["auto_enabled"] else "꺼짐"
            reply(update, f"자동 변경 시간표 저장 완료. (자동 변경 현재: {status})")
            return

        # 네이버: 입찰추정 설정 플로우
        if action == "naver_abtest":
            if step == "keyword":
                data["keyword"] = text.strip()
                set_pending(cid, "naver_abtest", "start_bid", data)
                reply(update, "입찰 추정을 시작할 '시작 입찰가(원)'를 입력하세요.", kb=CANCEL_KB)
                return

            if step == "start_bid":
                v = text.replace(",", "")
                try:
                    start_bid = int(v)
                except:
                    reply(update, "숫자만 입력하세요. 취소는 ‘취소’", kb=CANCEL_KB)
                    return
                data["start_bid"] = start_bid
                set_pending(cid, "naver_abtest", "marker", data)
                reply(update, "검색 결과에서 내 광고를 식별할 문구를 입력하세요.\n예: '두젠틀 예약카페 강남'", kb=CANCEL_KB)
                return

            if step == "marker":
                data["marker"] = text.strip()
                set_pending(cid, "naver_abtest", "interval", data)
                reply(update, "노출위치 확인 간격(초)을 입력하세요. (권장 60)", kb=CANCEL_KB)
                return

            if step == "interval":
                v = text.strip()
                if v:
                    try:
                        interval = max(10, int(v))
                    except:
                        interval = 60
                else:
                    interval = 60
                data["interval"] = interval
                set_pending(cid, "naver_abtest", "max_bid", data)
                reply(update, "최대 입찰가(원)를 입력하세요. (이 금액을 넘기면 추정을 중단합니다.)", kb=CANCEL_KB)
                return

            if step == "max_bid":
                v = text.replace(",", "")
                try:
                    max_bid = int(v)
                except:
                    start_bid = int(data.get("start_bid", 0))
                    max_bid = start_bid + 200
                keyword = data.get("keyword", "")
                marker = data.get("marker", "")
                start_bid = int(data.get("start_bid", 0))
                interval = int(data.get("interval", 60))
                step_bid = 10  # 10원 단위
                clear_pending(cid)
                start_naver_abtest(cid, keyword, marker, start_bid, max_bid, step_bid, interval)
                reply(
                    update,
                    f"입찰추정을 시작합니다.\n"
                    f"- 키워드: {keyword}\n"
                    f"- 시작 입찰가: {start_bid}원\n"
                    f"- 최대 입찰가: {max_bid}원\n"
                    f"- 확인 간격: {interval}초\n"
                    f"- 상승 단위: {step_bid}원",
                )
                return

    # ===== 기본 명령 처리 =====
    head = text.split()[0].lstrip("/")

    if head in ["도움말","help"]:
        reply(update, HELP)
        return

    if head == "메뉴":
        update.message.reply_text("모드를 선택하세요.", reply_markup=mode_inline_kb())
        return

    if head in ["보기","show"]:
        send_view(update)
        return

    if head in ["상태","status"]:
        send_status(update)
        return

    # 네이버 광고 모듈
    if head == "광고상태":
        send_naver_status(update)
        return

    if head == "광고설정":
        parts = text.split()
        if len(parts) >= 2:
            v = parts[1].replace(",", "")
            try:
                bid = int(v)
                success, msg = naver_set_bid(bid)
                reply(update, f"✅ {msg}" if success else f"⚠️ {msg}")
                return
            except:
                pass
        set_pending(cid, "naver_manual", "value", {})
        reply(update, "변경할 입찰가(원)를 숫자로 입력하세요.", kb=CANCEL_KB)
        return

    if head == "광고시간":
        set_pending(cid, "naver_schedule", "input", {})
        reply(update, "자동 변경 시간을 설정합니다. 예: 08:00/300 18:00/500", kb=CANCEL_KB)
        return

    if head == "광고자동":
        nav = state.setdefault("naver", {})
        nav["auto_enabled"] = not bool(nav.get("auto_enabled"))
        save_state()
        status = "켜짐" if nav["auto_enabled"] else "꺼짐"
        reply(update, f"네이버 광고 자동 변경이 '{status}' 상태입니다.")
        return

    if head in ["입찰추정","자동입찰"]:
        set_pending(cid, "naver_abtest", "keyword", {})
        reply(update, "입찰 추정을 위한 검색어를 입력하세요.", kb=CANCEL_KB)
        return

    # 코인 모듈
    if head == "코인":
        set_pending(cid, "coin", "mode", {})
        reply(update, "코인 관리 방식을 선택하세요.", kb=COIN_MODE_KB)
        return

    if head == "가격":
        set_pending(cid, "price", "symbol", {})
        reply(update, "조회할 코인을 선택하거나 직접 입력하세요.", kb=coin_kb())
        return

    if head == "평단":
        set_pending(cid, "setavg", "symbol", {})
        reply(update, "코인을 선택하거나 직접 입력하세요.", kb=coin_kb())
        return

    if head == "수량":
        set_pending(cid, "setqty", "symbol", {})
        reply(update, "코인을 선택하거나 직접 입력하세요.", kb=coin_kb())
        return

    if head == "임계값":
        parts = text.split()
        if len(parts) == 2:
            v = parts[1].replace(",","")
            try:
                act_setrate_default(update, float(v))
                return
            except:
                pass
        set_pending(cid, "setrate_coin", "symbol", {})
        reply(update, "개별 임계값 설정할 코인을 선택하거나 직접 입력하세요.", kb=coin_kb())
        return

    if head == "지정가":
        set_pending(cid, "trigger", "symbol", {})
        reply(update, "코인을 선택하거나 직접 입력하세요.", kb=coin_kb())
        return

    reply(update, HELP)

# ========= COIN ALERT LOOP =========
def check_loop(context):
    if not state["coins"]:
        return
    for m, info in list(state["coins"].items()):
        try:
            cur = get_price(m)
        except:
            continue

        # 변동 알림
        if info.get("last_notified_price") is None:
            info["last_notified_price"] = cur

        base = info.get("last_notified_price", cur)
        th   = norm_threshold(info.get("threshold_pct", None))

        try:
            delta = abs(cur/base - 1) * 100
        except:
            delta = 0

        if base > 0 and delta >= th:
            up = cur > base
            arrow = "🔴" if up else "🔵"
            sym = m.split("-")[1]
            avg = float(info.get("avg_price", 0.0))
            qty = float(info.get("qty", 0.0))
            pnl_w = (cur - avg) * qty
            pnl_p = 0.0 if avg == 0 else (cur/avg - 1) * 100
            msg = (
                f"📈 변동 알림({th}%) {arrow}\n"
                f"{pretty_sym(sym)}: {fmt(base)} → {fmt(cur)} 원 ({(cur/base-1)*100:+.2f}%)\n"
                f"평가손익:{pnl_p:+.2f}%  평가금액:{fmt(pnl_w)}"
            )
            try:
                send_ctx(context, msg)
            except:
                pass
            info["last_notified_price"] = cur

        # 지정가 트리거 알림
        prev = info.get("prev_price")
        if prev is None:
            info["prev_price"] = cur
            continue

        trigs = list(info.get("triggers", []))
        fired = []
        for t in trigs:
            try:
                t = float(t)
                up_cross   = (prev < t <= cur)
                down_cross = (prev > t >= cur)
                if up_cross or down_cross:
                    sym = m.split("-")[1]
                    direction = "🔴 상향" if up_cross else "🔵 하향"
                    try:
                        send_ctx(
                            context,
                            f"🎯 트리거 도달\n{direction} {sym}: 현재 {fmt(cur)}원 | 트리거 {fmt(t)}원"
                        )
                    except:
                        pass
                    fired.append(t)
            except:
                pass

        if fired:
            info["triggers"] = [x for x in info.get("triggers", []) if x not in fired]

        info["prev_price"] = cur

    save_state()

# ========= MAIN =========
def main():
    _start_keepalive()

    if not BOT_TOKEN:
        print("BOT_TOKEN 누락")
        return

    up = Updater(BOT_TOKEN, use_context=True)

    try:
        up.bot.delete_webhook(drop_pending_updates=True)
    except:
        pass

    dp = up.dispatcher
    dp.add_handler(CallbackQueryHandler(on_mode_select))
    dp.add_handler(MessageHandler(Filters.text & (~Filters.command), on_text))
    dp.add_handler(MessageHandler(Filters.command, on_text))

    # 코인 체크
    up.job_queue.run_repeating(check_loop, interval=3, first=3)
    # 네이버 시간표 자동 변경
    up.job_queue.run_repeating(naver_schedule_loop, interval=30, first=10)
    # 입찰추정 루프 (내부에서 간격 체크)
    up.job_queue.run_repeating(naver_abtest_loop, interval=15, first=15)

    def hi(ctx):
        try:
            if CHAT_ID:
                send_ctx(
                    ctx,
                    "봇이 시작되었습니다. '메뉴' 키로 모드를 선택하세요.\n"
                    "- 코인: 보기/상태/코인/지정가\n"
                    "- 네이버 광고: 광고상태/광고설정/광고시간/광고자동/입찰추정"
                )
        except:
            pass

    up.job_queue.run_once(lambda c: hi(c), when=2)

    print("////////////////////////////////////////")
    print(">>> Upbit + Naver Ads Telegram Bot is running")
    print("////////////////////////////////////////")

    up.start_polling(clean=True)
    up.idle()

if __name__ == "__main__":
    try:
        main()
    finally:
        _release_lock()
