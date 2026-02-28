# Blog

My blog. Jekyll-based.

# Workflow

## 1. Write up your post
```
$ ~/my/
$ git clone git@github.com:delbao/delbao.github.io.git
$ cd delbao.github.io
$ git submodule update --init _posts
```

```
cd \_posts

$ git commit ...

# run the script to commit / push at the parent level
$ commit_submodule.sh

```

## 2. Diagram
ChatGPT -> Mermaid -> Exclidraw

Add media files to \_posts/media/. At build time, Github Actions copy them to
/assets/posts_media/

## 3. Formatting

## 4. Private posts

Private posts live in the `_private_posts/` submodule.

The site uses one shared Jekyll build:

```bash
bundle exec jekyll build
```

When `_private_posts/` is available, Jekyll renders it as the `private_posts`
collection. Those documents are output under `/private/...`.

GitHub Actions and private environments use the same Jekyll config, but they do
not use the same checkout step.

GitHub Actions builds the public site with this flow:

```bash
git submodule update --init _posts
rsync _posts/media/ assets/posts_media/
bundle exec jekyll build
```

That workflow intentionally does not initialize `_private_posts`.
This matters because `_private_posts` is a private submodule, and GitHub Pages
deployments should not require private repository credentials.

As a result, the GitHub Actions build behaves like this:

- `_posts` is present and contributes normal public posts.
- `_posts/media` is copied into `assets/posts_media/` before the build.
- `_private_posts` is absent, so the `private_posts` collection contributes no
  documents.
- Jekyll still uses the same `_config.yml`; the difference is only which
  submodules were checked out before the build.

Local or VPS environments that have access to the private repository can
initialize both submodules before building:

```bash
git submodule update --init _posts _private_posts
bundle exec jekyll build
```

That private build path behaves like this:

- `_posts` contributes the public site content.
- `_private_posts` is available, so Jekyll renders the `private_posts`
  collection as `/private/...`.
- The build command stays the same; only the checkout step changes.

In short: same build, different checkout.
