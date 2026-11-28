"""data/articles/*.json から、ブログ風のページと Atom フィードを site/ に書き出す。"""
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
# GitHub Actions では公開リポジトリそのものが SITE、その data/ が記事置き場になる
ARTICLES = Path(os.environ.get("MATOME_ARTICLES") or ROOT / "data" / "articles")
SITE = Path(os.environ.get("MATOME_SITE") or ROOT / "site")
# 公開リポジトリの tool/ にコピーして GitHub Actions で動かすファイル（元はこのフォルダ）
TOOL_FILES = ["matome.py", "build.py", "config.json"]
JST = timezone(timedelta(hours=9))  # GitHub Actions は UTC で動くので、表示は日本時間に固定する

sys.stdout.reconfigure(encoding="utf-8")

CSS = """
:root{--bg:#f6f5f1;--card:#fff;--text:#222;--sub:#777;--line:#e2e0da;--accent:#c0392b;--name:#2e7d32;--quote:#f0eee8;--op:#1565c0}
@media (prefers-color-scheme:dark){:root{--bg:#17171a;--card:#202024;--text:#e6e6e6;--sub:#9a9a9a;--line:#33333a;--accent:#ff7a6b;--name:#7bc67e;--quote:#2a2a30;--op:#7ab4ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:"Hiragino Sans","Yu Gothic UI","Meiryo",sans-serif;line-height:1.75}
a{color:inherit}
header.site{background:var(--card);border-bottom:1px solid var(--line);padding:14px 16px}
header.site a{text-decoration:none;font-weight:700;font-size:1.25rem}
header.site p{margin:2px 0 0;color:var(--sub);font-size:.85rem}
main{max-width:760px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin-bottom:12px}
.card h2{font-size:1.05rem;margin:0 0 6px;line-height:1.5}
.card h2 a{text-decoration:none}
.card h2 a:hover{color:var(--accent)}
.meta{color:var(--sub);font-size:.8rem}
.tag{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:0 8px;margin-right:4px;font-size:.75rem}
article h1{font-size:1.3rem;line-height:1.5;margin:4px 0 8px}
.context{background:var(--quote);border-radius:6px;padding:8px 12px;font-size:.9rem;margin:12px 0}
.res{margin:18px 0}
.res .head{font-size:.85rem;color:var(--sub)}
.res .head b{color:var(--name)}
.res .head .op{color:#fff;background:var(--op);font-weight:700;font-size:.7rem;border-radius:4px;padding:0 5px;margin-left:4px}
.res .body{margin:2px 0 0 1em;font-size:1rem}
.res.post{border-bottom:1px solid var(--line);padding-bottom:14px}
.res .en{margin:4px 0 0 1em;font-size:.85rem;color:var(--sub);font-family:system-ui,"Segoe UI",sans-serif;line-height:1.55;border-left:3px solid var(--line);padding-left:8px}
.res ul.notes{margin:6px 0 0 1em;padding:6px 10px 6px 1.6em;background:var(--quote);border-radius:6px;font-size:.85rem;line-height:1.6}
.res ul.notes li::marker{content:"📘 "}
.reply{margin-left:1.6em;border-left:2px solid var(--line);padding-left:10px}
img.post{max-width:100%;border-radius:6px;margin:8px 0}
.src{margin-top:24px;font-size:.85rem}
.pager{text-align:center;color:var(--sub);font-size:.85rem}
"""


def load():
    arts = [json.loads(f.read_text(encoding="utf-8")) for f in ARTICLES.glob("*.json")]
    arts.sort(key=lambda a: a["published"], reverse=True)
    return arts


def text_html(s):
    """訳文をそのまま HTML に。改行と URL だけ生かす。"""
    s = html.escape(s or "")
    s = re.sub(r"(https?://[^\s<&]+)", r'<a href="\1" rel="noopener">\1</a>', s)
    return s.replace("\n", "<br>")


def article_body(a, inline=False):
    """記事本文（ページとフィードで共通）。inline=True はフィード用に装飾を style 属性で入れる。"""
    ja = a["ja"]
    tr = ja.get("comments", {})
    out = []
    if ja.get("context"):
        st = ' style="background:#f0eee8;padding:8px 12px"' if inline else ''
        out.append(f'<p class="context"{st}>💡 {text_html(ja["context"])}</p>')
    if a.get("image"):
        out.append(f'<p><img class="post" src="{html.escape(a["image"])}" alt=""></p>')
    if a.get("video"):
        out.append(f'<p>🎥 <a href="{html.escape(a["permalink"])}" rel="noopener">動画は元スレで見られます</a></p>')
    if a.get("link"):
        out.append(f'<p>🔗 <a href="{html.escape(a["link"])}" rel="noopener">{html.escape(a["link"][:80])}</a></p>')

    notes = ja.get("notes", {})

    def res(head, ja_text, en_text, note_lines, cls=""):
        """1書き込み分: 訳 → 原文 → 解説。フィードは CSS が効かないことが多いので inline で色を付ける。"""
        st_res = ' style="margin-left:1.5em;border-left:2px solid #ddd;padding-left:10px"' if (inline and cls == "reply") else ''
        st_en = ' style="color:#777;font-size:0.9em;border-left:3px solid #ddd;padding-left:8px"' if inline else ''
        st_nt = ' style="background:#f0eee8;font-size:0.9em"' if inline else ''
        h = [f'<div class="res {cls}"{st_res}><div class="head">{head}</div>',
             f'<div class="body">{text_html(ja_text)}</div>']
        if en_text and en_text.strip() != (ja_text or "").strip():
            h.append(f'<div class="en" lang="en"{st_en}>{text_html(en_text)}</div>')
        if note_lines:
            h.append(f'<ul class="notes"{st_nt}>' + "".join(
                f"<li>{'📘 ' if inline else ''}{text_html(x)}</li>" for x in note_lines) + "</ul>")
        h.append("</div>")
        return "".join(h)

    # 名前は Reddit と同じ u/ユーザー名。スレ主には OP の印（5ch 風の「名無しさん」はやめた）
    st_op = ' style="color:#1565c0;font-weight:bold"' if inline else ''
    st_nm = ' style="color:#2e7d32;font-weight:bold"' if inline else ''

    def name(author, is_op, score=None):
        badge = f' <span class="op"{st_op}>OP</span>' if is_op else ''
        pts = f' ・ ⬆{score:,}' if score is not None else ''
        return f'<b{st_nm}>u/{html.escape(author or "[deleted]")}</b>{badge}{pts}'

    body1 = ja.get("body") or ""
    en1 = a["title"] + (("\n\n" + a["selftext"]) if a.get("selftext") else "")
    out.append(res(name(a.get("author"), True, a["score"]), body1 or ja.get("title", ""),
                   en1, notes.get("title", []), "post"))
    for c in a["comments"]:
        out.append(res(name(c["author"], c["is_op"], c["score"]), tr.get(c["id"], c["body"]),
                       c["body"], notes.get(c["id"], [])))
        for r in c["replies"]:
            out.append(res(name(r["author"], r["is_op"], r["score"]), tr.get(r["id"], r["body"]),
                           r["body"], notes.get(r["id"], []), "reply"))
    out.append(f'<p class="src">元スレ: <a href="{html.escape(a["permalink"])}" rel="noopener">r/{html.escape(a["subreddit"])}'
               f'</a>（⬆{a["score"]:,} ・ 💬{a["num_comments"]:,}）</p>')
    return "\n".join(out)


def page(title, body, cfg, depth=0):
    up = "../" * depth
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{html.escape(title)}</title>
<link rel="alternate" type="application/atom+xml" title="{html.escape(cfg['site_title'])}" href="{up}feed.xml">
<style>{CSS}</style></head><body>
<header class="site"><a href="{up}index.html">{html.escape(cfg['site_title'])}</a><p>{html.escape(cfg['site_description'])}</p></header>
<main>{body}</main></body></html>"""


def meta_line(a):
    tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in a["ja"].get("tags", []))
    d = datetime.fromisoformat(a["published"]).astimezone(JST).strftime("%Y/%m/%d %H:%M")
    return f'<div class="meta">{tags} r/{html.escape(a["subreddit"])} ・ ⬆{a["score"]:,} ・ {d}</div>'


def on_actions():
    """GitHub Actions（公開リポジトリの中の tool/ から動いている）なら True。"""
    return ARTICLES.resolve() == (SITE / "data").resolve()


def import_from_site():
    """GitHub Actions が作った記事（site/data/）を、この PC の data/articles/ に取り込む。"""
    ARTICLES.mkdir(parents=True, exist_ok=True)
    for f in (SITE / "data").glob("*.json"):
        if not (ARTICLES / f.name).exists():
            shutil.copy2(f, ARTICLES / f.name)
    if (SITE / "skipped.json").exists() and (ARTICLES.parent / "skipped.json").exists():
        both = set(json.loads((SITE / "skipped.json").read_text(encoding="utf-8")))
        both |= set(json.loads((ARTICLES.parent / "skipped.json").read_text(encoding="utf-8")))
        (ARTICLES.parent / "skipped.json").write_text(json.dumps(sorted(both)), encoding="utf-8")
    elif (SITE / "skipped.json").exists():
        shutil.copy2(SITE / "skipped.json", ARTICLES.parent / "skipped.json")


def export_to_site():
    """記事の元データ・除外リスト・プログラムを公開リポジトリへ。
    元データはバックアップ兼「引っ越し用の元データ」。プログラムは GitHub Actions が毎朝動かす。"""
    (SITE / "data").mkdir(exist_ok=True)
    for f in ARTICLES.glob("*.json"):
        shutil.copy2(f, SITE / "data" / f.name)
    if (ARTICLES.parent / "skipped.json").exists():
        shutil.copy2(ARTICLES.parent / "skipped.json", SITE / "skipped.json")
    (SITE / "tool").mkdir(exist_ok=True)
    for name in TOOL_FILES:
        shutil.copy2(ROOT / name, SITE / "tool" / name)
    (SITE / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "daily.yml", SITE / ".github" / "workflows" / "daily.yml")
    # スマホの HTTP Shortcuts に貼るスクリプト。スマホのブラウザで開いてコピーできるよう公開する
    if (ROOT / "phone_shortcut.js").exists():
        shutil.copy2(ROOT / "phone_shortcut.js", SITE / "phone-script.txt")


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if not on_actions() and (SITE / "data").exists():
        import_from_site()
    arts = load()
    if SITE.exists():
        shutil.rmtree(SITE / "posts", ignore_errors=True)
    (SITE / "posts").mkdir(parents=True, exist_ok=True)

    for a in arts:
        body = (f'<article class="card"><h1>{html.escape(a["ja"]["title"])}</h1>{meta_line(a)}'
                f'{article_body(a)}</article><p class="pager"><a href="../index.html">← 記事一覧へ</a></p>')
        (SITE / "posts" / f'{a["id"]}.html').write_text(page(a["ja"]["title"], body, cfg, 1), encoding="utf-8")

    items = "".join(
        f'<div class="card"><h2><a href="posts/{a["id"]}.html">{html.escape(a["ja"]["title"])}</a></h2>{meta_line(a)}</div>'
        for a in arts)
    (SITE / "index.html").write_text(page(cfg["site_title"], items or "<p>まだ記事がありません</p>", cfg), encoding="utf-8")

    # Atom フィード（全文入り）
    base = cfg["site_url"].rstrip("/") + "/" if cfg["site_url"] else ""
    entries = []
    for a in arts[:cfg["feed_items"]]:
        link = f'{base}posts/{a["id"]}.html'
        tags = "".join(f'<category term="{html.escape(t)}"/>' for t in a["ja"].get("tags", []))
        entries.append(f"""<entry>
<title>{html.escape(a["ja"]["title"])}</title>
<link href="{html.escape(link)}"/>
<id>urn:reddit:{a["id"]}</id>
<updated>{a["published"]}</updated>
{tags}
<content type="html">{html.escape(article_body(a, inline=True))}</content>
</entry>""")
    updated = arts[0]["published"] if arts else datetime.now(timezone.utc).isoformat(timespec="seconds")
    feed = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>{html.escape(cfg["site_title"])}</title>
<subtitle>{html.escape(cfg["site_description"])}</subtitle>
<link href="{html.escape(base or "./")}"/>
<link rel="self" href="{html.escape(base + "feed.xml")}"/>
<id>{html.escape(base or "urn:kaigai-matome-feed")}</id>
<updated>{updated}</updated>
<author><name>{html.escape(cfg["site_title"])}</name></author>
{"".join(entries)}
</feed>"""
    (SITE / "feed.xml").write_text(feed, encoding="utf-8")

    (SITE / ".nojekyll").touch()  # GitHub Pages に余計な変換をさせない
    if not on_actions():
        export_to_site()
    print(f"site/ に {len(arts)} 記事を書き出しました（フィードは {min(len(arts), cfg['feed_items'])} 件）")
    if not cfg["site_url"]:
        print("※ config.json の site_url が空なので、フィード内のリンクは相対パスです（公開後に設定）")


if __name__ == "__main__":
    main()
