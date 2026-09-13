"""Fallback: push local git commits to GitHub via REST API.

用于「git push 被网络网关阻断」的环境：把本地若干连续 commit 通过
GitHub REST API（blobs -> trees -> commits -> ref）推送到远端。
优化点：
- 自动比较远端已有对象，只上传「新增」blob（可反复重试、断点续传式）；
- 并行上传 + 自动重试；
- 远端 commit 与本地 commit 内容一致（sha 会不同）。

用法:
    PAT_VALUE=<your_token> python scripts/push_via_api.py <owner/repo> <commit1> [commit2 ...] [--branch main]

安全说明:
    Token 只从环境变量读取，不写入任何文件。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

API = "https://api.github.com"


def git(*args: str, binary: bool = False):
    out = subprocess.run(["git", *args], capture_output=True, check=True).stdout
    return out if binary else out.decode("utf-8")


class Gh:
    def __init__(self, repo: str, token: str, workers: int = 6):
        self.repo = repo
        self.workers = workers
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ct-scan-push",
        })

    def call(self, method: str, path: str, body=None, retries: int = 6):
        last = None
        for i in range(retries):
            try:
                r = self.s.request(method, f"{API}{path}", json=body, timeout=180)
                if r.status_code >= 300:
                    if r.status_code in (429, 500, 502, 503, 504):
                        last = f"{r.status_code}: {r.text[:200]}"
                        time.sleep(2 * (i + 1))
                        continue
                    raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
                return r.json() if r.content else None
            except requests.RequestException as e:
                last = str(e)
                time.sleep(2 * (i + 1))
        raise RuntimeError(f"{method} {path} failed: {last}")

    # ------------------------------------------------------------------
    def collect_remote_objects(self, commit_sha: str):
        """All blob & tree shas already present on the remote (for this commit's tree)."""
        blobs, trees = set(), set()
        c = self.call("GET", f"/repos/{self.repo}/git/commits/{commit_sha}")
        tree_sha = c["tree"]["sha"]
        try:
            t = self.call("GET", f"/repos/{self.repo}/git/trees/{tree_sha}?recursive=1")
            for e in t.get("tree", []):
                (blobs if e["type"] == "blob" else trees).add(e["sha"])
            if t.get("truncated"):
                print("  ! warning: remote tree listing truncated")
        except RuntimeError as e:
            print("  ! cannot list remote tree:", e)
        return blobs, trees

    def upload_blobs(self, entries):
        """entries: list of (sha, path). Returns actual-number uploaded."""
        done = 0
        lock = __import__("threading").Lock()

        def one(item):
            nonlocal done
            sha, path = item
            content = git("cat-file", "blob", sha, binary=True)
            res = self.call("POST", f"/repos/{self.repo}/git/blobs",
                            {"content": base64.b64encode(content).decode(), "encoding": "base64"})
            if res["sha"] != sha:
                raise RuntimeError(f"blob sha mismatch for {path}: {sha[:8]} vs {res['sha'][:8]}")
            with lock:
                done += 1
                if done % 50 == 0:
                    print(f"  uploaded {done}/{len(entries)} blobs")

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = [ex.submit(one, it) for it in entries]
            for f in as_completed(futs):
                f.result()
        return done

    def build_tree(self, local_tree_sha: str, remote_trees: set, memo):
        if local_tree_sha in remote_trees:
            return local_tree_sha
        if local_tree_sha in memo:
            return memo[local_tree_sha]
        out = git("ls-tree", local_tree_sha)
        entries = []
        for line in out.splitlines():
            if not line.strip():
                continue
            meta, name = line.split("\t", 1)
            mode, typ, sha = meta.split()
            if typ == "tree":
                sub = self.build_tree(sha, remote_trees, memo)
                entries.append({"path": name, "mode": mode, "type": "tree", "sha": sub})
            elif typ == "commit":  # submodule
                entries.append({"path": name, "mode": mode, "type": "commit", "sha": sha})
            else:
                entries.append({"path": name, "mode": mode, "type": "blob", "sha": sha})
        res = self.call("POST", f"/repos/{self.repo}/git/trees", {"tree": entries})
        memo[local_tree_sha] = res["sha"]
        return res["sha"]

    def commit_info(self, sha: str):
        msg = git("log", "-1", "--format=%B", sha).rstrip("\n")
        name = git("log", "-1", "--format=%an", sha)
        email = git("log", "-1", "--format=%ae", sha)
        date = git("log", "-1", "--format=%aI", sha)
        return {"message": msg,
                "author": {"name": name, "email": email, "date": date},
                "committer": {"name": name, "email": email, "date": date}}

    # ------------------------------------------------------------------
    def push(self, commits, branch="main"):
        ref = self.call("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
        remote_head = ref["object"]["sha"]
        print(f"[push-api] remote head: {remote_head[:10]}")
        print("[push-api] fetching remote object index (this may take a moment)...")
        remote_blobs, remote_trees = self.collect_remote_objects(remote_head)
        print(f"[push-api] remote has {len(remote_blobs)} blobs, {len(remote_trees)} trees")

        # parent chain check
        first_parent = git("log", "-1", "--format=%P", commits[0]).strip()
        if first_parent != remote_head:
            print(f"[push-api] ! warning: first commit parent {first_parent[:10]} != remote head {remote_head[:10]}")
            print("[push-api]   (push will still be attempted as fast-forward; may fail if not related)")

        # collect new blobs over all target commits
        new_blobs = {}
        for c in commits:
            out = git("ls-tree", "-r", c)
            for line in out.splitlines():
                if not line.strip():
                    continue
                meta, path = line.split("\t", 1)
                mode, typ, sha = meta.split()
                if typ == "blob" and sha not in remote_blobs:
                    new_blobs[sha] = path
        print(f"[push-api] new blobs to upload: {len(new_blobs)}")
        t0 = time.time()
        n = self.upload_blobs(sorted(new_blobs.items()))
        print(f"[push-api] blobs uploaded: {n} in {time.time()-t0:.1f}s")

        # build commits sequentially
        memo = {}
        parent = remote_head
        last_sha = None
        for c in commits:
            tree_local = git("rev-parse", f"{c}^{{tree}}")
            tree_remote = self.build_tree(tree_local, remote_trees, memo)
            info = self.commit_info(c)
            body = {"message": info["message"], "tree": tree_remote,
                    "parents": [parent], **info}
            new_commit = self.call("POST", f"/repos/{self.repo}/git/commits", body)
            print(f"[push-api] commit {c[:8]} -> {new_commit['sha'][:8]}  {info['message'].splitlines()[0][:60]}")
            parent = new_commit["sha"]
            last_sha = new_commit["sha"]

        self.call("PATCH", f"/repos/{self.repo}/git/refs/heads/{branch}",
                  {"sha": last_sha, "force": False})
        print(f"[push-api] ref refs/heads/{branch} -> {last_sha[:10]} ✔")
        return last_sha


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("commits", nargs="+")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    token = os.environ.get("PAT_VALUE") or os.environ.get("GITHUB_PAT") or ""
    if not token:
        print("缺少 PAT_VALUE 环境变量")
        return 1
    gh = Gh(args.repo, token, args.workers)
    gh.push(args.commits, args.branch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
