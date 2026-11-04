"""Blog and search file tests: python3 -m unittest test_blog"""
import datetime, glob, importlib.util, os, re, shutil, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("build_blog", os.path.join(HERE, "tools", "build_blog.py"))
bb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bb)
TODAY = datetime.date(2026, 11, 4)


def text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def post(status="published", date="2026-11-03", title="A post", extra="", body="Hello."):
    return f"---\ntitle: {title}\ndescription: What it says.\ndate: {date}\nstatus: {status}\n{extra}---\n{body}\n"


class Build(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t)
        self.site, self.docs, self.posts = (os.path.join(self.t, n) for n in ("site", "docs", "posts"))
        for d in (self.site, self.docs, self.posts):
            os.makedirs(d)
        open(os.path.join(self.docs, "index.html"), "w").close()
        p = mock.patch.multiple(bb, SITE=self.site, DOCS=self.docs, POSTS=self.posts)
        p.start()
        self.addCleanup(p.stop)

    def add(self, name, text, folder=None):
        with open(os.path.join(folder or self.posts, name + ".md"), "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, *path):
        return text(os.path.join(self.site, *path))

    def test_published_post_reaches_page_list_sitemap_feed_and_llms(self):
        self.add("first-post", post(extra="updated: 2026-11-04\n"))
        bb.build(TODAY)
        self.assertIn("<h1 itemprop=\"headline\">A post</h1>", self.read("blog", "first-post", "index.html"))
        self.assertIn("/blog/first-post/", self.read("blog", "index.html"))
        self.assertIn("<loc>https://getcalsync.app/blog/first-post/</loc><lastmod>2026-11-04</lastmod>", self.read("sitemap.xml"))
        self.assertIn("https://getcalsync.app/blog/first-post/", self.read("blog", "feed.xml"))
        self.assertIn("/blog/first-post/", self.read("llms.txt"))

    def test_draft_and_future_posts_stay_out_everywhere(self):
        self.add("a-draft", post("draft", title="Secret draft"))
        self.add("for-later", post(date="2026-11-05", title="Later"))
        live, _ = bb.build(TODAY)
        self.assertEqual(live, [])
        self.assertFalse(os.path.exists(os.path.join(self.site, "blog", "a-draft")))
        self.assertFalse(os.path.exists(os.path.join(self.site, "blog", "for-later")))
        for f in (("sitemap.xml",), ("llms.txt",), ("blog", "feed.xml"), ("blog", "index.html")):
            self.assertNotIn("Secret draft", self.read(*f))
            self.assertNotIn("for-later", self.read(*f))

    def test_a_scheduled_post_appears_on_its_day(self):
        self.add("for-later", post(date="2026-11-05"))
        live, _ = bb.build(datetime.date(2026, 11, 5))
        self.assertEqual([p["slug"] for p in live], ["for-later"])

    def test_a_removed_post_loses_its_page(self):
        self.add("gone", post())
        bb.build(TODAY)
        os.remove(os.path.join(self.posts, "gone.md"))
        bb.build(TODAY)
        self.assertFalse(os.path.exists(os.path.join(self.site, "blog", "gone")))
        self.assertNotIn("gone", self.read("sitemap.xml"))

    def test_draft_previews_are_noindex_and_outside_the_sitemap(self):
        d = os.path.join(self.t, "drafts")
        os.makedirs(d)
        self.add("look-at-me", post("draft"), d)
        bb.build(TODAY, d)
        page = self.read("blog", "drafts", "look-at-me", "index.html")
        self.assertIn('<meta name="robots" content="noindex">', page)
        self.assertNotIn('rel="canonical"', page)
        self.assertNotIn("look-at-me", self.read("sitemap.xml"))
        self.assertNotIn("look-at-me", self.read("blog", "index.html"))

    def test_html_in_a_post_is_escaped(self):
        self.add("x", post(title="<b>Tag</b>", body="<script>alert(1)</script> and `<code>`\n\n```\n<pre>\n```"))
        bb.build(TODAY)
        page = self.read("blog", "x", "index.html")
        self.assertNotIn("<script", page)
        self.assertNotIn("<b>Tag", page)
        self.assertIn("&lt;script&gt;", page)

    def test_bad_posts_stop_the_build(self):
        for name, text in (("no-title", "---\ndescription: d\ndate: 2026-11-01\nstatus: draft\n---\nx"),
                           ("bad-date", post(date="soon")), ("bad-status", post("maybe")),
                           ("Bad_Name", post()), ("drafts", post()), ("long", post().replace("What it says.", "x" * 171))):
            self.add(name, text)
            with self.assertRaises(SystemExit, msg=name):
                bb.build(TODAY)
            os.remove(os.path.join(self.posts, name + ".md"))


class Markdown(unittest.TestCase):
    def test_blocks(self):
        out = bb.markdown("# Dropped\n\n## Two\n\n### Three\n\nA **b** *i* `c` [l](/x/).\n\n- one\n- two\n\n1. a\n2. b\n\n> q\n\n```\nx *y*\n```")
        for want in ("<h2>Two</h2>", "<h3>Three</h3>", "<strong>b</strong>", "<em>i</em>", "<code>c</code>", '<a href="/x/">l</a>',
                     "<ul><li>one</li><li>two</li></ul>", "<ol><li>a</li><li>b</li></ol>", "<blockquote><p>q</p></blockquote>",
                     "<pre><code>x *y*</code></pre>"):
            self.assertIn(want, out)
        self.assertNotIn("Dropped", out)


class Committed(unittest.TestCase):
    def pages(self, site):
        return [p for p in glob.glob(os.path.join(HERE, site, "**", "*.html"), recursive=True)
                if "drafts" not in p and not p.endswith("404.html")]

    def test_every_page_has_its_own_title_description_and_canonical(self):
        for site, base in (("site", bb.URL), ("docs-site", bb.DOCS_URL)):
            titles, descriptions = set(), set()
            for path in self.pages(site):
                s = text(path)
                rel = os.path.relpath(path, os.path.join(HERE, site)).replace(os.sep, "/")
                title = re.search(r"<title>(.+?)</title>", s)[1]
                description = re.search(r'<meta name="description" content="(.+?)">', s)[1]
                self.assertIn(f'<link rel="canonical" href="{base}/{rel[:-len("index.html")]}">', s, path)
                self.assertNotIn(title, titles, path)
                self.assertNotIn(description, descriptions, path)
                self.assertLessEqual(len(description), 170, path)
                titles.add(title)
                descriptions.add(description)

    def test_built_files_match_the_posts(self):
        posts = bb.read_posts(os.path.join(HERE, "blog", "posts"))
        sitemap = text(os.path.join(HERE, "site", "sitemap.xml"))
        for p in posts:
            if p["status"] == "published" and p["date"] <= datetime.date.today():
                self.assertIn(f'/blog/{p["slug"]}/', sitemap, "run tools/build_blog.py")

    def test_robots_names_both_sitemaps(self):
        s = text(os.path.join(HERE, "site", "robots.txt"))
        self.assertIn("Sitemap: https://getcalsync.app/sitemap.xml", s)
        self.assertIn("Sitemap: https://docs.getcalsync.app/sitemap.xml", s)


if __name__ == "__main__":
    unittest.main()
