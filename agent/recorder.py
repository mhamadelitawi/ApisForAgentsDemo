"""JSONL record/replay of model turns and tool results.

Line types: meta, turn, tool_result, summary.
"""

import json
import time
from pathlib import Path


class Recorder:
    def __init__(self, path: Path | None):
        self.path = path
        self._fh = None
        self._t0 = time.monotonic()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8")

    def _write(self, kind: str, **data) -> None:
        if self._fh:
            line = {"type": kind, "t": round(time.monotonic() - self._t0, 2), **data}
            self._fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._fh.flush()

    def meta(self, **data) -> None:
        self._write("meta", **data)

    def turn(self, **data) -> None:
        self._write("turn", **data)

    def tool_result(self, **data) -> None:
        self._write("tool_result", **data)

    def summary(self, **data) -> None:
        self._write("summary", **data)

    def close(self) -> None:
        if self._fh:
            self._fh.close()


class NullRecorder(Recorder):
    def __init__(self):
        super().__init__(None)


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
