import requests, os

NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

PLACE_ID = os.getenv("NAVER_PLACE_ID", "").strip()
if not PLACE_ID:
    PLACE_ID = input("NAVER_PLACE_ID 입력: ").strip()

paths = ["", "/review", "/review/visitor"]
full = ""

for p in paths:
    url = f"https://m.place.naver.com/place/{PLACE_ID}{p}"
    r = requests.get(url, headers=NAVER_HEADERS, timeout=10)
    print(p or "/", "status:", r.status_code, "length:", len(r.text))
    if r.status_code == 200:
        full += "\n\n==== " + url + " ====\n\n" + r.text

with open("naver_review_debug.html", "w", encoding="utf-8") as f:
    f.write(full)

print("saved: naver_review_debug.html")
