# app.py
try:
    import pkg_resources
except ImportError:
    import types as _types, sys as _sys
    _pkg = _types.ModuleType('pkg_resources')
    _pkg.get_distribution = lambda name: _types.SimpleNamespace(version='unknown')
    _pkg.DistributionNotFound = Exception
    _sys.modules['pkg_resources'] = _pkg
import os, json, requests, atexit, signal, threading, random, re, time, base64, hmac, hashlib, urllib.parse
from datetime import datetime, timezone, timedelta
KST = timezone(timedelta(hours=9))
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

# Persistent state dir (Render: DATA_DIR=/data)
DATA_DIR    = os.getenv("DATA_DIR", "").strip() or "."
os.makedirs(DATA_DIR, exist_ok=True)

# Naver Searchad API
NAVER_BASE_URL      = "https://api.naver.com"
NAVER_API_KEY       = os.getenv("NAVER_API_KEY", "").strip()
NAVER_API_SECRET    = os.getenv("NAVER_API_SECRET", "").strip()
NAVER_CUSTOMER_ID   = os.getenv("NAVER_CUSTOMER_ID", "").strip()
NAVER_CAMPAIGN_ID   = os.getenv("NAVER_CAMPAIGN_ID", "").strip()
NAVER_ADGROUP_ID    = os.getenv("NAVER_ADGROUP_ID", "").strip()
NAVER_ADGROUP_NAME  = os.getenv("NAVER_ADGROUP_NAME", "").strip()

# Naver Place (리뷰/노출 감시용)
NAVER_PLACE_ID      = os.getenv("NAVER_PLACE_ID", "").strip()

DATA_FILE = os.path.join(DATA_DIR, "portfolio.json")
LOCK_FILE = os.path.join(DATA_DIR, "bot.lock")
UPBIT     = "https://api.upbit.com/v1"

NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; SM-G998N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

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

def _release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except:
        pass

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
            "auto_enabled": False,
            "schedules": [],
            "last_applied": "",
            "last_known_bid": None,
            "adgroup_id": None,
            "abtest": None,
            "rank_watch": {
                "enabled": False,
                "keyword": "",
                "marker": "",
                "interval": 300,
                "last_rank": None,       # 기본(자연) 순위만 저장
                "last_check": 0.0,
            },
            "review_watch": {
                "enabled": False,
                "interval": 180,
                "last_count": None,
                "last_check": 0.0,
            },
        },
        "modes": {},
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
    nav = d.setdefault("naver", {})
    nav.setdefault("auto_enabled", False)
    nav.setdefault("schedules", [])
    nav.setdefault("last_applied", "")
    nav.setdefault("last_known_bid", None)
    nav.setdefault("adgroup_id", None)
    nav.setdefault("abtest", None)

    rw = nav.setdefault("rank_watch", {})
    rw.setdefault("enabled", False)
    rw.setdefault("keyword", "")
    rw.setdefault("marker", "")
    rw.setdefault("interval", 300)
    rw.setdefault("last_rank", None)
    rw.setdefault("last_check", 0.0)

    rv = nav.setdefault("review_watch", {})
    rv.setdefault("enabled", False)
    rv.setdefault("interval", 180)
    rv.setdefault("last_count", None)
    rv.setdefault("last_check", 0.0)

    d.setdefault("modes", {})

    # 코인 데이터 마이그레이션
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

if "default_threshold_pct" not in state:
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
                ["광고상태", "노출현황", "리뷰현황"],
                ["광고시간", "광고설정", "입찰추정"],
                ["광고자동", "노출감시", "리뷰감시"],
                ["도움말", "메뉴"],
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
    try:
        ctx.bot.send_message(chat_id=cid, text=text, reply_markup=MAIN_KB(cid))
    except:
        pass

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

# ========= HOTEL (랜덤 후기 3줄) =========
REVIEWS = [
    [
        "{휴가기간|일주일|며칠|주말} 동안 맡겼는데 너무 좋았어요!",
        "시설도 깔끔하고 아이가 노는 영상을 자주 보내주셔서 안심됐어요.",
        "사장님이 세심하게 챙겨주셔서 다음에도 꼭 맡길 거예요."
    ],
    [
        "{한 달|휴가기간|며칠|일주일} 동안 맡겼는데 완전 만족이에요!",
        "사진이랑 영상으로 아이 소식을 자주 받아서 마음이 놓였어요.",
        "시설도 깨끗하고 분위기도 좋아서 또 이용하려구요."
    ],
    [
        "{며칠|휴가기간|연휴|주말} 동안 맡겼는데 정말 잘 지냈어요.",
        "하루에도 몇 번씩 사진과 영상 보내주셔서 걱정이 싹 사라졌어요.",
        "사장님이 너무 친절해서 믿음이 가는 곳이에요."
    ],
    [
        "{휴가기간|일주일|며칠|연휴} 동안 맡겼는데 대만족이에요!",
        "시설도 깨끗하고 아이가 즐겁게 노는 모습이 영상으로 와서 행복했어요.",
        "두젠틀은 진짜 믿고 맡길 수 있는 곳이에요."
    ],
    [
        "{한 달|휴가기간|며칠|일주일} 동안 맡겼는데 너무 만족스러웠어요.",
        "영상으로 아이가 노는 모습 보내주셔서 매일 안심됐어요.",
        "시설도 깔끔하고 사장님도 세심하게 케어해주셨어요."
    ],
    [
        "{며칠|휴가기간|연휴|주말} 동안 이용했는데 최고였어요.",
        "사진이랑 영상으로 아이 근황 알려주셔서 든든했어요.",
        "시설도 깨끗하고 아이가 밝아져서 너무 만족입니다."
    ],
    [
        "{휴가기간|일주일|3일|며칠} 동안 맡겼는데 정말 마음에 들었어요.",
        "영상으로 아이 상태를 바로 확인할 수 있어서 걱정이 줄었어요.",
        "사장님이 세심하게 챙겨주셔서 믿고 맡길 수 있었습니다."
    ],
    [
        "{한 달|휴가기간|며칠|연휴} 동안 맡겼는데 너무 좋았어요.",
        "사진, 영상으로 아이 소식을 자주 받아서 마음이 편했어요.",
        "시설도 깨끗하고 케어가 꼼꼼해서 정말 만족했어요."
    ],
    [
        "{일주일|휴가기간|며칠|연휴} 동안 맡겼는데 완전 만족이에요.",
        "아이 영상을 수시로 보내주셔서 매일 안심됐어요.",
        "시설도 좋고 분위기도 밝아서 또 맡길 예정이에요."
    ],
    [
        "{한 달|휴가기간|며칠|주말} 동안 맡겼는데 진짜 최고였어요.",
        "하루에도 여러 번 사진, 영상 보내주셔서 믿음이 갔어요.",
        "아이도 행복해 보여서 또 이용하려구요."
    ],
]

def _expand_braces(text: str) -> str:
    def repl(match):
        options = match.group(1).split("|")
        return random.choice(options).strip()
    return re.sub(r"{([^}]+)}", repl, text)

def build_random_hotel_review() -> str:
    first_lines  = [r[0] for r in REVIEWS]
    second_lines = [r[1] for r in REVIEWS]
    third_lines  = [r[2] for r in REVIEWS]
    l1 = _expand_braces(random.choice(first_lines))
    l2 = _expand_braces(random.choice(second_lines))
    l3 = _expand_braces(random.choice(third_lines))
    return "\n".join([l1, l2, l3])

# ========= HELP =========
HELP = (
    "📖 도움말\n"
    "• 모든 명령은 한글로, 슬래시(/) 없이 입력합니다.\n"
    "\n"
    "📊 코인 기능\n"
    "• 보기 / 상태 / 코인 / 가격 / 평단 / 수량 / 임계값 / 지정가\n"
    "\n"
    "📢 네이버 광고 기능\n"
    "• 광고상태 : 현재 설정/감시 요약\n"
    "• 광고설정 X : 입찰가를 X원으로 즉시 변경\n"
    "• 광고시간 : 'HH:MM/입찰가' 형식 시간표 설정\n"
    "• 광고자동 : 시간표 자동 적용 켜기/끄기\n"
    "• 입찰추정 : 1순위 추정 입찰가 자동 탐색\n"
    "• 노출감시 : 플레이스 순위 변동 실시간 감시 (광고/기본 순위 함께 표시)\n"
    "• 노출현황 : 현재 플레이스 순위를 즉시 1회 조회 (광고/기본 순위 함께 표시)\n"
    "• 리뷰감시 : NAVER_PLACE_ID 기준 신규 리뷰 감시\n"
    "• 리뷰현황 : 현재 리뷰 개수를 즉시 1회 조회\n"
    "\n"
    "🏨 호텔 : 랜덤 후기 3줄 생성\n"
    "🔧 메뉴 : '네이버 광고 / 코인 가격알림' 모드 전환"
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

# ========= NAVER 검색 URL =========
def _naver_search_url(keyword: str) -> str:
    q = urllib.parse.quote(keyword)
    # 최신 place 검색 탭 기준
    return f"https://search.naver.com/search.naver?where=place&sm=tab_nx.place&query={q}"

# ========= APOLLO STATE 파서 & 순위 계산 =========
def _extract_js_object(s: str, start_idx: int):
    depth = 0
    in_str = False
    esc = False
    started = False
    for i in range(start_idx, len(s)):
        ch = s[i]
        if not started:
            if ch == "{":
                started = True
                depth = 1
            else:
                continue
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start_idx:i+1]
    return None

def _extract_apollo_state(html: str):
    idx = html.find("__APOLLO_STATE__")
    if idx < 0:
        return None
    brace = html.find("{", idx)
    if brace < 0:
        return None
    obj = _extract_js_object(html, brace)
    if not obj:
        return None
    js = (
        obj.replace("undefined", "null")
           .replace("!0", "true")
           .replace("!1", "false")
    )
    try:
        return json.loads(js)
    except Exception as e:
        print("[NAVER] __APOLLO_STATE__ JSON 파싱 실패:", e)
        return None

def _normalize(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))

def _match_name(name: str, marker: str) -> bool:
    if not name or not marker:
        return False
    nn = _normalize(name)
    mm = _normalize(marker)
    if mm and mm in nn:
        return True
    tokens = [t for t in re.split(r"\s+", marker.strip()) if t]
    if tokens and all(t in name for t in tokens):
        return True
    return False

def _get_name_id(apollo, ref):
    node = apollo.get(ref, {}) or {}
    name = node.get("name") or node.get("businessName") or node.get("title")
    bid = node.get("id") or node.get("businessId")

    # attraction 하위에 실제 place 정보가 있을 수 있음
    if (not name or not bid) and "attraction" in node:
        ref2 = node["attraction"].get("__ref")
        if ref2:
            n2 = apollo.get(ref2, {}) or {}
            if not name:
                name = n2.get("name") or n2.get("businessName") or n2.get("title")
            if not bid:
                bid = n2.get("id") or n2.get("businessId")

    if bid is not None:
        bid = str(bid).strip()
    return name, bid

def detect_place_ranks(html: str, marker: str):
    """
    광고/기본 둘 다 계산:
    - 광고 순위: adBusinesses(...) 순서
    - 기본 순위: attractions(...).businesses(...).items 순서
    반환: {"ad": ad_rank or None, "organic": organic_rank or None} 또는 None
    """
    if not marker:
        return None

    apollo = _extract_apollo_state(html)
    if not apollo:
        return None

    root = apollo.get("ROOT_QUERY", {})

    # 광고 순위
    ad_rank = None
    ad_key = next((k for k in root.keys() if k.startswith("adBusinesses(")), None)
    if ad_key:
        try:
            ad_items = root[ad_key].get("items", [])
            idx = 0
            for it in ad_items:
                ref = it.get("__ref")
                if not ref:
                    continue
                name, _ = _get_name_id(apollo, ref)
                if not name:
                    continue
                idx += 1
                if ad_rank is None and _match_name(name, marker):
                    ad_rank = idx
        except Exception as e:
            print("[NAVER] adBusinesses 파싱 실패:", e)

    # 기본 순위
    org_rank = None
    att_key = next((k for k in root.keys() if k.startswith("attractions(")), None)
    if att_key:
        att = root.get(att_key, {})
        biz_key = next((k for k in att.keys() if k.startswith("businesses(")), None)
        if biz_key:
            biz = att.get(biz_key, {})
            items = biz.get("items", [])
            idx = 0
            for it in items:
                ref = it.get("__ref")
                if not ref:
                    continue
                name, _ = _get_name_id(apollo, ref)
                if not name:
                    continue
                idx += 1
                if org_rank is None and _match_name(name, marker):
                    org_rank = idx

    if ad_rank is None and org_rank is None:
        return None

    return {"ad": ad_rank, "organic": org_rank}

def _fmt_rank(v):
    return f"{v}위" if isinstance(v, int) and v > 0 else "정보 없음"

# ========= NAVER STATUS / SCHEDULE =========
def send_naver_status(update):
    nav = state.setdefault("naver", {})
    auto = "켜짐" if nav.get("auto_enabled") else "꺼짐"
    schedules = nav.get("schedules") or []
    rw = nav.get("rank_watch", {})
    rv = nav.get("review_watch", {})

    lines = ["📢 네이버 광고 상태"]
    lines.append(f"- 자동 변경: {auto}")
    if schedules:
        lines.append("- 시간표:")
        for s in schedules:
            lines.append(f"  · {s['time']} → {s['bid']}원")
    else:
        lines.append("- 시간표: 없음 (광고시간 명령으로 설정)")

    current = None
    try:
        if naver_enabled():
            current = naver_get_bid()
    except:
        pass
    if current is not None:
        try:
            current_int = int(current)
        except:
            current_int = current
        lines.append(f"- 현재 입찰가: {current_int}원")
    else:
        if naver_enabled():
            lines.append("- 현재 입찰가: 조회 실패")
        else:
            lines.append("- 현재 입찰가: Searchad API 미설정")

    last = nav.get("last_applied") or "없음"
    lines.append(f"- 마지막 자동 적용: {last}")

    ab = nav.get("abtest") or {}
    if ab.get("status") == "running":
        lines.append(
            f"- 입찰추정: 진행 중 (키워드 '{ab.get('keyword','')}', "
            f"현재 {ab.get('current_bid')}원, 간격 {ab.get('interval')}초)"
        )

    if rw.get("enabled"):
        lines.append(
            f"- 노출감시: ON (키워드 '{rw.get('keyword','')}', "
            f"간격 {rw.get('interval',300)}초, 최근 기본 순위 {_fmt_rank(rw.get('last_rank'))})"
        )
    else:
        lines.append("- 노출감시: OFF")

    if rv.get("enabled"):
        iv = int(rv.get("interval",180))
        lines.append(
            f"- 리뷰감시: ON (간격 {iv//60}분, 마지막 리뷰수 {rv.get('last_count')})"
        )
    else:
        lines.append("- 리뷰감시: OFF")

    reply(update, "\n".join(lines))

def naver_schedule_loop(context):
    if not naver_enabled():
        return

    nav = state.setdefault("naver", {})
    if not nav.get("auto_enabled"):
        return

    schedules = nav.get("schedules") or []
    if not schedules:
        return

    now = datetime.now(KST)
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

# ========= NAVER 입찰추정 (기존 로직) =========
def detect_ad_position(html: str, marker: str):
    if not marker:
        return None
    idx = html.find(marker)
    if idx < 0:
        return None
    last_rank = None
    for m in re.finditer(r'data-cr-rank="(\d+)"', html):
        pos = m.start()
        rank = int(m.group(1))
        if pos < idx:
            last_rank = rank
        else:
            break
    if last_rank is not None:
        return last_rank
    return 1

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
        "phase": "set",
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

    if phase == "check":
        last = float(ab.get("last_check", 0))
        if now - last < interval:
            return

        html = ""
        try:
            url = _naver_search_url(keyword)
            r = requests.get(url, headers=NAVER_HEADERS, timeout=5)
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
                        f"✅ [입찰추정 완료]\n"
                        f"키워드 '{keyword}' 1순위 추정 입찰가: {cur_bid}원\n"
                        f"(검색 페이지 구조/개인화에 따라 실제와 다를 수 있습니다.)"
                    ),
                    reply_markup=MAIN_KB(cid),
                )
            except:
                pass
            return

        next_bid = cur_bid + step
        if max_bid and next_bid > max_bid:
            ab["status"] = "done"
            save_state()
            try:
                context.bot.send_message(
                    chat_id=cid,
                    text=(
                        f"⚠️ [입찰추정 종료]\n"
                        f"최대 입찰가 {max_bid}원을 초과하여 중단했습니다.\n"
                        f"{cur_bid}원까지 올렸지만 1순위로 추정되지 않았습니다."
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

# ========= NAVER 노출감시 (광고/기본 동시 확인) =========
def naver_rank_watch_loop(context):
    nav = state.setdefault("naver", {})
    cfg = nav.setdefault("rank_watch", {})
    if not cfg.get("enabled"):
        return

    keyword = (cfg.get("keyword") or "").strip()
    marker = (cfg.get("marker") or "").strip()
    interval = int(cfg.get("interval", 300))
    last_check = float(cfg.get("last_check", 0.0))
    now = time.time()

    if not (keyword and marker):
        return
    if now - last_check < interval:
        return

    html = ""
    try:
        url = _naver_search_url(keyword)
        r = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        html = r.text
    except Exception as e:
        print("[NAVER] 노출감시 조회 실패:", e)
        return

    cfg["last_check"] = now

    res = detect_place_ranks(html, marker) if html else None
    if not res:
        print("[NAVER] 노출감시: 지정 문구 결과 없음")
        save_state()
        return

    ad_rank = res.get("ad")
    org_rank = res.get("organic")
    prev_org = cfg.get("last_rank")

    if org_rank is not None:
        if prev_org is None:
            try:
                send_ctx(
                    context,
                    f"📡 [노출감시 시작]\n"
                    f"키워드 '{keyword}'\n"
                    f"광고 : {_fmt_rank(ad_rank)}\n"
                    f"기본 : {_fmt_rank(org_rank)} (광고 제외)"
                )
            except:
                pass
        elif org_rank != prev_org:
            try:
                send_ctx(
                    context,
                    f"📡 [노출감시] 순위 변경\n"
                    f"키워드 '{keyword}'\n"
                    f"이전 기본 : {_fmt_rank(prev_org)} → 현재 기본 : {_fmt_rank(org_rank)}\n"
                    f"광고 : {_fmt_rank(ad_rank)}"
                )
            except:
                pass
        cfg["last_rank"] = org_rank

    save_state()

# ========= NAVER 리뷰감시 =========
def _parse_review_count_from_html(html: str):
    """
    네이버 플레이스 최신 구조 기준 리뷰 수 파싱.
    1순위: __APOLLO_STATE__ 내 VisitorReviewStatsResult / PlaceDetailBase에서 추출
    2순위: 예전 JSON/텍스트 패턴 정규식 (하위 호환)
    """

    # 1) __APOLLO_STATE__ 기반 파싱 (최신 구조)
    apollo = _extract_apollo_state(html)
    if apollo:
        candidates = []

        for v in apollo.values():
            if not isinstance(v, dict):
                continue
            typ = v.get("__typename")

            # VisitorReviewStatsResult 노드
            if typ == "VisitorReviewStatsResult":
                review = v.get("review") or {}
                if isinstance(review, dict):
                    c = review.get("totalCount") or review.get("allCount")
                    if isinstance(c, (int, float)):
                        candidates.append(int(c))

                for field in ["visitorReviewsTotal", "ratingReviewsTotal"]:
                    c = v.get(field)
                    if isinstance(c, (int, float)):
                        candidates.append(int(c))

            # PlaceDetailBase 노드
            if typ == "PlaceDetailBase":
                for field in [
                    "visitorReviewsTotal",
                    "visitorReviewsTextReviewTotal",
                    "reviewCount",
                    "totalReviewCount",
                ]:
                    c = v.get(field)
                    if isinstance(c, (int, float)):
                        candidates.append(int(c))

        # 후보 값들 중 최대값을 리뷰 총합으로 사용
        if candidates:
            return max(candidates)

    # 2) 예전/예비 패턴 (하위 호환용)
    mv = re.search(r'"visitorReviewCount"\s*:\s*(\d+)', html)
    mb = re.search(r'"blogReviewCount"\s*:\s*(\d+)', html)
    if mv or mb:
        v = int(mv.group(1)) if mv else 0
        b = int(mb.group(1)) if mb else 0
        if v or b:
            return v + b

    mv = re.search(r"방문자\s*리뷰\s*([0-9,]+)", html)
    mb = re.search(r"블로그\s*리뷰\s*([0-9,]+)", html)
    if mv or mb:
        v = int(mv.group(1).replace(",", "")) if mv else 0
        b = int(mb.group(1).replace(",", "")) if mb else 0
        if v or b:
            return v + b

    mt = re.search(r'"totalReviewCount"\s*:\s*(\d+)', html)
    if mt:
        return int(mt.group(1))

    # "리뷰 123건" 같은 일반 패턴 (최후 보정)
    ml = re.search(r"리뷰\s*([0-9,]+)\s*건", html)
    if ml:
        return int(ml.group(1).replace(",", ""))

    return None

def get_place_review_count():
    if not NAVER_PLACE_ID:
        return None

    urls = [
        f"https://m.place.naver.com/place/{NAVER_PLACE_ID}",
        f"https://map.naver.com/p/entry/place/{NAVER_PLACE_ID}",
        f"https://pcmap.place.naver.com/restaurant/{NAVER_PLACE_ID}/home",
    ]

    for url in urls:
        try:
            r = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        except Exception as e:
            print(f"[NAVER] 리뷰 URL 요청 실패: {url} :: {e}")
            continue

        try:
            cnt = _parse_review_count_from_html(r.text)
            if cnt is not None:
                return cnt
        except Exception as e:
            print(f"[NAVER] 리뷰 파싱 실패: {url} :: {e}")

    return None


def naver_review_watch_loop(context):
    nav = state.setdefault("naver", {})
    cfg = nav.setdefault("review_watch", {})
    if not cfg.get("enabled"):
        return
    if not NAVER_PLACE_ID:
        return

    now = time.time()
    interval = int(cfg.get("interval", 180))
    last_check = float(cfg.get("last_check", 0.0))

    if now - last_check < interval:
        return

    cnt = get_place_review_count()
    cfg["last_check"] = now

    if cnt is None:
        print("[NAVER] 리뷰감시: 리뷰 수 파싱 실패")
        save_state()
        return

    last = cfg.get("last_count")
    if last is None:
        cfg["last_count"] = cnt
        save_state()
        try:
            send_ctx(
                context,
                f"⭐️ [리뷰감시 시작]\n현재 리뷰 {cnt}건 기준으로 감시합니다."
            )
        except:
            pass
        return

    if cnt > last:
        diff = cnt - last
        cfg["last_count"] = cnt
        save_state()
        try:
            send_ctx(
                context,
                f"⭐️ [리뷰감시]\n신규 리뷰 {diff}건 추가 (총 {cnt}건)"
            )
        except:
            pass
    else:
        save_state()

def naver_review_check_once(update):
    if not NAVER_PLACE_ID:
        reply(update, "NAVER_PLACE_ID가 설정되어 있지 않습니다. .env에 플레이스 ID를 입력하세요.")
        return

    cnt = get_place_review_count()
    if cnt is None:
        reply(update, "리뷰현황 조회 중 오류가 발생했습니다.")
        return

    nav = state.setdefault("naver", {})
    cfg = nav.setdefault("review_watch", {})
    cfg["last_count"] = cnt
    save_state()
    reply(update, f"리뷰현황: 현재 네이버 플레이스 리뷰는 총 {cnt}건입니다.")

# ========= 즉시 노출 조회 =========
def naver_rank_check_once(update):
    nav = state.setdefault("naver", {})
    cfg = nav.setdefault("rank_watch", {})

    keyword = (cfg.get("keyword") or "").strip()
    marker = (cfg.get("marker") or "").strip()

    if not (keyword and marker):
        reply(
            update,
            "노출감시 설정이 되어 있지 않습니다.\n"
            "먼저 '노출감시' 명령으로 키워드와 식별 문구를 설정해 주세요."
        )
        return

    try:
        url = _naver_search_url(keyword)
        r = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        html = r.text
        res = detect_place_ranks(html, marker)
    except Exception as e:
        print("[NAVER] 노출현황 조회 실패:", e)
        reply(update, "노출현황 조회 중 오류가 발생했습니다.")
        return

    if not res:
        reply(
            update,
            "📡 노출현황 알림\n"
            f"🔍 키워드: '{keyword}'\n"
            "⚠️ 검색 결과에서 지정한 매장을 찾지 못했습니다.\n"
            "설정하신 키워드/문구를 다시 한 번 확인해 주세요."
        )
    else:
        ad_rank = res.get("ad")
        org_rank = res.get("organic")
        if org_rank is not None:
            cfg["last_rank"] = org_rank
            save_state()
        reply(
            update,
           "📡 노출현황 알림\n"
        f"🔍 키워드: '{keyword}'\n"
        f"💚 광고 노출: {_fmt_rank(ad_rank)}\n"
        f"📍 기본 노출: {_fmt_rank(org_rank)} (광고 제외)"
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

        # --- 코인 플로우 ---
        if action == "coin" and step == "mode":
            if text not in ["추가","삭제"]:
                reply(update,"‘추가/삭제’ 중 선택하세요.", kb=COIN_MODE_KB)
            else:
                next_action = "coin_add" if text == "추가" else "coin_del"
                set_pending(cid, next_action, "symbol", {})
                reply(update, f"{text}할 코인을 선택하거나 직접 입력하세요.", kb=coin_kb())
            return

        if action in ["coin_add","coin_del"] and step == "symbol":
            symbol = text.upper()
            if action == "coin_add":
                act_add(update, symbol)
            else:
                act_del(update, symbol)
            clear_pending(cid)
            return

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

        # --- 지정가(트리거) 플로우 ---
        if action == "trigger":
            if step == "symbol":
                data["symbol"] = text.upper()
                set_pending(cid, "trigger", "menu", data)
                reply(update, "동작을 선택하세요.", kb=ReplyKeyboardMarkup(
                    [["추가", "삭제"], ["목록", "초기화"], ["취소"]],
                    resize_keyboard=True, one_time_keyboard=True
                ))
                return

            if step == "menu":
                if text not in ["추가","삭제","목록","초기화","취소"]:
                    reply(update, "‘추가/삭제/목록/초기화/취소’ 중 선택하세요.",
                          kb=ReplyKeyboardMarkup(
                              [["추가","삭제"],["목록","초기화"],["취소"]],
                              resize_keyboard=True, one_time_keyboard=True))
                    return
                sym = data["symbol"]

                if text == "목록":
                    m = krw_symbol(sym); c = ensure_coin(m)
                    reply(update, _trigger_list_text(c),
                          kb=ReplyKeyboardMarkup(
                              [["추가","삭제"],["목록","초기화"],["취소"]],
                              resize_keyboard=True, one_time_keyboard=True))
                    return

                if text == "초기화":
                    n = trigger_clear(sym)
                    reply(update, f"트리거 {n}개 삭제됨.",
                          kb=ReplyKeyboardMarkup(
                              [["추가","삭제"],["목록","초기화"],["취소"]],
                              resize_keyboard=True, one_time_keyboard=True))
                    return

                if text == "삭제":
                    m = krw_symbol(sym); c = ensure_coin(m)
                    if not c.get("triggers"):
                        reply(update, "등록된 트리거가 없습니다.",
                              kb=ReplyKeyboardMarkup(
                                  [["추가","삭제"],["목록","초기화"],["취소"]],
                                  resize_keyboard=True, one_time_keyboard=True))
                        return
                    set_pending(cid, "trigger", "delete_select", data)
                    reply(update,
                          _trigger_list_text(c)+"\n삭제할 번호를 입력(예: 1 또는 1,3)",
                          kb=CANCEL_KB)
                    return

                if text == "추가":
                    set_pending(cid, "trigger", "add_mode", data)
                    reply(update, "입력 방식을 선택하세요.",
                          kb=ReplyKeyboardMarkup(
                              [["직접가격","현재가±%","평단가±%"],["취소"]],
                              resize_keyboard=True, one_time_keyboard=True))
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
                    reply(update,"‘직접가격/현재가±%/평단가±%’ 중 선택하세요.",
                          kb=ReplyKeyboardMarkup(
                              [["직접가격","현재가±%","평단가±%"],["취소"]],
                              resize_keyboard=True, one_time_keyboard=True))
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

        # --- 네이버 수동 입찰 ---
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

        # --- 네이버 시간표 ---
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

        # --- 네이버 입찰추정 플로우 ---
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
                reply(update, "검색 결과에서 내 매장을 식별할 문구를 입력하세요.\n예: '두젠틀 애견카페 강남'", kb=CANCEL_KB)
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
                step_bid = 10
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

        # --- 네이버 노출감시 설정 플로우 ---
        if action == "naver_rank_watch":
            nav = state.setdefault("naver", {})
            cfg = nav.setdefault("rank_watch", {})
            if step == "keyword":
                cfg["keyword"] = text.strip()
                set_pending(cid, "naver_rank_watch", "marker", {})
                save_state()
                reply(update, "플레이스 리스트에서 내 매장을 식별할 문구를 입력하세요.\n예: '두젠틀 애견카페 강남'", kb=CANCEL_KB)
                return
            if step == "marker":
                cfg["marker"] = text.strip()
                set_pending(cid, "naver_rank_watch", "interval", {})
                save_state()
                reply(update, "확인 간격(초)을 입력하세요. (권장 300)", kb=CANCEL_KB)
                return
            if step == "interval":
                try:
                    sec = max(30, int(text.strip()))
                except:
                    sec = 300
                cfg["interval"] = sec
                cfg["enabled"] = True
                cfg["last_rank"] = None
                cfg["last_check"] = 0.0
                save_state()
                clear_pending(cid)
                reply(update, f"노출감시를 시작합니다. (간격 {sec}초, 광고/기본 순위 모두 확인)")
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
        if not state["coins"]:
            reply(update, "등록된 코인이 없습니다. ‘코인 → 추가’로 등록하세요.")
        else:
            lines = ["📊 보기"]
            for _, _, m, info, cur in sorted_coin_items():
                lines.append(view_block(m, info, cur))
            reply(update, ("\n".join(lines))[:4000])
        return

    if head in ["상태","status"]:
        g = norm_threshold(state.get("default_threshold_pct", DEFAULT_THRESHOLD))
        header = (
            f"⚙️ 상태(전체 설정)\n"
            f"- 기본 임계값: {g}%\n"
            f"- 등록 코인 수: {len(state['coins'])}\n"
        )
        if not state["coins"]:
            reply(update, header + "- 코인 없음")
        else:
            rows = []
            for _, _, m, info, cur in sorted_coin_items():
                rows.append(status_line(m, info, cur))
            reply(update, (header + "\n".join(rows))[:4000])
        return

    # 네이버 광고 명령
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

    if head == "노출감시":
        nav = state.setdefault("naver", {})
        cfg = nav.setdefault("rank_watch", {})
        if cfg.get("enabled"):
            cfg["enabled"] = False
            save_state()
            reply(update, "노출감시를 중지했습니다.")
        else:
            set_pending(cid, "naver_rank_watch", "keyword", {})
            reply(update, "노출감시용 키워드를 입력하세요. (예: 강남 애견카페)", kb=CANCEL_KB)
        return

    if head in ["노출현황","노출조회","노출상태"]:
        naver_rank_check_once(update)
        return

    # 리뷰감시: 리뷰감시 [분], 리뷰감시중지
    if head in ["리뷰감시중지", "리뷰중지", "리뷰감시끄기"]:
        nav = state.setdefault("naver", {})
        cfg = nav.setdefault("review_watch", {})
        cfg["enabled"] = False
        save_state()
        reply(update, "리뷰감시를 중지했습니다.")
        return

    # 리뷰감시: 리뷰감시 [분]
    if head.startswith("리뷰감시"):
        nav = state.setdefault("naver", {})
        cfg = nav.setdefault("review_watch", {})
        parts = text.split()
        if len(parts) >= 2 and parts[1].isdigit():
            minutes = int(parts[1])
            sec = max(60, minutes * 60)
            cfg["interval"] = sec
        if not NAVER_PLACE_ID:
            reply(update, "NAVER_PLACE_ID가 설정되어 있지 않습니다. .env에 플레이스 ID를 입력하세요.")
            return
        cfg["enabled"] = True
        cfg["last_check"] = 0.0
        save_state()
        iv = int(cfg.get("interval", 180))
        reply(update, f"리뷰감시를 시작합니다. {iv//60}분 간격으로 확인합니다.")
        return

    if head in ["리뷰현황","리뷰조회","리뷰상태"]:
        naver_review_check_once(update)
        return

    # 코인 기본 명령
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

    # Job queues
    up.job_queue.run_repeating(check_loop, interval=3, first=3)
    up.job_queue.run_repeating(naver_schedule_loop, interval=30, first=10)
    up.job_queue.run_repeating(naver_abtest_loop, interval=15, first=15)
    up.job_queue.run_repeating(naver_rank_watch_loop, interval=60, first=20)
    up.job_queue.run_repeating(naver_review_watch_loop, interval=60, first=40)

    def hi(ctx):
        try:
            if CHAT_ID:
                send_ctx(
                    ctx,
                    "김비서 출근했어요 💖"
                )
        except:
            pass

    up.job_queue.run_once(lambda c: hi(c), when=2)

    print("////////////////////////////////////////")
    print(">>> Upbit + Naver Ads + Place Watch Bot is running")
    print("////////////////////////////////////////")

    up.start_polling(clean=True)
    up.idle()

if __name__ == "__main__":
    try:
        main()
    finally:
        _release_lock()

