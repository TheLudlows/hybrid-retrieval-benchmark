"""Persist MIRACL run state and sample local benchmark resources."""

import json
import os
from pathlib import Path
import threading
import time


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    with pending.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def proc_snapshot():
    rows = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            comm, rest = (path / "stat").read_text().rsplit(") ", 1)
            values = rest.split()
            if values[0] == "Z":
                continue
            rows[int(path.name)] = {
                "ppid": int(values[1]),
                "comm": comm.split("(", 1)[1],
                "ticks": int(values[11]) + int(values[12]),
                "rss": int(values[21]) * os.sysconf("SC_PAGE_SIZE"),
            }
        except (OSError, ValueError, IndexError):
            continue
    return rows


class Sampler:
    def __init__(self, roots, target):
        self.roots, self.target = roots, target
        self.stop_event = threading.Event()
        self.samples = []

    def snapshot(self):
        procs = proc_snapshot()
        groups = {}
        for name, root in self.roots.items():
            selected = (
                {p for p, row in procs.items() if row["comm"] == "postgres"}
                if name == "postgres_instance"
                else {root}
            )
            while True:
                children = {p for p, row in procs.items() if row["ppid"] in selected}
                if children <= selected:
                    break
                selected |= children
            groups[name] = {
                "pids": sorted(selected & procs.keys()),
                "rss_bytes_sum": sum(procs[p]["rss"] for p in selected if p in procs),
                "cpu_ticks": {
                    str(p): procs[p]["ticks"] for p in selected if p in procs
                },
            }
        return {
            "unix": time.time(),
            "groups": groups,
            "host_cgroup_memory_bytes": int(
                Path("/sys/fs/cgroup/memory.current").read_text()
            ),
        }

    def start(self):
        self.samples.append(self.snapshot())

        def loop():
            while not self.stop_event.wait(0.5):
                self.samples.append(self.snapshot())

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        self.thread.join()
        self.samples.append(self.snapshot())
        with self.target.open("w") as stream:
            for row in self.samples:
                stream.write(json.dumps(row) + "\n")
        summary = {}
        for name in self.roots:
            first = self.samples[0]["groups"][name]
            last = self.samples[-1]["groups"][name]
            summary[name] = {
                "baseline_rss_bytes_sum": first["rss_bytes_sum"],
                "peak_sampled_rss_bytes_sum": max(
                    s["groups"][name]["rss_bytes_sum"] for s in self.samples
                ),
                "cpu_seconds": sum(
                    max(0, ticks - first["cpu_ticks"].get(pid, 0))
                    for pid, ticks in last["cpu_ticks"].items()
                )
                / os.sysconf("SC_CLK_TCK"),
            }
        return {
            "sample_interval_s": 0.5,
            "components": summary,
            "peak_host_cgroup_memory_bytes": max(
                s["host_cgroup_memory_bytes"] for s in self.samples
            ),
            "limitations": "RSS sums may count shared pages more than once; PostgreSQL is instance-wide, not single-bank attribution. Shared model and idle peer API are reported separately. Peaks are sampled.",
        }
