import os, json, requests, atexit, signal, threading, random, re
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup
from telegram.ext import Updater, MessageHandler, Filters

# ========= ENV =========
load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID     = str(os.getenv("CHAT_ID", "")).strip()
DEFAULT_THRESHOLD = float(os.getenv("THRESHOLD_PCT", "1.0"))  # 기본 임계값 (예: 1.0)
PORT        = int(os.getenv("PORT", "0"))                     # keepalive HTTP 포트 (0이면 비활성)
DATA_DIR    = os.getenv("DATA_DIR", "").strip() or "."        # Render에선 /data 로 설정 (Persistent Disk)

os.makedirs(DATA_DIR, exist_ok=True)

DATA_FILE = os.path.join(DATA_DIR, "portfolio.json")
LOCK_FILE = os.path.join(DATA_DIR, "bot.lock")
UPBIT     = "https://api.upbit.com/v1"

# ========= KEEPALIVE HTTP (선택) =========
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
    """
    /data(또는 DATA_DIR)에 lock 파일을 두고,
    - 살아있는 PID가 있으면 즉시 종료 (중복 실행 방지)
    - 죽은 PID면 lock 재사용
    """
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
def load_state():
    if not os.path.exists(DATA_FILE):
        return {"coins": {}, "default_threshold_pct": DEFAULT_THRESHOLD, "pending": {}}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except:
        return {"coins": {}, "default_threshold_pct": DEFAULT_THRESHOLD, "pending": {}}

    d.setdefault("coins", {})
    d.setdefault("default_threshold_pct", DEFAULT_THRESHOLD)
    d.setdefault("pending", {})

    # 과거 target/stop 필드 마이그레이션 (존재 시 triggers로 이동)
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

# .env 기본값 동기화
if float(state.get("default_threshold_pct", DEFAULT_THRESHOLD)) != float(DEFAULT_THRESHOLD):
    state["default_threshold_pct"] = float(DEFAULT_THRESHOLD)
    save_state()

# ========= KEYBOARDS =========
def MAIN_KB():
    return ReplyKeyboardMarkup(
        [["보기","상태","도움말"],
         ["코인","가격","임계값"],
         ["평단","수량","지정가"]],
        resize_keyboard=True
    )

COIN_MODE_KB = ReplyKeyboardMarkup(
    [["추가","삭제"],["취소"]],
    resize_keyboard=True,
    one_time_keyboard=True
)
CANCEL_KB = ReplyKeyboardMarkup(
    [["취소"]],
    resize_keyboard=True,
    one_time_keyboard=True
)

def coin_kb(include_cancel=True):
    syms = [m.split("-")[1] for m in state["coins"].keys()] or ["BTC","ETH","SOL"]
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

# 이모지 규칙
# 수익중 = 🔴, 손실중 = 🔵, 단순 추가 = ⚪️, 평단만 입력 = 🟡
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
    update.message.reply_text(text, reply_markup=(kb or MAIN_KB()))

def send_ctx(ctx, text):
    if not CHAT_ID:
        return
    ctx.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=MAIN_KB())

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

# ========= 정렬 로직 =========
def sorted_coin_items():
    """
    1순위: qty > 0 (보유)          → 매수총액(avg*qty) 내림차순
    2순위: avg > 0, qty == 0      → 24h 거래대금 내림차순
    3순위: 그 외(단순 추가 등)    → 24h 거래대금 내림차순
    """
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
            primary = -(avg * qty)  # 매수총액 큰 순
        elif avg > 0:
            group = 2
            primary = -vol
        else:
            group = 3
            primary = -vol

        items.append((group, primary, m, info, cur))

    # group asc, primary asc(음수라 desc 효과), 심볼명 asc
    items.sort(key=lambda x: (x[0], x[1], x[2]))
    return items

# ========= SUMMARY / FORMATTERS =========
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
    buy_amt = avg * qty  # 매수총액
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

# ========= RANDOM HOTEL REVIEW ( /호텔 ) =========
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
    line1 = _expand_braces(random.choice(REVIEWS)[0])
    line2 = _expand_braces(random.choice(REVIEWS)[1])
    line3 = _expand_braces(random.choice(REVIEWS)[2])
    return "\n".join([line1, line2, line3])

HELP = (
    "📖 도움말\n"
    "• 버튼으로 실행\n"
    "• 보기: 보유 현황 (보유 코인 매수총액 순 정렬)\n"
    "• 상태: 전체 설정\n"
    "• 코인: 추가/삭제\n"
    "• 지정가: 트리거 추가/삭제/목록/초기화\n"
    "\n"
    "💬 명령어\n"
    "• /호텔 : 두젠틀 후기용 3줄 랜덤 문장 생성"
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

# ========= ACTION HELPERS =========
def ensure_coin(m):
    c = state["coins"].setdefault(m, {
        "avg_price":0.0,
        "qty":0.0,
        "threshold_pct":None,
        "last_notified_price":None,
        "prev_price":None,
        "triggers":[]
    })
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

# ========= ALERT LOOP =========
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

# ========= HANDLER =========
def on_text(update, context):
    if not only_owner(update):
        return

    text = (update.message.text or "").strip()
    cid  = update.effective_chat.id

    # /호텔: 명령어로만 동작
    if text.startswith("/호텔") or text.lower().startswith("/hotel"):
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
                return
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
                return
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

        # 지정가(트리거) 플로우
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

    # ===== 기본 명령 처리 =====
    head = text.split()[0].lstrip("/")

    if head in ["도움말","help"]:
        reply(update, HELP); return

    if head in ["보기","show"]:
        send_view(update); return

    if head in ["상태","status"]:
        send_status(update); return

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
    dp.add_handler(MessageHandler(Filters.text & (~Filters.command), on_text))
    dp.add_handler(MessageHandler(Filters.command, on_text))

    up.job_queue.run_repeating(check_loop, interval=3, first=3)

    def hi(ctx):
        try:
            if CHAT_ID:
                send_ctx(ctx, "봇이 시작되었습니다. ‘보기/상태/코인/지정가’ 버튼을 눌러보세요.")
        except:
            pass

    up.job_queue.run_once(lambda c: hi(c), when=2)

    print("////////////////////////////////////////")
    print(">>> Upbit Telegram Bot is running")
    print("////////////////////////////////////////")

    up.start_polling(clean=True)
    up.idle()

if __name__ == "__main__":
    try:
        main()
    finally:
        _release_lock()
