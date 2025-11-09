import requests, urllib.parse

NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# 노출감시에 실제 사용하신 키워드 그대로 넣으시면 됩니다.
keyword = "강남 애견카페"
q = urllib.parse.quote(keyword)
url = f"https://search.naver.com/search.naver?where=place&sm=tab_jum&query={q}"

print("GET", url)
r = requests.get(url, headers=NAVER_HEADERS, timeout=10)
print("status:", r.status_code, "length:", len(r.text))

with open("naver_rank_debug.html", "w", encoding="utf-8") as f:
    f.write(r.text)

print("saved: naver_rank_debug.html")
