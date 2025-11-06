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
PORT = int(os.getenv("PORT","8080"))  # Render keepalive

DATA_FILE = "portfolio.json"
LOCK_FILE = "bot.lock"
UPBIT     = "https://api.upbit.com/v1"

# ========== KEEPALIVE ==========
class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode("utf-8"))
    def log_message(self, *a, **k): return

def _start_keepalive():
    if PORT<=0: return
    def _run():
        try:
            httpd = HTTPServer(("", PORT), _Ok)
            httpd.serve_forever()
        except Exception as e:
            print(f"[KEEPALIVE ERROR] {e}")
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
    return d

def save_state():
    tmp=DATA_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(state,f,ensure_ascii=False,indent=2)
    os.replace(tmp,DATA_FILE)

state=load_state()

# ========== UTIL ==========
def only_owner(update): return (not CHAT_ID) or (str(update.effective_chat.id)==CHAT_ID)
def krw_symbol(sym): return sym if "-" in sym else "KRW-"+sym.upper()
def fmt(n): 
    try: 
        x=float(n); 
        return f"{x:,.0f}" if abs(x)>=1 else f"{x:,.6f}".rstrip("0").rstrip(".")
    except: return str(n)
def get_price(market):
    r=requests.get(f"{UPBIT}/ticker", params={"markets":market}, timeout=5)
    r.raise_for_status()
    return float(r.json()[0]["trade_price"])

def send_ctx(ctx, text):
    ctx.bot.send_message(chat_id=CHAT_ID, text=text)

# ========== CHECK LOOP ==========
def check_loop(context):
    for m,info in list(state["coins"].items()):
        try: cur=get_price(m)
        except: continue
        base=info.get("last_notified_price",cur)
        th=float(info.get("threshold_pct",DEFAULT_THRESHOLD))
        delta=abs(cur-base)/base*100 if base>0 else 0
        if delta>=th:
            arrow="🔴" if cur>base else "🔵"
            sym=m.split("-")[1]
            msg=f"📈 변동 알림({th}%) {arrow}\n{sym}: {fmt(base)} → {fmt(cur)}원 ({(cur/base-1)*100:+.2f}%)"
            send_ctx(context,msg)
            info["last_notified_price"]=cur
    save_state()

# ========== MAIN ==========
def main():
    _start_keepalive()
    if not BOT_TOKEN:
        print("BOT_TOKEN 누락"); return
    up=Updater(BOT_TOKEN, use_context=True)
    try: up.bot.delete_webhook(drop_pending_updates=True)
    except: pass
    dp=up.dispatcher
    dp.add_handler(MessageHandler(Filters.text & (~Filters.command), lambda u,c: c.bot.send_message(chat_id=CHAT_ID,text="봇 정상작동중 ✅")))
    dp.add_handler(MessageHandler(Filters.command, lambda u,c: c.bot.send_message(chat_id=CHAT_ID,text="봇 정상작동중 ✅")))
    up.job_queue.run_repeating(check_loop, interval=3, first=3)
    up.start_polling()
    up.idle()

if __name__=="__main__":
    main()
