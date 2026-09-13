#!/usr/bin/env python3
"""Fallback: push local git commits to GitHub via REST API.

用于「git push 被网络网关阻断」的环境：把本地的若干连续 commit 通过
GitHub REST API（blobs -> trees -> commits -> ref）重建到远端，内容和提交信息保持一致
（远端 commit sha 会与本地不同）。

用法:
    PAT_VALUE=<your_token> python scripts/push_via_api.py <owner/repo> <commit1> [commit2 ...] [--branch main]

安全说明:
    Token 只从环境变量读取，不写入任何文件；请使用最小权限的细粒度 PAT
    （需要 Contents: Read and write；若还需建仓库再加 Administration）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests

API = "https://api.github.com"


def git(*args: str, binary: bool = False):
    out = subprocess.run(["git", *args], capture_output=True, check=True)
    return out.stdout if binary else out.stdout.decode("utf-8").strip()


class Pusher:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ct-scan-push",
        })

    def gh(self, method: str, path: str, body=None):
        r = self.session.request(method, f"{API}{path}", json=body, timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else None

    # ------------------------------------------------------------------
    def list_tree(self, commit: str):
        out = git("ls-tree", "-r", "-z", commit)
        entries = []
        for line in out.split("\0"):
            if not line:
                continue
            meta, path = line.split("\t", 1)
            mode, typ, sha = meta.split()
            entries.append({"mode": mode, "type": typ, "sha": sha, "path": path})
        return entries

    def commit_info(self, commit: str):
        msg = git("log", "-1", "--format=%B", commit)
        author_name = git("log", "-1", "--format=%an", commit)
        author_email = git("log", "-1", "--format=%ae", commit)
        author_date = git("log", "-1", "--format=%aI", commit)
        return {
            "message": msg,
            "author": {"name": author_name, "email": author_email, "date": author_date},
            "committer": {"name": author_name, "email": author_email, "date": author_date},
        }

    def push(self, commits, branch="main"):
        blob_map = {}
        all_entries = []
        for c in commits:
            all_entries.extend(self.list_tree(c))
        unique_blobs = {e["sha"]: e["path"] for e in all_entries if e["type"] == "blob"}
        print(f"[push-api] {len(unique_blobs)} blobs to upload across {len(commits)} commits")

        i = 0
        for sha, path in unique_blobs.items():
            i += 1
            content = git("cat-file", "blob", sha, binary=True)
            res = self.gh("POST", f"/repos/{self.repo}/git/blobs",
                          {"content": __import__("base64").b64encode(content).decode(),
                           "encoding": "base64"})
            if res["sha"] != sha:
                print(f"  ! sha mismatch for {path}: local={sha[:8]} remote={res['sha'][:8]}")
            blob_map[sha] = res["sha"]
            if i % 50 == 0 or i == len(unique_blobs):
                print(f"  blob {i}/{len(unique_blobs)} uploaded")

        parent = None
        last_sha = None
        for c in commits:
            tree_entries = []
            seen = set()
            for e in self.list_tree(c):
                if e["path"] in seen:
                    continue
                seen.add(e["path"])
                tree_entries.append({
                    "path": e["path"], "mode": e["mode"], "type": e["type"],
                    "sha": blob_map.get(e["sha"], e["sha"]) if e["type"] == "blob" else e["sha"],
                })
            tree = self.gh("POST", f"/repos/{self.repo}/git/trees",
                           {"tree": tree_entries} if parent is None else
                           {"tree": tree_entries, "base_tree": None})
            info = self.commit_info(c)
            body = {"message": info["message"], "tree": tree["sha"],
                    "parents": [parent] if parent else [], **info}
            commit = self.gh("POST", f"/repos/{self.repo}/git/commits", body)
            print(f"[push-api] commit {c[:8]} -> {commit['sha'][:8]}  {info['message'].splitlines()[0][:60]}")
            parent = commit["sha"]
            last_sha = commit["sha"]

        if last_sha is None:
            raise RuntimeError("no commits to push")
        try:
            self.gh("POST", f"/repos/{self.repo}/git/refs",
                    {"ref": f"refs/heads/{branch}", "sha": last_sha})
            print(f"[push-api] ref refs/heads/{branch} created")
        except RuntimeError as e:
            if "422" in str(e):
                self.gh("PATCH", f"/repos/{self.repo}/git/refs/heads/{branch}",
                        {"sha": last_sha, "force": True})
                print(f"[push-api] ref refs/heads/{branch} updated (force)")
            else:
                raise
        print("[push-api] done ✔")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", help="owner/repo")
    ap.add_argument("commits", nargs="+", help="按从旧到新的顺序给出 commit（sha / sha 区间勿混用）")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    token = os.environ.get("PAT_VALUE") or os.environ.get("GITHUB_PAT") or ""
    if not token:
        print("缺少 PAT_VALUE 环境变量（不要把 token 写进命令行参数）")
        return 1
    p = Pusher(args.repo, token)
    p.push(args.commits, args.branch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
