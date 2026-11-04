# Blog

Each post is one Markdown file in `posts/`. The file name is the address: `posts/two-way-sync.md` becomes `/blog/two-way-sync/`.

```
---
title: The title
description: One sentence for the list page and search results. 170 characters at most.
date: 2026-11-05
updated: 2026-11-20
status: published
---
The post. Headings start at ##.
```

`status` is `draft` or `published`. `updated` is optional. A published post with a future date stays hidden until a build on or after that date.

Run `python3 tools/build_blog.py`, then commit `blog/` and what it changed in `site/` and `docs-site/`. It rebuilds `site/blog/`, the RSS feed, both sitemaps and `llms.txt`, so a post needs no other edits.

Only write what the docs or the code can back up: no made-up numbers, customers or quotes.
