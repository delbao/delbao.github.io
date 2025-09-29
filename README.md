# Blog

My blog. Jekyll-based.

# Workflow

## 1. Write up your post
```
$ ~/my/
$ git clone --recurse-submodules git@github.com:delbao/delbao.github.io.git
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


