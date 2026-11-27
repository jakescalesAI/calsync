#!/usr/bin/env python3
"""Builds the blog and the search files for getcalsync.app. Standard library only: python3 tools/build_blog.py

Reads blog/posts/*.md and writes site/blog/ (list, posts, RSS), site/sitemap.xml, site/llms.txt and
docs-site/sitemap.xml. A post appears when it has status: published and its date has come.

--drafts DIR  also writes the posts in DIR to site/blog/drafts/, for looking at on a preview server.
              That folder is ignored by git, marked noindex and in no sitemap, so a deploy never carries it.
"""
import argparse, datetime, email.utils, html, os, re, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE, DOCS, POSTS = (os.path.join(ROOT, p) for p in ("site", "docs-site", os.path.join("blog", "posts")))
URL, DOCS_URL = "https://getcalsync.app", "https://docs.getcalsync.app"
OWNER = "jakescalesAI"
NEEDED = ("title", "description", "date", "status")
esc = html.escape


# posts

def read_post(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not m:
        raise SystemExit(f"{path}: no front matter")
    post = {k.strip(): v.strip() for k, v in (line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)}
    for k in NEEDED:
        if not post.get(k):
            raise SystemExit(f"{path}: missing {k}")
    if post["status"] not in ("draft", "published"):
        raise SystemExit(f"{path}: status must be draft or published")
    if len(post["description"]) > 170:
        raise SystemExit(f"{path}: description is over 170 characters")
    try:
        post["date"] = datetime.date.fromisoformat(post["date"])
        post["updated"] = datetime.date.fromisoformat(post.get("updated") or post["date"].isoformat())
    except ValueError as e:
        raise SystemExit(f"{path}: {e}")
    post["slug"] = os.path.basename(path)[:-3]
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", post["slug"]) or post["slug"] == "drafts":
        raise SystemExit(f"{path}: bad file name for a slug")
    post["body"] = m.group(2)
    return post


def read_posts(folder):
    if not os.path.isdir(folder):
        return []
    posts = [read_post(os.path.join(folder, n)) for n in os.listdir(folder) if n.endswith(".md")]
    return sorted(posts, key=lambda p: (p["date"], p["slug"]), reverse=True)


def inline(text):
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{esc(part[1:-1], quote=False)}</code>")
            continue
        s = esc(part, quote=False)
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{esc(m[2])}">{m[1]}</a>', s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
        out.append(s)
    return "".join(out)


def markdown(text):
    """The small part of Markdown the blog uses: ## and ### headings, paragraphs, lists, quotes, code fences."""
    lines, out, i = text.strip().split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("```"):
            end = i + 1
            while end < len(lines) and not lines[end].startswith("```"):
                end += 1
            out.append(f"<pre><code>{esc(chr(10).join(lines[i + 1:end]), quote=False)}</code></pre>")
            i = end + 1
        elif m := re.match(r"(#{2,3}) (.+)", line):
            n = len(m[1])
            out.append(f"<h{n}>{inline(m[2])}</h{n}>")
            i += 1
        elif line.startswith("# "):
            i += 1                                  # the page prints the title itself
        elif re.match(r"(- |\d+\. )", line):
            tag = "ul" if line.startswith("- ") else "ol"
            mark = r"- " if tag == "ul" else r"\d+\. "
            items = []
            while i < len(lines) and (re.match(mark, lines[i]) or (lines[i].startswith("  ") and items)):
                if re.match(mark, lines[i]):
                    items.append(re.sub("^" + mark, "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
        elif line.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].startswith("> "):
                quote.append(lines[i][2:])
                i += 1
            out.append(f"<blockquote><p>{inline(' '.join(quote))}</p></blockquote>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"(#{1,3} |- |\d+\. |> |```)", lines[i]):
                para.append(lines[i])
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
    return "\n".join(out)


def nice(d):
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def minutes(body):
    return max(1, round(len(body.split()) / 220))


# pages

def page(title, description, path, main, *, noindex=False, article=None):
    canonical = URL + path
    meta = ['<meta name="robots" content="noindex">'] if noindex else [f'<link rel="canonical" href="{canonical}">']
    kind = "article" if article else "website"
    if article:
        meta += [f'<meta property="article:published_time" content="{article["date"]}">',
                 f'<meta property="article:modified_time" content="{article["updated"]}">']
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<meta name="color-scheme" content="light">
<meta name="theme-color" content="#ffffff">
{chr(10).join(meta)}
<link rel="icon" href="/favicon.ico" sizes="32x32"><link rel="icon" href="/mark-small.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="alternate" type="application/rss+xml" title="calsync blog" href="/blog/feed.xml">
<link rel="stylesheet" href="/style.css">
<meta property="og:type" content="{kind}">
<meta property="og:site_name" content="calsync">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{canonical}">
<meta property="og:image" content="{URL}/og.png">
<meta name="twitter:card" content="summary_large_image">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>

<header class="top">
<div class="wrap">
<a class="brand" href="/"><img class="glyph" src="/glyph-light.svg" alt="" width="37" height="40">calsync</a>
<nav aria-label="Main">
<a href="/#how">How it works</a>
<a href="/#careful">Safety</a>
<a href="/#compare">Compare</a>
<a href="/blog/" aria-current="page">Blog</a>
<a href="{DOCS_URL}/">Docs</a>
<a class="gh" href="https://github.com/{OWNER}/calsync">GitHub</a>
</nav>
<a class="burger" href="#menu" aria-label="Open menu"><i></i><i></i><i></i></a>
</div>
</header>
<span id="shut" hidden></span>
<nav class="menu" id="menu" aria-label="Menu">
<div class="menu-top"><a class="brand" href="/"><img class="glyph" src="/glyph-light.svg" alt="" width="37" height="40">calsync</a><a class="shut" href="#shut" aria-label="Close menu"><i></i><i></i></a></div>
<a href="/#how">How it works</a>
<a href="/#careful">Safety</a>
<a href="/#compare">Compare</a>
<a href="/blog/" aria-current="page">Blog</a>
<a href="{DOCS_URL}/">Docs</a>
<a href="https://github.com/{OWNER}/calsync">GitHub</a>
</nav>

<main id="main">
{main}
</main>

<footer class="foot">
<div class="wrap">
<a class="brand" href="/" aria-label="calsync home"><img class="glyph" src="/glyph-light.svg" alt="" width="22" height="24">calsync</a>
<nav aria-label="Footer"><a href="/blog/">Blog</a><a href="{DOCS_URL}/">Docs</a><a href="{DOCS_URL}/faq/">FAQ</a><a href="https://github.com/{OWNER}/calsync">GitHub</a></nav>
<p>MIT licence. This page has no trackers, analytics or cookies.</p>
</div>
</footer>
</body>
</html>
"""


def rows(posts, base, tag=""):
    if not posts:
        return '<p class="none">Nothing here yet. Posts show up here as they are written.</p>'
    items = []
    for p in posts:
        items.append(f'<li><a href="{base}{p["slug"]}/"><time datetime="{p["date"]}">{nice(p["date"])}</time>'
                     f'<h2>{esc(p["title"])}{tag}</h2><p>{esc(p["description"])}</p></a></li>')
    return '<ul class="posts">\n' + "\n".join(items) + "\n</ul>"


def list_page(posts):
    main = f"""<section class="wrap blog-top">
<p class="eyebrow">Blog</p>
<h1>Notes on calendars. <span class="dim">And keeping them honest.</span></h1>
</section>
<section class="wrap blog-list">
{rows(posts, "/blog/")}
</section>"""
    return page("Blog | calsync", "How calendar sync works, how to set it up, and how to keep two calendars honest without a subscription.",
                "/blog/", main)


def post_page(p, base="/blog/", noindex=False):
    main = f"""<article class="post" itemscope itemtype="https://schema.org/BlogPosting">
<header class="wrap post-head">
<a class="back" href="{base}">Back to the blog</a>
<h1 itemprop="headline">{esc(p["title"])}</h1>
<p class="lede" itemprop="description">{esc(p["description"])}</p>
<p class="byline"><time itemprop="datePublished" datetime="{p["date"]}">{nice(p["date"])}</time> · {minutes(p["body"])} min read · <span itemprop="author">calsync contributors</span><meta itemprop="dateModified" content="{p["updated"]}"></p>
</header>
<div class="wrap prose" itemprop="articleBody">
{markdown(p["body"])}
</div>
</article>
<section class="wrap post-end">
<h2>Free, open source, runs on your machine.</h2>
<div class="btns"><a class="btn primary" href="/#install">Install calsync</a><a class="btn" href="{DOCS_URL}/">Read the docs</a></div>
</section>"""
    return page(f'{p["title"]} | calsync', p["description"], f'{base}{p["slug"]}/', main, noindex=noindex, article=p)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# search files

def last_change(rel):
    """The day this file last changed in git, or None for a file git has not seen."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cs", "--", rel], cwd=ROOT, capture_output=True, text=True, check=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def sitemap(entries):
    body = "\n".join(f"  <url><loc>{u}</loc>" + (f"<lastmod>{d}</lastmod>" if d else "") + "</url>" for u, d in entries)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}\n</urlset>\n'


def docs_entries():
    entries = []
    for folder, _, names in sorted(os.walk(DOCS)):
        if "index.html" in names and "fonts" not in folder:
            rel = os.path.relpath(folder, DOCS).replace(os.sep, "/")
            path = "/" if rel == "." else f"/{rel}/"
            entries.append((DOCS_URL + path, last_change(f"docs-site{path}index.html")))
    return entries


def feed(posts):
    items = "".join(f"""
<item><title>{esc(p["title"])}</title><link>{URL}/blog/{p["slug"]}/</link><guid>{URL}/blog/{p["slug"]}/</guid>
<pubDate>{email.utils.format_datetime(datetime.datetime(p["date"].year, p["date"].month, p["date"].day, 14, tzinfo=datetime.timezone.utc))}</pubDate>
<description>{esc(p["description"])}</description></item>""" for p in posts[:20])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>calsync blog</title><link>{URL}/blog/</link>
<description>How calendar sync works, and how to set it up.</description><language>en</language>{items}
</channel></rss>
"""


def llms(posts):
    lines = ["# calsync", "", "> A free, open source, self-hosted tool that keeps Google and Microsoft 365 calendars in two-way sync.",
             "> There is no hosted version. It runs on your own machine.", "", "## Docs", ""]
    lines += [f"- [{u}]({u})" for u, _ in docs_entries()]
    if posts:
        lines += ["", "## Blog", ""] + [f'- [{p["title"]}]({URL}/blog/{p["slug"]}/): {p["description"]}' for p in posts]
    return "\n".join(lines) + "\n"


def build(today, drafts_dir=None):
    all_posts = read_posts(POSTS)
    live = [p for p in all_posts if p["status"] == "published" and p["date"] <= today]
    out = os.path.join(SITE, "blog")
    shutil.rmtree(out, ignore_errors=True)
    write(os.path.join(out, "index.html"), list_page(live))
    write(os.path.join(out, "feed.xml"), feed(live))
    for p in live:
        write(os.path.join(out, p["slug"], "index.html"), post_page(p))
    newest = max((p["updated"].isoformat() for p in live), default=None)
    entries = [(f"{URL}/", last_change("site/index.html")), (f"{URL}/blog/", newest or last_change("blog"))]
    entries += [(f'{URL}/blog/{p["slug"]}/', p["updated"].isoformat()) for p in live]
    write(os.path.join(SITE, "sitemap.xml"), sitemap(entries))
    write(os.path.join(SITE, "llms.txt"), llms(live))
    write(os.path.join(DOCS, "sitemap.xml"), sitemap(docs_entries()))
    drafts = read_posts(drafts_dir) if drafts_dir else []
    if drafts:
        base = "/blog/drafts/"
        main = f'<section class="wrap blog-top"><p class="eyebrow">Not published</p><h1>Drafts.</h1></section>\n' \
               f'<section class="wrap blog-list">{rows(drafts, base, " <span class=tag>Draft</span>")}</section>'
        write(os.path.join(out, "drafts", "index.html"), page("Drafts | calsync", "Drafts, not published.", base, main, noindex=True))
        for p in drafts:
            write(os.path.join(out, "drafts", p["slug"], "index.html"), post_page(p, base, noindex=True))
    return live, drafts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drafts", metavar="DIR")
    ap.add_argument("--today", type=datetime.date.fromisoformat, default=datetime.date.today())
    args = ap.parse_args()
    live, drafts = build(args.today, args.drafts)
    print(f"blog: {len(live)} published, {len(drafts)} draft previews")


if __name__ == "__main__":
    sys.exit(main())
