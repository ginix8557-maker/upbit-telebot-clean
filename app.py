import os, json, requests, atexit, signal
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup
from telegram.ext import Updater, MessageHandler, Filters

# ========== ENV ==========
load_dotenv()
BOT_TOKEN   = os.getenv("BOT_TOKEN","").strip()
CHAT_ID     = str(os.getenv("CHAT_ID","")).strip()
DEFAULT_THRESHOLD = float(os.getenv("THRESHOLD_PCT","1.0"))  # 기본 1.0%

DATA_FILE = "portfolio.json"
LOCK_FILE = "bot.lock"
UPBIT     = "https://api.upbit.com/v1"

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
    return d
def save_state():
    tmp=DATA_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(state,f,ensure_ascii=False,indent=2)
    os.replace(tmp,DATA_FILE)
state=load_state()

# ========== KEYBOARDS (3×3) ==========
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

# ========== UTIL ==========
def only_owner(update): return (not CHAT_ID) or (str(update.effective_chat.id)==CHAT_ID)
def krw_symbol(sym): 
    s=sym.upper().strip()
    return s if "-" in s else "KRW-"+s
def fmt(n):
    try: return f"{float(n):,.0f}"
    except: return str(n)
def get_price(market):
    r=requests.get(f"{UPBIT}/ticker", params={"markets":market}, timeout=5); r.raise_for_status()
    return float(r.json()[0]["trade_price"])
def norm_threshold(th):
    if th is None: return float(state.get("default_threshold_pct", DEFAULT_THRESHOLD))
    try: return float(th)
    except: return float(state.get("default_threshold_pct", DEFAULT_THRESHOLD))

# --- 이모지 규칙: 🟢(수익, 보유·현재가>평단) / 🔴(손실, 보유·현재가<평단) / ⚪️(미보유 또는 평단 0) ---
def pretty_sym(sym:str)->str:
    sym = sym.upper()
    market = "KRW-"+sym
    info = state["coins"].get(market)
    if not info:
        e = "⚪️"
    else:
        avg = float(info.get("avg_price",0.0))
        qty = float(info.get("qty",0.0))
        if avg <= 0 or qty <= 0:
            e = "⚪️"
        else:
            try:
                cur = get_price(market)
                e = "🟢" if cur > avg else "🔴"
            except:
                e = "⚪️"
    return f"{e} {sym} {e}"

def reply(update, text, kb=None):
    update.message.reply_text(text, reply_markup=(kb or MAIN_KB()))
def send_ctx(ctx, text):
    ctx.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=MAIN_KB())

# ========== SUMMARY ==========
def summary_line(mkt, info, cur):
    sym=mkt.split("-")[1]  # KRW 제거
    avg=float(info.get("avg_price",0.0)); qty=float(info.get("qty",0.0))
    amt=avg*qty; pnl_w=(cur-avg)*qty; pnl_p=0.0 if avg==0 else (cur/avg-1)*100
    th=norm_threshold(info.get("threshold_pct",None))
    t=info.get("target_price"); s=info.get("stop_price")
    extra=[]
    if t: extra.append(f"목표:{fmt(t)}")
    if s: extra.append(f"손절:{fmt(s)}")
    extra_txt=(" | "+", ".join(extra)) if extra else ""
    return f"{pretty_sym(sym)} / {avg:,.0f} / {qty} / {amt:,.0f} / {cur:,.0f} / {pnl_w:,.0f} ({pnl_p:+.2f}%) | 임계 {th}%{extra_txt}"

HELP=(
"📖 도움말\n"
"• 버튼만 눌러 실행 (슬래시 불필요)\n"
"• 보기: 손익 요약, 상태: 전체 설정\n"
"• 코인: 추가/삭제 통합 관리\n"
"• 지정가: 목표가·손절가 설정(직접입력/현재가±%/평단가±%) — 둘 중 도달 시 ‘지정가 도달’ 신호"
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
    return state["coins"].setdefault(m, {"avg_price":0.0,"qty":0.0,"threshold_pct":None,"last_notified_price":None,"target_price":None,"stop_price":None})

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

# 지정가
def set_target_stop(update, symbol, which, mode, value):
    m=krw_symbol(symbol); c=ensure_coin(m)
    if which=="초기화":
        c["target_price"]=None; c["stop_price"]=None; save_state()
        reply(update, f"{pretty_sym(m.split('-')[1])} 지정가 초기화 완료"); return
    cur=None
    if mode in ("cur_pct","avg_pct"):
        if mode=="cur_pct":
            try: cur=get_price(m)
            except: reply(update,"현재가 조회 실패"); return
        else:
            cur=float(c.get("avg_price",0.0))
            if cur<=0: reply(update,"평단가가 없습니다. 먼저 ‘평단’ 설정을 해주세요."); return
    if mode=="direct":
        final=float(value)
    else:
        pct=float(value); base=cur
        final = base*(1+pct/100) if which=="목표가" else base*(1-pct/100)
    if which=="목표가": c["target_price"]=final
    else:               c["stop_price"]=final
    save_state()
    reply(update, f"{pretty_sym(m.split('-')[1])} {which} 설정: {fmt(final)} 원")

# ========== VIEW / STATUS ==========
def send_view(update):
    if not state["coins"]:
        reply(update, "등록된 코인이 없습니다. ‘코인 → 추가’로 등록하세요."); return
    lines=[]
    for m,info in state["coins"].items():
        try: cur=get_price(m)
        except: cur=0.0
        lines.append(summary_line(m,info,cur))
    reply(update, ("📊 보기(요약)\n"+"\n".join(lines))[:4000])
def send_status(update):
    g=norm_threshold(state.get("default_threshold_pct", DEFAULT_THRESHOLD))
    header=f"⚙️ 상태(전체 설정)\n- 기본 임계값: {g}%\n- 등록 코인 수: {len(state['coins'])}\n"
    if not state["coins"]:
        reply(update, header+"- 코인 없음"); return
    rows=[]
    for m,c in state["coins"].items():
        th=norm_threshold(c.get("threshold_pct",None))
        lastp=c.get("last_notified_price",None)
        tg=c.get("target_price"); sp=c.get("stop_price")
        extra=[]
        if tg: extra.append(f"목표:{fmt(tg)}")
        if sp: extra.append(f"손절:{fmt(sp)}")
        rows.append(f"{pretty_sym(m.split('-')[1])} | avg:{fmt(c.get('avg_price',0))} qty:{c.get('qty',0)} | 임계:{th} | 마지막통지:{fmt(lastp) if lastp else '없음'}"
                    + ((" | "+", ".join(extra)) if extra else ""))
    reply(update, (header+"\n".join(rows))[:4000])

# ========== ALERT LOOP ==========
def check_loop(context):
    if not state["coins"]: return
    for m,info in list(state["coins"].items()):
        try: cur=get_price(m)
        except: continue
        if info.get("last_notified_price") is None:
            info["last_notified_price"]=cur; save_state()
        base=info.get("last_notified_price")
        th=norm_threshold(info.get("threshold_pct",None))
        try: delta=abs(cur/base-1)*100
        except: delta=0
        if delta>=th:
            sym=m.split("-")[1]
            avg=float(info.get("avg_price",0.0)); qty=float(info.get("qty",0.0))
            pnl_w=(cur-avg)*qty; pnl_p=0.0 if avg==0 else (cur/avg-1)*100
            msg=(f"📈 변동 알림({th}%)\n{pretty_sym(sym)}: {fmt(base)} → {fmt(cur)} 원 ({(cur/base-1)*100:+.2f}%)\n"
                 f"[요약] {sym} / {avg:,.0f} / {qty} / {(avg*qty):,.0f} / {cur:,.0f} / {pnl_w:,.0f} ({pnl_p:+.2f}%)")
            try: send_ctx(context, msg)
            except: pass
            info["last_notified_price"]=cur; save_state()
        tg=info.get("target_price"); sp=info.get("stop_price")
        sym=m.split("-")[1]
        reached=False; reason=""
        if tg and cur>=float(tg):
            reached=True; reason=f"목표가 {fmt(tg)}"
            info["target_price"]=None
        if sp and cur<=float(sp):
            reached=True; reason = (reason+" / " if reason else "") + f"손절가 {fmt(sp)}"
            info["stop_price"]=None
        if reached:
            try: send_ctx(context, f"🎯 지정가 도달\n{pretty_sym(sym)}: 현재 {fmt(cur)}원 ({reason})")
            except: pass
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

        if action=="target":
            if step=="symbol":
                data["symbol"]=text.upper()
                kb1=ReplyKeyboardMarkup([["목표가","손절가","초기화"],["취소"]], resize_keyboard=True, one_time_keyboard=True)
                set_pending(update.effective_chat.id, "target", "type", data)
                reply(update,"설정 유형을 선택하세요.", kb=kb1); return
            if step=="type":
                if text not in ["목표가","손절가","초기화"]:
                    kb1=ReplyKeyboardMarkup([["목표가","손절가","초기화"],["취소"]], resize_keyboard=True, one_time_keyboard=True)
                    reply(update,"‘목표가/손절가/초기화’ 중 선택하세요.", kb=kb1); return
                data["which"]=text
                if text=="초기화":
                    set_target_stop(update, data["symbol"], "초기화", "direct", 0)
                    clear_pending(update.effective_chat.id); return
                kb2=ReplyKeyboardMarkup([["직접입력","현재가±%","평단가±%"],["취소"]], resize_keyboard=True, one_time_keyboard=True)
                set_pending(update.effective_chat.id, "target", "mode", data)
                reply(update,"입력 방식을 선택하세요.", kb=kb2); return
            if step=="mode":
                if text not in ["직접입력","현재가±%","평단가±%"]:
                    kb2=ReplyKeyboardMarkup([["직접입력","현재가±%","평단가±%"],["취소"]], resize_keyboard=True, one_time_keyboard=True)
                    reply(update,"‘직접입력/현재가±%/평단가±%’ 중 선택하세요.", kb=kb2); return
                data["mode"]=("direct" if text=="직접입력" else "cur_pct" if text=="현재가±%" else "avg_pct")
                set_pending(update.effective_chat.id, "target", "value", data)
                reply(update, ("가격(원)을 입력하세요." if data["mode"]=="direct" else "변화율(%)을 입력하세요. 예: 5 또는 0.5"), kb=CANCEL_KB); return
            if step=="value":
                v=text.replace("%","").replace(",","")
                try: float(v)
                except: reply(update,"숫자만 입력하세요.", kb=CANCEL_KB); return
                set_target_stop(update, data["symbol"], data["which"], data["mode"], float(v))
                clear_pending(update.effective_chat.id); return

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
            try: float(v)
            except: reply(update,"숫자만 입력하세요. 취소는 ‘취소’", kb=CANCEL_KB); return
            symbol=data.get("symbol","")
            if action=="setavg": act_setavg(update,symbol,v)
            elif action=="setqty": act_setqty(update,symbol,v)
            elif action=="setrate_coin": act_setrate_symbol(update,symbol,v)
            clear_pending(update.effective_chat.id); return

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
        set_pending(update.effective_chat.id, "target", "symbol", {})
        reply(update, "코인을 선택하거나 직접 입력하세요.", kb=coin_kb()); return
    reply(update, HELP)

# ========== MAIN ==========
def main():
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
