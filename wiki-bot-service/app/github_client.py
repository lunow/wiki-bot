import asyncio
import base64
from typing import Dict, Optional, Union

import httpx


class WikiStore:
    """GitHub-backed store for the wiki/ folder (Git Data API).

    Reads are by blob SHA (exact, never stale, cached forever); writes are one
    atomic commit containing any number of files.
    """

    def __init__(self, token: str, repo: str, branch: str = "main"):
        self.repo = repo
        self.branch = branch
        self.api = f"https://api.github.com/repos/{repo}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client: Optional[httpx.AsyncClient] = None
        self._blobs: Dict[str, str] = {}  # blob sha -> text

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self.headers, timeout=30.0, follow_redirects=True
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def snapshot(self) -> Dict[str, dict]:
        """Map of wiki/**/*.md path -> {sha, size} at the branch head."""
        resp = await self.client.get(
            f"{self.api}/git/trees/{self.branch}", params={"recursive": "1"}
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("truncated"):
            raise RuntimeError("GitHub tree response truncated; repo too large to list")
        return {
            i["path"]: {"sha": i["sha"], "size": i.get("size", 0)}
            for i in data["tree"]
            if i["type"] == "blob"
            and i["path"].startswith("wiki/")
            and i["path"].endswith(".md")
        }

    async def read(self, path: str, snap: Dict[str, dict]) -> str:
        sha = snap[path]["sha"]
        if sha not in self._blobs:
            resp = await self.client.get(f"{self.api}/git/blobs/{sha}")
            resp.raise_for_status()
            self._blobs[sha] = base64.b64decode(resp.json()["content"]).decode("utf-8")
        return self._blobs[sha]

    async def read_many(self, paths, snap: Dict[str, dict]) -> Dict[str, str]:
        sem = asyncio.Semaphore(8)

        async def one(p: str) -> str:
            async with sem:
                return await self.read(p, snap)

        paths = list(paths)
        return dict(zip(paths, await asyncio.gather(*(one(p) for p in paths))))

    async def commit_files(self, files: Dict[str, Union[str, bytes]], message: str) -> str:
        """Commit all files in a single commit. Returns the commit SHA."""
        ref = await self.client.get(f"{self.api}/git/ref/heads/{self.branch}")
        ref.raise_for_status()
        head_sha = ref.json()["object"]["sha"]

        commit = await self.client.get(f"{self.api}/git/commits/{head_sha}")
        commit.raise_for_status()
        base_tree = commit.json()["tree"]["sha"]

        entries = []
        for path, content in files.items():
            entry = {"path": path, "mode": "100644", "type": "blob"}
            if isinstance(content, bytes):
                blob = await self.client.post(
                    f"{self.api}/git/blobs",
                    json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
                )
                blob.raise_for_status()
                entry["sha"] = blob.json()["sha"]
            else:
                entry["content"] = content
            entries.append(entry)

        tree = await self.client.post(
            f"{self.api}/git/trees", json={"base_tree": base_tree, "tree": entries}
        )
        tree.raise_for_status()

        new_commit = await self.client.post(
            f"{self.api}/git/commits",
            json={"message": message, "tree": tree.json()["sha"], "parents": [head_sha]},
        )
        new_commit.raise_for_status()
        new_sha = new_commit.json()["sha"]

        # Fast-forward only (no force): fails if someone pushed in between
        upd = await self.client.patch(
            f"{self.api}/git/refs/heads/{self.branch}", json={"sha": new_sha}
        )
        upd.raise_for_status()
        return new_sha
