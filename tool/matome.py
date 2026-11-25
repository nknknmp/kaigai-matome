"""Reddit の人気スレを Arctic Shift から集め、claude -p で日本語のまとめ記事にする。

出力: data/articles/<日付>-<投稿ID>.json （1記事1ファイル。原文と訳文の両方を持つ）
ページとフィードは build.py が作る。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
# GitHub Actions では公開リポジトリの data/ を記事置き場にする（MATOME_ARTICLES で指定）
ARTICLES = Path(os.environ.get("MATOME_ARTICLES") or ROOT / "data" / "articles")
API = "https://arctic-shift.photon-reddit.com/api"
UA = "kaigai-matome-feed/0.1 (personal reader)"

sys.stdout.reconfigure(encoding="utf-8")


def log(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


# ---------- Arctic Shift ----------

class Timeout(Exception):
    pass


def api_get(path, **params):
    """API を呼ぶ。混雑・タイムアウト（422/429）は待って2回まで再試行し、だめなら Timeout。"""
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.load(r)
            time.sleep(1.5)  # 無料サービスなので間を空ける
            return data
        except urllib.error.HTTPError as e:
            if e.code not in (422, 429):
                raise
            wait = int(e.headers.get("X-RateLimit-Reset") or 10)
            time.sleep(min(max(wait, 5), 60) + attempt * 5)
        except (urllib.error.URLError, TimeoutError):
            time.sleep(10)
    raise Timeout(path)


SUB_TIME_LIMIT = 180  # 1つの板にかける秒数の上限（AskReddit は15分以上かかったので外した）


def fetch_posts(sub, after, before, depth=0, started=None):
    """after〜before に立ったスレを全部取る。重い板は時間帯を半分に割って取り直す。"""
    started = started or time.time()
    fields = "id,title,score,num_comments,created_utc,over_18,subreddit,selftext,url,post_hint,author"
    posts, cur = [], before
    try:
        while True:
            d = api_get("/posts/search", subreddit=sub, after=after, before=cur,
                        limit=100, sort="desc", fields=fields)["data"]
            posts += d
            if time.time() - started > SUB_TIME_LIMIT:
                log(f"  r/{sub}: 時間切れ。取れた分だけ使う")
                return posts
            if len(d) < 100:
                return posts
            cur = int(min(p["created_utc"] for p in d))
    except Timeout:
        if depth >= 3 or time.time() - started > SUB_TIME_LIMIT:
            log(f"  r/{sub}: 混んでいて取れなかった部分を飛ばした")
            return posts
        mid = (after + cur) // 2
        return posts + fetch_posts(sub, mid, cur, depth + 1, started) + fetch_posts(sub, after, mid, depth + 1, started)


def fetch_thread(post, cfg):
    """スレの上位コメントと、その下の上位返信を取る。"""
    tree = api_get("/comments/tree", link_id=post["id"], limit=400,
                   start_breadth=40, start_depth=3)["data"]

    def usable(n):
        if n.get("kind") != "t1":
            return False
        c = n["data"]
        return (c.get("body") not in (None, "[deleted]", "[removed]")
                and c.get("author") != "AutoModerator" and not c.get("stickied"))

    def children(n):
        rep = n["data"].get("replies")
        if not isinstance(rep, dict):
            return []
        return [x for x in rep["data"]["children"] if usable(x)]

    def pack(c, op_author):
        return {"id": c["id"], "author": c["author"], "score": c["score"],
                "is_op": c["author"] == op_author, "body": c["body"][:1200]}

    tops = sorted([n for n in tree if usable(n)], key=lambda n: n["data"]["score"], reverse=True)
    comments = []
    for n in tops[:cfg["comments_per_article"]]:
        item = pack(n["data"], post["author"])
        reps = sorted(children(n), key=lambda x: x["data"]["score"], reverse=True)
        item["replies"] = [pack(r["data"], post["author"]) for r in reps[:cfg["replies_per_comment"]]
                           if r["data"]["score"] >= 5]
        comments.append(item)
    return comments


# ---------- Claude ----------

NOTE_RULE = """- 解説: 英語の勉強に使う読者のために、原文に出てくるスラング・熟語・ネット略語・面白い言い回しを、1つの書き込みにつき0〜3個。
  1行に1つ「表現 … 意味（ひとこと説明）」の形で。例: 「It's giving ... … 〜っぽい雰囲気（Z世代のスラング）」
  中学英語で分かる普通の単語は取り上げない。取り上げるものが無ければ書かない。
"""

PROMPT = """あなたは、日本の「海外の反応」系まとめブログの管理人です。
下の JSON は英語圏の Reddit のスレ（スレ主の投稿と上位コメント）です。日本人がサクサク楽しく読める記事用に翻訳してください。
「海外の人たちがこう盛り上がっている」というノリが伝わるのが大事。お堅い翻訳記事にはしない。

ルール:
- タイトル: 海外の反応ブログ定番の形「短くて感情が伝わる見出し　「コメントから選んだひと言」」。
  前半は25字くらいまで。ニュース見出しのような硬い言い方（「〜が撮影される」「〜と判明」「〜を発表」「〜した話」）は避ける。
  「〜すぎる」「〜しちゃった」「まさかの〜」「〜に驚きの声」「〜が話題に」など、気持ちが動く言い方にする。数字や細かい事実は本文に回す。
  後ろの「」は（前に「海外」は付けない）吹き替え調の短いひと言（「なんてこった！」「彼は天才だよ」「これは反則だろ」など）。
  例: 「深海で巨大ザメと目が合っちゃった映像が怖すぎる　「夢に出てきそうだよ」」
      「勝手にDoorDashに載せられたピザ屋、逆手に取って荒稼ぎ　「彼は天才だよ」」
      「97歳のおじいちゃんが家族のために焼いたデザートが尊い　「泣かせないでくれよ」」
  【悲報】【朗報】のような括弧の煽りや「www」は使わない。釣りすぎず、内容と合っていること。
- 背景: 日本人が前提を知らないと分かりにくい場合だけ、1〜2文で（アメリカの制度、人名、ネットミームなど）。不要なら「なし」。
- タグ: ジャンルを1〜2個、読点区切り（例: ニュース, 雑学, 仕事, 恋愛, ゲーム, 映画, スポーツ, 政治, テック, 生活, 動物, 歴史, 笑い）。
- 本文: スレ主の本文の翻訳。本文が空なら「なし」。
- コメント: 各コメントの翻訳。海外の反応ブログ定番の「吹き替え調」にする。洋画の吹き替えや翻訳小説のような、少し芝居がかった親しげな口調。
  例: 「なぜ彼はそんなことをしたんだい？」「これは最高だよ！」「彼は天才さ」「冗談だろ？」「勘弁してくれよ」「信じられないね」
      「オーマイガー」「なんてこった」「僕は〜だと思うね」「君は〜かい？」「〜だぜ」「〜なのさ」「〜だわ」（女性っぽい書き手なら）
  一人称は「僕」「俺」「私」を書き手のキャラで使い分ける。he / she は「彼」「彼女」と訳してよい。
  「です・ます」調は使わない（原文が本当に丁寧な場合だけ例外）。長い文は短く区切って読みやすくする。
  「ワイ」「草」「〜ンゴ」「〜やで」などの 5ch 語や関西弁、「マジ卍」のような今どきの若者言葉は使わない。lol / lmao は「（笑）」程度で。
  意味は変えない。皮肉や言葉遊びは日本語で通じるように意訳してよい。URL はそのまま残す。
""" + NOTE_RULE + """
先に判定: 読者は、少しでも「いい話でしょ？」と押し付けてくる、無理やり感のあるほっこり話が嫌い。
次に当てはまるスレは、他は何も書かずに「=== 除外」の1行だけを出力する。
- 人間の「いい話」を見せるための投稿: お年寄りや家族の微笑ましいエピソード、善行・親切、記念日、手作り、再会、回復報告、感謝の話など
- 「泣ける」「尊い」「人類捨てたもんじゃない」と感動させにきている投稿や、いいねを稼ぐための演出っぽい投稿
- コメントの大半が「素敵」「優しい」「泣ける」「おめでとう」「守りたい」などの称賛・祝福
コメントに軽い冗談が混じっていても、話そのものが上のタイプなら除外する。
ただし、動物や赤ちゃんの写真・動画（見たまま可愛い、面白い）は除外しない。
残すのは、動物・赤ちゃん、意外な事実、ニュース、議論や意見の対立、ツッコミどころ、変な出来事、驚き、怒り、考えさせられる話。

除外しない場合は、次の形のテキストだけを出力してください（前後に説明を付けない。コメントは JSON にある id すべてについて書く）:
=== タイトル
（見出し）
=== 背景
（背景 または なし）
=== タグ
（タグ）
=== 本文
（本文の訳 または なし）
=== n:title
（スレのタイトルと本文の英語の解説。無ければこの見出しごと省く）
=== c:<コメントid>
（そのコメントの訳）
=== n:<コメントid>
（そのコメントの英語の解説。無ければこの見出しごと省く）
=== c:<次のコメントid>
（訳）

スレ:
"""

NOTES_ONLY_PROMPT = """下の JSON は英語圏の Reddit のスレ（スレ主の投稿と上位コメント）です。
英語を勉強している日本人のために、解説だけを書いてください（翻訳は不要）。
""" + NOTE_RULE + """
次の形のテキストだけを出力してください（前後に説明を付けない。解説の無いものは見出しごと省く）:
=== n:title
（スレのタイトルと本文の解説）
=== n:<コメントid>
（そのコメントの解説）

スレ:
"""


def parse_sections(text):
    """「=== 見出し」で区切られた返答を辞書にする。JSON だと訳文中の " で壊れやすかったのでこの形にした。"""
    sec, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^===\s*(.+?)\s*$", line)
        if m:
            cur = m.group(1)
            sec[cur] = []
        elif cur is not None:
            sec[cur].append(line)
    sec = {k: "\n".join(v).strip() for k, v in sec.items()}
    if "除外" in sec:
        return {"skip": True}
    none =lambda v: "" if v in ("なし", "（なし）") else v
    return {"title": sec.get("タイトル", ""), "context": none(sec.get("背景", "")),
            "tags": [t.strip() for t in re.split(r"[、,，]", sec.get("タグ", "")) if t.strip()],
            "body": none(sec.get("本文", "")),
            "comments": {k[2:].strip(): v for k, v in sec.items() if k.startswith("c:")},
            "notes": {k[2:].strip(): [x.strip().lstrip("-・* ").strip() for x in v.splitlines()
                                      if x.strip() and none(x.strip())]
                      for k, v in sec.items() if k.startswith("n:")}}


def ask_claude(thread, model):
    """返答の形が崩れていたら、もう1回だけ頼む。"""
    try:
        return _ask_claude(thread, model)
    except (ValueError, RuntimeError) as e:
        log(f"  やり直し（{str(e)[:60]}）")
        return _ask_claude(thread, model)


def thread_src(thread):
    return json.dumps({"subreddit": thread["subreddit"], "title": thread["title"], "body": thread["selftext"],
                       "comments": [{"id": c["id"], "text": c["body"],
                                     "replies": [{"id": r["id"], "text": r["body"]} for r in c["replies"]]}
                                    for c in thread["comments"]]}, ensure_ascii=False)


def run_claude(prompt, model):
    # C:\dev の CLAUDE.md（作業ルール）を読ませないよう、作業フォルダを一時フォルダにする
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(["claude", "-p", "--model", model, "--output-format", "text"],
                           input=prompt, capture_output=True, text=True, encoding="utf-8",
                           cwd=tmp, timeout=600, shell=sys.platform == "win32")
    if r.returncode != 0:
        raise RuntimeError(f"claude -p failed: {r.stderr[:300] or r.stdout[:300]}")
    return r.stdout


def _ask_claude(thread, model):
    ja = parse_sections(run_claude(PROMPT + thread_src(thread), model))
    if ja.get("skip"):
        return ja
    if not ja["title"]:
        raise ValueError("返答の形が違う（タイトルが無い）")
    ids = {c["id"] for c in thread["comments"]} | {x["id"] for c in thread["comments"] for x in c["replies"]}
    missing = ids - set(ja["comments"])
    if len(missing) > len(ids) // 2:
        raise ValueError(f"訳が足りない（{len(missing)}/{len(ids)} 件抜け）")
    return ja


# ---------- 同じ話題の重複よけ ----------

STOP = set("the a an and or of to in on at for with from by is are was were be been this that these those it its "
           "my your his her their our after before over under about into than then just very what when who how why "
           "til today learned".split())


def words(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2 and w not in STOP}


def same_topic(t1, t2):
    """英語タイトルの大事な単語が6割以上かぶっていたら同じ話題とみなす（同じ動画が別の板に投稿される対策）。"""
    a, b = words(t1), words(t2)
    if min(len(a), len(b)) < 4:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.6


# ---------- main ----------

def image_of(p):
    url = p.get("url") or ""
    if re.search(r"\.(jpe?g|png|gif|webp)$", url, re.I) or p.get("post_hint") == "image":
        return url
    return ""


def add_notes(cfg):
    """解説（英語の勉強用）が無い記事に、解説だけを後から付ける。python matome.py --notes"""
    for f in sorted(ARTICLES.glob("*.json")):
        a = json.loads(f.read_text(encoding="utf-8"))
        if "notes" in a["ja"]:
            continue
        try:
            a["ja"]["notes"] = parse_sections(run_claude(NOTES_ONLY_PROMPT + thread_src(a), cfg["claude_model"]))["notes"]
            f.write_text(json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
            log(f"  ✔ 解説 {sum(map(len, a['ja']['notes'].values()))} 個: {a['ja']['title'][:40]}")
        except Exception as e:
            log(f"  {a['id']}: 失敗 {e}")


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if sys.argv[1:] == ["--notes"]:
        add_notes(cfg)
        return
    if sys.argv[1:] == ["--retranslate"]:  # 訳し方を変えたとき、今ある記事を全部訳し直す
        for f in sorted(ARTICLES.glob("*.json")):
            a = json.loads(f.read_text(encoding="utf-8"))
            try:
                ja = ask_claude(a, cfg["claude_model"])
                if ja.get("skip"):
                    log(f"  ただのいい話と判定（今の訳のまま残す）: {a['ja']['title'][:50]}")
                    continue
                a["ja"] = ja
                f.write_text(json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
                log(f"  ✔ 解説 {sum(map(len, a['ja']['notes'].values()))} 個: {a['ja']['title'][:50]}")
            except Exception as e:
                log(f"  {a['id']}: 失敗 {e}")
        return
    if len(sys.argv) > 1:  # 例: python matome.py 3 （試しに3本だけ作る）
        cfg["articles_per_run"] = int(sys.argv[1])
    ARTICLES.mkdir(parents=True, exist_ok=True)
    done = {f.stem.split("-", 1)[1] for f in ARTICLES.glob("*.json")}

    now = int(time.time())
    after = now - cfg["window_start_hours_ago"] * 3600
    before = now - cfg["window_end_hours_ago"] * 3600

    candidates = []
    for sub in cfg["subreddits"]:
        try:
            posts = fetch_posts(sub, after, before)
        except Exception as e:  # 1つの板の失敗で全体を止めない
            log(f"r/{sub}: 取得失敗 {e}")
            continue
        ok = [p for p in posts if not p.get("over_18")
              and p["score"] >= cfg["min_score"] and p["id"] not in done]
        ok.sort(key=lambda p: p["score"], reverse=True)
        candidates += ok[:cfg["max_per_subreddit"]]
        log(f"r/{sub}: {len(posts)} スレ中 候補 {len(ok[:cfg['max_per_subreddit']])}")

    candidates.sort(key=lambda p: p["score"], reverse=True)
    # 過去の記事・今回選んだものと同じ話題は飛ばす
    seen_titles = [json.loads(f.read_text(encoding="utf-8"))["title"] for f in ARTICLES.glob("*.json")]
    # ただのいい話として除外したスレ（翌日にまた判定して枠を使わないよう記録する）
    skipped_file = ARTICLES.parent / "skipped.json"
    skipped = set(json.loads(skipped_file.read_text(encoding="utf-8"))) if skipped_file.exists() else set()
    log(f"候補 {len(candidates)} 本から {cfg['articles_per_run']} 本作る")

    made = 0
    for p in candidates:
        if made >= cfg["articles_per_run"]:
            break
        if p["id"] in skipped:
            continue
        if any(same_topic(p["title"], t) for t in seen_titles):
            log(f"  同じ話題なので飛ばす: {p['title'][:60]}")
            continue
        seen_titles.append(p["title"])
        try:
            thread = {"id": p["id"], "subreddit": p["subreddit"], "title": p["title"],
                      "selftext": (p.get("selftext") or "")[:3000], "author": p["author"],
                      "score": p["score"], "num_comments": p["num_comments"],
                      "created_utc": int(p["created_utc"]), "image": image_of(p),
                      "video": "video" in (p.get("post_hint") or "") or "v.redd.it" in (p.get("url") or ""),
                      "link": "" if image_of(p) or re.search(r"reddit\.com|redd\.it", p.get("url") or "") else p.get("url", ""),
                      "permalink": f"https://www.reddit.com/r/{p['subreddit']}/comments/{p['id']}/"}
            if thread["selftext"] in ("[deleted]", "[removed]"):
                thread["selftext"] = ""
            thread["comments"] = fetch_thread(p, cfg)
            if not thread["comments"]:
                log(f"  {p['id']}: コメントが取れないので飛ばす")
                continue
            ja = ask_claude(thread, cfg["claude_model"])
            if ja.get("skip"):
                log(f"  ただのいい話なので除外: {p['title'][:60]}")
                skipped.add(p["id"])
                skipped_file.write_text(json.dumps(sorted(skipped)), encoding="utf-8")
                continue
            article = {**thread, "ja": ja,
                       "published": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            day = datetime.now().strftime("%Y%m%d")
            (ARTICLES / f"{day}-{p['id']}.json").write_text(
                json.dumps(article, ensure_ascii=False, indent=1), encoding="utf-8")
            made += 1
            log(f"  ✔ {ja.get('title', '')[:50]}")
        except Exception as e:
            log(f"  {p['id']}: 失敗 {e}")
    log(f"完了: {made} 本作成")


if __name__ == "__main__":
    main()
