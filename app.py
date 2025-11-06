import os, json, requests, atexit, signal, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup
from telegram.ext import Updater, MessageHandler, Filters

# ========== ENV ==========
load_dotenv()
BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = str(os.getenv("CHAT_ID","")).strip()
DEFAULT_THRESHOLD = float(os.getenv("THRESHOLD_PCT","1.0"))  # 기본 1.0%

PORT = int(os.getenv("PORT","0"))  # Render keepalive (0이면 비활성)

DATA_FILE = "portfolio.json"
LOCK_FILE = "bot.lock"
UPBIT     = "https://api.upbit.com/v1"

# ========== KEEPALIVE ==========
class _Ok(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        try:
            self.send_response(200); self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers(); self.wfile.write(b"OK")
        except: pass
    def log_message(self, *a, **k): return

def _start_keepalive():
    if PORT<=0: return
    def _run():
        try:
            httpd = HTTPServer(("", PORT), _Ok)
            httpd.serve_forever()
        except: pass
    threading.Thread(target=_run, daemon=True).start()

# ========== LOCK ==========
def _pid_alive(pid:int)->bool:
    try: os.kill(pid,0); return True
    except: return False
def _acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE,"r") as f: old=int((f.read() or "0").strip())
            if old and _pid_alive(old):
                print(f"[LOCK] already running pid={old}"); raise SystemExit(0)
        except: pass
    with open(LOCK_FILE,"w") as f: f.write(str(os.getpid()))
    atexit.register(_release_lock)
def _release_lock():
    try:
        if os.path.exists(LOCK_FILE): os.remove(LOCK_FILE)
    except: pass
for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, lambda *_: (_release_lock(), exit(0)))
_acquire_lock()

# ========== STATE ==========
def load_state():
    if not os.path.exists(DATA_FILE):
        return {"coins":{}, "default_threshold_pct":DEFAULT_THRESHOLD, "pending":{}}
    with open(DATA_FILE,"r",encoding="utf-8") as f:
        d=json.load(f)
    d.setdefault("coins",{})
    d.setdefault("default_threshold_pct",DEFAULT_THRESHOLD)
    d.setdefault("pending",{})
    # migrate target/stop -> triggers
    changed=False
    for m,info in d["coins"].items():
        info.setdefault("triggers",[])
        info.setdefault("prev_price",None)
        for k in ("target_price","stop_price"):
            if info.get(k):
                try:
                    v=float(info[k])
                    if v not in info["triggers"]:
                        info["triggers"].append(v); changed=True
                except: pass
                info[k]=None
    if changed:
        tmp=DATA_FILE+".tmp"
        with open(tmp,"w",encoding="utf-8") as f: json.dump(d,f,ensure_ascii=False,indent=2)
        os.replace(tmp,DATA_FILE)
    return d

def save_state():
    tmp=DATA_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(state,f,ensure_ascii=False,indent=2)
    os.replace(tmp,DATA_FILE)

state=load_state()
# 환경 기본값을 항상 반영(파일에 과거 값이 있어도 .env 1.0을 우선)
if float(state.get("default_threshold_pct", DEFAULT_THRESHOLD)) != float(DEFAULT_THRESHOLD):
    state["default_threshold_pct"]=float(DEFAULT_THRESHOLD); save_state()

# ========== KEYBOARDS ==========
def MAIN_KB():
    return ReplyKeyboardMarkup(
        [["보기","상태","도움말"],
         ["코인","가격","임계값"],
         ["평단","수량","지정가"]],
        resize_keyboard=True
    )

COIN_MODE_KB = ReplyKeyboardMarkup([["추가","삭제"],["취소"]], resize_keyboard=True, one_time_keyboard=True)
CANCEL_KB    = ReplyKeyboardMarkup([["취소"]], resize_keyboard=True, one_time_keyboard=True)

def coin_kb(include_cancel=True):
    syms=[m.split("-")[1] for m in state["coins"].keys()] or ["BTC","ETH","SOL"]
    rows=[syms[i:i+3] for i in range(0,len(syms),3)]
    if include_cancel: rows.append(["취소"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

def trigger_menu_kb():
    return ReplyKeyboardMarkup(
        [["추가","삭제","목록"],["초기화","취소"]],
        resize_keyboard=True, one_time_keyboard=True
    )

def trigger_add_mode_kb():
    return ReplyKeyboardMarkup(
        [["직접가격","현재가±%","평단가±%"],["취소"]],
        resize_keyboard=True, one_time_keyboard=True
    )

# ========== UTIL ==========
def only_owner(update): return (not CHAT_ID) or (str(update.effective_chat.id)==CHAT_ID)
def krw_symbol(sym): 
    s=sym.upper().strip()
    return s if "-" in s else "KRW-"+s
def fmt(n):
    try:
        x=float(n)
        return f"{x:,.0f}" if abs(x)>=1 else f"{x:,.6f}".rstrip("0").rstrip(".")
    except: return str(n)
def get_price(market):
    r=requests.get(f"{UPBIT}/ticker", params={"markets":market}, timeout=5); r.raise_for_status()
    return float(r.json()[0]["trade_price"])
def norm_threshold(th):
    if th is None: return float(state.get("default_threshold_pct", DEFAULT_THRESHOLD))
    try: return float(th)
    except: return float(state.get("default_threshold_pct", DEFAULT_THRESHOLD))

# 이모지 규칙 (요청 사양)
# 수익중 = 🔴, 손실중 = 🔵, 단순 추가 = ⚪️(avg=0, qty=0), 평단만 입력 = 🟡(avg>0, qty=0)
def status_emoji(info, cur):
    avg=float(info.get("avg_price",0.0))
    qty=float(info.get("qty",0.0))
    if qty<=0:
        if avg<=0: return "⚪️"
        return "🟡"
    if avg<=0: return "⚪️"
    return "🔴" if cur>avg else "🔵"

def pretty_sym(sym:str)->str:
    sym = sym.upper()
    market = "KRW-"+sym
    info = state["coins"].get(market, {})
    try:
        cur = get_price(market)
    except:
        cur = 0.0
    e = status_emoji(info, cur) if info else "⚪️"
    return f"{e} {sym} {e}"

def reply(update, text, kb=None):
    update.message.reply_text(text, reply_markup=(kb or MAIN_KB()))
def send_ctx(ctx, text):
    ctx.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=MAIN_KB())

# ========== SUMMARY/FORMATTERS ==========
def format_triggers(info):
    trigs = info.get("triggers",[])
    return "없음" if not trigs else " | ".join(fmt(t) for t in sorted(set(trigs)))

def status_line(mkt, info, cur):
    sym=mkt.split("-")[1]
    th=norm_threshold(info.get("threshold_pct",None))
    lastp=info.get("last_notified_price",None)
    return (f"{pretty_sym(sym)} | "
            f"평단가:{fmt(info.get('avg_price',0))}  "
            f"수량:{info.get('qty',0)}  "
            f"임계:{th}  "
            f"마지막통지:{fmt(lastp) if lastp else '없음'}  "
            f"트리거:[{format_triggers(info)}]")

def view_block(mkt, info, cur):
    sym=mkt.split("-")[1]
    avg=float(info.get("avg_price",0.0))
    qty=float(info.get("qty",0.0))
    buy_amt=avg*qty
    pnl_p = 0.0 if avg==0 else (cur/avg-1)*100
    pnl_w = (cur-avg)*qty
    th    = norm_threshold(info.get("threshold_pct",None))
    trig  = format_triggers(info)
    head  = f"{pretty_sym(sym)}"
    # 두 줄 블록(가독성 Up)
    line1 = f"{sym}  평단가:{fmt(avg)}  보유수량:{qty}  매수금액:{fmt(buy_amt)}"
    line2 = f"현재가:{fmt(cur)}  평가손익({pnl_p:+.2f}%)  평가금액:{fmt(pnl_w)}  임계:{th}  트리거:[{trig}]"
    return head+"\n"+line1+"\n"+line2

HELP=(
"📖 도움말\n"
"• 버튼으로 실행\n"
"• 보기: 보유 현황\n"
"• 상태: 전체 설정\n"
"• 코인: 추가/삭제\n"
"• 지정가: 트리거 추가/삭제/목록/초기화 (가격 관통 시 1회 알림 후 삭제)"
)

# ========== PENDING ==========
def set_pending(cid, action, step="symbol", data=None):
    p=state["pending"].setdefault(str(cid),{})
    p.update({"action":action,"step":step,"data":data or {}})
    save_state()
def clear_pending(cid):
    state["pending"].pop(str(cid),None); save_state()
def get_pending(cid):
    return state["pending"].get(str(cid))

# ========== ACTIONS ==========
def ensure_coin(m):
    c = state["coins"].setdefault(m, {
        "avg_price":0.0,"qty":0.0,
        "threshold_pct":None,
        "last_notified_price":None,
        "prev_price":None,
        "triggers":[]
    })
    c.setdefault("triggers", [])
    c.setdefault("prev_price", None)
    return c

def act_add(update, symbol):
    m=krw_symbol(symbol); ensure_coin(m); save_state()
    reply(update, f"추가 완료: {pretty_sym(m.split('-')[1])}")

def act_del(update, symbol):
    m=krw_symbol(symbol)
    if m in state["coins"]:
        state["coins"].pop(m); save_state()
        reply(update, f"삭제 완료: {pretty_sym(m.split('-')[1])}")
    else:
        reply(update, "해당 코인이 없습니다.")

def act_price(update, symbol):
    m=krw_symbol(symbol)
    try:
        p=get_price(m); reply(update, f"{pretty_sym(m.split('-')[1])} 현재가 {fmt(p)} 원")
    except:
        reply(update, "가격 조회 실패")

def act_setavg(update,symbol,value):
    m=krw_symbol(symbol); c=ensure_coin(m); c["avg_price"]=float(value); save_state()
    reply(update, f"{pretty_sym(m.split('-')[1])} 평단 {fmt(value)} 원")

def act_setqty(update,symbol,value):
    m=krw_symbol(symbol); c=ensure_coin(m); c["qty"]=float(value); save_state()
    reply(update, f"{pretty_sym(m.split('-')[1])} 수량 {value}")

def act_setrate_default(update,value):
    state["default_threshold_pct"]=float(value); save_state()
    reply(update, f"기본 임계값 {value}%")

def act_setrate_symbol(update,symbol,value):
    m=krw_symbol(symbol); c=ensure_coin(m); c["threshold_pct"]=float(value); save_state()
    reply(update, f"{pretty_sym(m.split('-')[1])} 개별 임계값 {value}%")

# 트리거
def _trigger_list_text(c):
    trigs = c.get("triggers",[])
    if not trigs: return "트리거: 없음"
    lines = [f"{i+1}. {fmt(v)}" for i,v in enumerate(sorted(trigs))]
    return "트리거 목록\n" + "\n".join(lines)

def trigger_add(symbol, mode, value):
    m=krw_symbol(symbol); c=ensure_coin(m)
    if mode=="direct":
        target=float(value)
    else:
        if mode=="cur_pct":
            base=get_price(m)
        else:
            base=float(c.get("avg_price",0.0))
            if base<=0: raise ValueError("평단가가 없습니다.")
        pct=float(value)
        target = base*(1+pct/100.0)
    c["triggers"].append(float(target))
    save_state(); return target

def trigger_delete(symbol, indices):
    m=krw_symbol(symbol); c=ensure_coin(m)
    trigs = sorted(list(c.get("triggers",[])))
    kept=[v for i,v in enumerate(trigs, start=1) if i not in indices]
    c["triggers"]=kept; save_state()
    return len(trigs)-len(kept)

def trigger_clear(symbol):
    m=krw_symbol(symbol); c=ensure_coin(m)
    n=len(c.get("triggers",[])); c["triggers"]=[]; save_state(); return n

# ========== VIEW / STATUS ==========
def send_view(update):
    if not state["coins"]:
        reply(update, "등록된 코인이 없습니다. ‘코인 → 추가’로 등록하세요."); return
    lines=["📊 보기"]
    for m,info in state["coins"].items():
        try: cur=get_price(m)
        except: cur=0.0
        lines.append(view_block(m,info,cur))
    reply(update, ("\n".join(lines))[:4000])

def send_status(update):
    g=norm_threshold(state.get("default_threshold_pct", DEFAULT_THRESHOLD))
    header=f"⚙️ 상태(전체 설정)\n- 기본 임계값: {g}%\n- 등록 코인 수: {len(state['coins'])}\n"
    if not state["coins"]:
        reply(update, header+"- 코인 없음"); return
    rows=[]
    for m,c in state["coins"].items():
        try: cur=get_price(m)
        except: cur=0.0
        rows.append(status_line(m,c,cur))
    reply(update, (header+"\n".join(rows))[:4000])

# ========== ALERT LOOP ==========
def check_loop(context):
    if not state["coins"]: return
    for m,info in list(state["coins"].items()):
        try: cur=get_price(m)
        except: continue

        # 변동 알림 (상승/하락 이모지 포함)
        if info.get("last_notified_price") is None:
            info["last_notified_price"]=cur
        base=info.get("last_notified_price",cur)
        th=norm_threshold(info.get("threshold_pct",None))
        try: delta=abs(cur/base-1)*100
        except: delta=0
        if delta>=th and base>0:
            up = cur>base
            arrow = "🔴" if up else "🔵"   # 상승=빨강, 하락=파랑
            sym=m.split("-")[1]
            avg=float(info.get("avg_price",0.0)); qty=float(info.get("qty",0.0))
            pnl_w=(cur-avg)*qty; pnl_p=0.0 if avg==0 else (cur/avg-1)*100
            msg=(f"📈 변동 알림({th}%) {arrow}\n"
                 f"{pretty_sym(sym)}: {fmt(base)} → {fmt(cur)} 원 ({(cur/base-1)*100:+.2f}%)\n"
                 f"평가손익:{pnl_p:+.2f}%  평가금액:{fmt(pnl_w)}")
            try: send_ctx(context, msg)
            except: pass
            info["last_notified_price"]=cur

        # 트리거 교차 알림
        prev = info.get("prev_price")
        if prev is None:
            info["prev_price"]=cur
            continue
        trigs = list(info.get("triggers",[]))
        fired=[]
        for t in trigs:
            try:
                t=float(t)
                up_cross   = (prev < t <= cur)
                down_cross = (prev > t >= cur)
                if up_cross or down_cross:
                    sym=m.split("-")[1]
                    direction = "🔴 상향" if up_cross else "🔵 하향"
                    try:
                        send_ctx(context, f"🎯 트리거 도달\n{direction} {sym}: 현재 {fmt(cur)}원 | 트리거 {fmt(t)}원")
                    except: pass
                    fired.append(t)
            except: pass
        if fired:
            info["triggers"]=[x for x in info.get("triggers",[]) if x not in fired]
        info["prev_price"]=cur
    save_state()

# ========== HANDLER ==========
def on_text(update, context):
    if not only_owner(update): return
    text=(update.message.text or "").strip()

    pend=get_pending(update.effective_chat.id)
    if pend:
        action=pend.get("action"); step=pend.get("step"); data=pend.get("data",{})
        if text=="취소":
            clear_pending(update.effective_chat.id); reply(update,"취소되었습니다."); return

        # 코인 추가/삭제
        if action=="coin" and step=="mode":
            if text not in ["추가","삭제"]:
                reply(update,"‘추가/삭제’ 중 선택하세요.", kb=COIN_MODE_KB); return
            next_action = "coin_add" if text=="추가" else "coin_del"
            set_pending(update.effective_chat.id, next_action, "symbol", {})
            reply(update, f"{text}할 코인을 선택하거나 직접 입력하세요.", kb=coin_kb()); return

        if action in ["coin_add","coin_del"] and step=="symbol":
            symbol=text.upper()
            if action=="coin_add": act_add(update, symbol)
            else:                   act_del(update, symbol)
            clear_pending(update.effective_chat.id); return

        # 가격/평단/수량/개별 임계값
        if step=="symbol":
            symbol=text.upper(); data["symbol"]=symbol
            if action in ["price","setavg","setqty","setrate_coin"]:
                if action=="price":
                    act_price(update, symbol); clear_pending(update.effective_chat.id); return
                set_pending(update.effective_chat.id, action, "value", data)
                label={"setavg":"평단가(원)","setqty":"수량","setrate_coin":"임계값(%)"}[action]
                reply(update, f"{symbol} {label} 값을 숫자로 입력하세요.", kb=CANCEL_KB); return
        if step=="value":
            v=text.replace(",","")
            if action in ["setavg","setqty","setrate_coin"]:
                try: float(v)
                except: reply(update,"숫자만 입력하세요. 취소는 ‘취소’", kb=CANCEL_KB); return
                symbol=data.get("symbol","")
                if action=="setavg": act_setavg(update,symbol,v)
                elif action=="setqty": act_setqty(update,symbol,v)
                elif action=="setrate_coin": act_setrate_symbol(update,symbol,v)
                clear_pending(update.effective_chat.id); return

        # 지정가(트리거) 플로우
        if action=="trigger":
            if step=="symbol":
                data["symbol"]=text.upper()
                set_pending(update.effective_chat.id, "trigger", "menu", data)
                reply(update, "동작을 선택하세요.", kb=trigger_menu_kb()); return
            if step=="menu":
                if text not in ["추가","삭제","목록","초기화","취소"]:
                    reply(update, "‘추가/삭제/목록/초기화/취소’ 중 선택하세요.", kb=trigger_menu_kb()); return
                if text=="목록":
                    m=krw_symbol(data["symbol"]); c=ensure_coin(m)
                    reply(update, _trigger_list_text(c), kb=trigger_menu_kb()); return
                if text=="초기화":
                    n=trigger_clear(data["symbol"])
                    reply(update, f"트리거 {n}개 삭제됨.", kb=trigger_menu_kb()); return
                if text=="삭제":
                    m=krw_symbol(data["symbol"]); c=ensure_coin(m)
                    if not c.get("triggers"):
                        reply(update, "등록된 트리거가 없습니다.", kb=trigger_menu_kb()); return
                    set_pending(update.effective_chat.id, "trigger", "delete_select", data)
                    reply(update, _trigger_list_text(c)+"\n삭제할 번호를 입력(예: 1 또는 1,3)", kb=CANCEL_KB); return
                if text=="추가":
                    set_pending(update.effective_chat.id, "trigger", "add_mode", data)
                    reply(update, "입력 방식을 선택하세요.", kb=trigger_add_mode_kb()); return
            if step=="delete_select":
                nums=[]
                for part in text.replace(" ","").split(","):
                    if part.isdigit(): nums.append(int(part))
                if not nums:
                    reply(update, "번호를 올바르게 입력하세요. 예: 1 또는 1,3", kb=CANCEL_KB); return
                cnt=trigger_delete(data["symbol"], set(nums))
                clear_pending(update.effective_chat.id)
                reply(update, f"{cnt}개 삭제 완료."); return
            if step=="add_mode":
                if text not in ["직접가격","현재가±%","평단가±%"]:
                    reply(update,"‘직접가격/현재가±%/평단가±%’ 중 선택하세요.", kb=trigger_add_mode_kb()); return
                data["mode"]=("direct" if text=="직접가격" else "cur_pct" if text=="현재가±%" else "avg_pct")
                set_pending(update.effective_chat.id, "trigger", "add_value", data)
                reply(update, ("가격(원)을 입력하세요." if data["mode"]=="direct"
                               else "변화율(%)을 입력하세요. 예: 5 또는 -5"), kb=CANCEL_KB); return
            if step=="add_value":
                v=text.replace("%","").replace(",","")
                try: float(v)
                except: reply(update,"숫자만 입력하세요.", kb=CANCEL_KB); return
                try:
                    trg=trigger_add(data["symbol"], data["mode"], float(v))
                except ValueError as e:
                    reply(update, f"오류: {e}", kb=CANCEL_KB); return
                clear_pending(update.effective_chat.id)
                reply(update, f"트리거 등록: {data['symbol'].upper()} {fmt(trg)}원"); return

    # 기본 명령
    head=text.split()[0].lstrip("/")
    if head in ["도움말","help"]: reply(update, HELP); return
    if head in ["보기","show"]:     send_view(update); return
    if head in ["상태","status"]:   send_status(update); return
    if head in ["코인"]:
        set_pending(update.effective_chat.id, "coin", "mode", {})
        reply(update, "코인 관리 방식을 선택하세요.", kb=COIN_MODE_KB); return
    if head in ["가격"]:
        set_pending(update.effective_chat.id, "price", "symbol", {})
        reply(update, "조회할 코인을 선택하거나 직접 입력하세요.", kb=coin_kb()); return
    if head in ["평단"]:
        set_pending(update.effective_chat.id, "setavg", "symbol", {})
        reply(update, "코인을 선택하거나 직접 입력하세요.", kb=coin_kb()); return
    if head in ["수량"]:
        set_pending(update.effective_chat.id, "setqty", "symbol", {})
        reply(update, "코인을 선택하거나 직접 입력하세요.", kb=coin_kb()); return
    if head in ["임계값"]:
        parts=text.split()
        if len(parts)==2:
            v=parts[1].replace(",","")
            try: act_setrate_default(update,float(v)); return
            except: pass
        set_pending(update.effective_chat.id, "setrate_coin", "symbol", {})
        reply(update, "개별 임계값 설정할 코인을 선택하거나 직접 입력하세요.", kb=coin_kb()); return
    if head in ["지정가"]:
        set_pending(update.effective_chat.id, "trigger", "symbol", {})
        reply(update, "코인을 선택하거나 직접 입력하세요.", kb=coin_kb()); return

    reply(update, HELP)

# ========== MAIN ==========
def main():
    _start_keepalive()
    if not BOT_TOKEN:
        print("BOT_TOKEN 누락"); return
    up=Updater(BOT_TOKEN, use_context=True)
    try: up.bot.delete_webhook(drop_pending_updates=True)
    except: pass
    dp=up.dispatcher
    dp.add_handler(MessageHandler(Filters.text & (~Filters.command), on_text))
    dp.add_handler(MessageHandler(Filters.command, on_text))
    up.job_queue.run_repeating(check_loop, interval=3, first=3)
    def hi(ctx):
        try: send_ctx(ctx, "봇이 시작되었습니다. ‘보기/상태/코인/지정가’ 버튼을 눌러보세요.")
        except: pass
    up.job_queue.run_once(lambda c: hi(c), when=2)
    up.start_polling(clean=True); up.idle()

if __name__=="__main__": main()
