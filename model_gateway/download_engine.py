"""Hugging Face Xet download engine, extracted from the local downloader."""
import fcntl
import json
import os
import pathlib
import re
import urllib.parse
import shutil
import signal
import struct
import subprocess
import tempfile
import time

from threading import Event, RLock, Thread
from concurrent.futures import ThreadPoolExecutor

STATE_LOCK = RLock()
PROCESSES = {}
STOP = Event()
LAB = pathlib.Path(os.environ.get("MEDIA_LAB_ROOT", pathlib.Path.home() / "LocalModelLab")).expanduser().resolve()
TASKS_JSON = LAB / "download-tasks.json"
REPORT_DIR = LAB / "report" / "hf-xet-fetch"
LOG_DIR = LAB / "logs"
ENDPOINT = os.environ.get("MEDIA_HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
OFFICIAL_ENDPOINT = "https://huggingface.co"


def endpoint_for(source):
    return OFFICIAL_ENDPOINT if source == "official" else ENDPOINT


def probe_sources(repo=None):
    def probe(source):
        suffix = f"/api/models/{repo}" if repo else "/api/models?limit=1"
        result = subprocess.run(
            ["curl", "--disable", "-fsSL", "--noproxy", "*", "-o", "/dev/null",
             "--max-time", "5", "-w", "%{time_total}", endpoint_for(source) + suffix],
            capture_output=True, text=True,
        )
        return {"source": source, "reachable": result.returncode == 0,
                "milliseconds": round(float(result.stdout) * 1000) if result.returncode == 0 else None}
    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(probe, ("mirror", "official")))


def select_source(source, repo=None):
    if source != "auto":
        return source
    return next((item["source"] for item in probe_sources(repo) if item["reachable"]), "mirror")

WORKER = pathlib.Path(__file__).with_name("hf_xet_worker.sh")
TRANSIENT = (6, 7, 18, 28, 35, 52, 56, 92)
PROBE_INTERVAL = 900        # s between bucket probes per task
PROBE_SECONDS = 5   # xet bursts are short; a long probe drinks the burst
                    # the workers were meant to download (v3.2.1)
PROBE_THRESHOLD = 500_000    # B/s on the fast (unbounded) path; deliberately
                             # low — buckets oscillate, the post-spawn growth
                             # confirmation is the real gate
CONFIRM_SECONDS = 60        # growth window after spawn
CONFIRM_MIN_BYTES = 100_000
STALL_SECONDS = 180         # stream alive but 0 new bytes for this long is
                            # a zombie connection: kill and let the probe
                            # cycle re-spawn into the next burst
CONN_RECYCLE_S = 300        # sustained sub-burst speed this long on a live
CONN_RECYCLE_BPS = 1_000_000  # stream -> rebuild it (fresh xet speed draw)
TICK = 60
LOCK = None


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S %z')}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "hf-xet-fetch.log", "a") as f:
        f.write(line + "\n")


def slugify(repo):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", repo).strip("-").lower()


# ---------------------------------------------------------------- tasks ----

def load_tasks():
    if TASKS_JSON.exists():
        return json.loads(TASKS_JSON.read_text())
    return {}


def save_tasks(tasks):
    TASKS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(tasks, indent=2, ensure_ascii=False))
    tmp.replace(TASKS_JSON)


def task_status_path(task):
    return REPORT_DIR / f"{slugify(task['repo'])}.status.json"


def complete_path(target, task):
    return REPORT_DIR / f"{slugify(task['repo'])}.COMPLETE.json"


def write_status(task, phase, **kw):
    data = {"repo": task["repo"], "phase": phase,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    data.update(kw)
    p = task_status_path(task)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(p)


# ------------------------------------------------------------- metadata ----

def repo_meta(repo, target, fresh=True, source="mirror"):
    meta = target / ".hf-mirror-metadata.json"
    # Keep the registered commit pinned across restarts and source changes.
    if meta.exists():
        try:
            return json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            if not fresh:
                raise
    elif not fresh:
        raise RuntimeError("no metadata cache yet")
    meta.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=meta.parent, prefix=".hf-meta-", delete=False) as stream:
        tmp = pathlib.Path(stream.name)
    try:
        result = subprocess.run(
            ["curl", "--disable", "-fsSL", "--noproxy", "*", "--retry", "5",
             "--connect-timeout", "20", "--max-time", "180", "-o", str(tmp),
             f"{endpoint_for(source)}/api/models/{repo}?blobs=true"],
            capture_output=True, text=True,
        )
        if result.returncode:
            if meta.exists():
                return json.loads(meta.read_text())
            raise RuntimeError("Repository metadata fetch failed")
        data = json.loads(tmp.read_text())
        if not isinstance(data, dict) or not data.get("sha") or not isinstance(data.get("siblings"), list):
            raise ValueError("Invalid repository metadata")
        tmp.replace(meta)
        return data
    finally:
        tmp.unlink(missing_ok=True)


def tree_sizes(repo, prefix, source="mirror", revision="main"):
    """siblings from /api/models lack sizes; the tree endpoint has them."""
    seg = prefix.strip("/").split("/")[0] if prefix else ""
    urls = []
    if seg:
        urls.append(f"{endpoint_for(source)}/api/models/{repo}/tree/{urllib.parse.quote(revision, safe='')}/{urllib.parse.quote(seg, safe='')}")
    urls.append(f"{endpoint_for(source)}/api/models/{repo}/tree/{urllib.parse.quote(revision, safe='')}?recursive=true")
    for url in urls:
        r = subprocess.run(
            ["curl", "--disable", "-fsSL", "--noproxy", "*", "--retry", "3",
             "--connect-timeout", "20", "--max-time", "60", url],
            capture_output=True, text=True)
        if r.returncode:
            continue
        try:
            entries = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(entries, list):
            continue
        out = {}
        stack = list(entries)
        while stack:
            e = stack.pop()
            if e.get("type") == "directory":
                continue
            n = int(e.get("size") or (e.get("lfs") or {}).get("size") or 0)
            if e.get("path") and n:
                out[e["path"]] = n
        if out:
            return out
    return {}


def remote_size(url):
    r = subprocess.run(
        ["curl", "--disable", "-s", "--noproxy", "*", "-o", "/dev/null", "-D", "-",
         "-m", "30", "--range", "0-0", "-L", url],
        capture_output=True, text=True)
    m = re.findall(r"(?im)^content-range:\s*bytes\s*\d+-\d+/(\d+)", r.stdout)
    if m:
        return int(m[-1])
    m = re.findall(r"(?im)^content-length:\s*(\d+)", r.stdout)
    return int(m[-1]) if m else 0


def load_sizes(task):
    c = pathlib.Path(task["target"]) / ".hf-xet-sizes.json"
    try:
        return json.loads(c.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def sync_sizes(task):
    meta = repo_meta(task["repo"], pathlib.Path(task["target"]), source=task.get("source", "mirror"))
    sizes = tree_sizes(task["repo"], task.get("prefix") or "", task.get("source", "mirror"), meta["sha"])
    if sizes:
        c = pathlib.Path(task["target"]) / ".hf-xet-sizes.json"
        c.parent.mkdir(parents=True, exist_ok=True)
        merged = load_sizes(task)
        merged.update(sizes)
        c.write_text(json.dumps(merged))
    return sizes


def plan_for(task, meta, sizes=None):
    """[(relpath, url, size, dest, rev)] largest-first."""
    rev = meta["sha"]
    if sizes is None:
        sizes = load_sizes(task)
    plan = []
    for s in meta.get("siblings", []):
        rel = s["rfilename"]
        root = pathlib.Path(task["target"]).resolve()
        dest = root / rel
        if (not rel or ".." in pathlib.PurePosixPath(rel).parts
                or pathlib.PurePosixPath(rel).is_absolute()
                or not dest.resolve().is_relative_to(root)
                or not dest.with_name(dest.name + ".complete").resolve().is_relative_to(root)):
            raise ValueError("Invalid file path in repository metadata")
        if task["prefix"] and not rel.startswith(task["prefix"]):
            continue
        url = f"{endpoint_for(task.get('source', 'mirror'))}/{task['repo']}/resolve/{urllib.parse.quote(rev, safe='')}/{urllib.parse.quote(rel)}"
        size = int(s.get("size") or (s.get("lfs") or {}).get("size")
                   or sizes.get(rel) or 0)
        plan.append((rel, url, size, dest, rev))
    plan.sort(key=lambda t: (-(t[2] or 0), t[0]))
    return plan


def head(p):
    return p.stat().st_size if p.exists() else 0


def file_done(dest, size, rev):
    marker = dest.with_name(dest.name + ".complete")
    return (marker.exists() and marker.read_text().strip() == rev
            and dest.exists() and (not size or head(dest) == size))


def pending_for(task, fresh=True):
    meta = repo_meta(task["repo"], pathlib.Path(task["target"]), fresh=fresh, source=task.get("source", "mirror"))
    plan = plan_for(task, meta)
    if fresh and any(not p[2] for p in plan):
        sync_sizes(task)               # refresh .hf-xet-sizes.json
        plan = plan_for(task, meta)    # rebuild with known sizes
    out = []
    for rel, url, size, dest, rev in plan:
        if fresh and size and dest.exists() and head(dest) == size \
                and not dest.with_name(dest.name + ".complete").exists():
            dest.with_name(dest.name + ".complete").write_text(rev)
        if not file_done(dest, size, rev):
            out.append((rel, url, size, dest, rev))
    return out


# ------------------------------------------------------------ downloading --

def verify_safetensors(path):
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        hdr = json.loads(f.read(n))
    end = 0
    for k, meta in hdr.items():
        if k == "__metadata__" or not isinstance(meta, dict):
            continue  # header carries a non-tensor metadata entry
        end = max(end, meta["data_offsets"][1])
    want = 8 + n + end
    got = path.stat().st_size
    return want == got, f"hdr={want} disk={got}"



def verify_gguf(path):
    # Conservative structural check.  These ISTA-DASLab exports use a
    # private dtype numbering (qwen4exp/qwen35 variants: dual array headers
    # n-u32/n-u64 + u32 elem-type, padded 2-byte ints, non-standard array
    # element widths) that no public gguf table parses; full KV walking was
    # abandoned deliberately so a verifier can never wipe good downloads.
    # Structural floor: magic + version + plausible header fields.
    size = path.stat().st_size
    if size < 32:
        return False, f"too small ({size})"
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            return False, "bad magic"
        version = struct.unpack("<I", f.read(4))[0]
        if version < 2:
            return False, f"version={version}"
        n_tensors = struct.unpack("<Q", f.read(8))[0]
        n_kv = struct.unpack("<Q", f.read(8))[0]
        if not (1 <= n_tensors < 100_000):
            return False, f"n_tensors={n_tensors}"
        if not (1 <= n_kv < 10_000):
            return False, f"n_kv={n_kv}"
        # first KV key must be a sane short printable string
        klen = struct.unpack("<Q", f.read(8))[0]
        if not (3 < klen < 80):
            return False, f"first key len {klen}"
        key = f.read(klen)
        if not key or not all(32 <= c < 127 for c in key):
            return False, "first key not printable"
    return True, f"v{version} tensors={n_tensors} kv={n_kv} structural"


def verify_task(task, wipe_on_fail=True):
    mode = task.get("verify", "auto")
    pend_meta = repo_meta(task["repo"], pathlib.Path(task["target"]), source=task.get("source", "mirror"))
    results, all_ok = {}, True
    for rel, url, size, dest, rev in plan_for(task, pend_meta):
        if not file_done(dest, size, rev):
            continue  # incomplete files are verified after completion
        ok, msg = True, "size-only"
        if mode in ("auto", "safetensors") and dest.name.endswith(
                ".safetensors"):
            ok, msg = verify_safetensors(dest)
        elif mode in ("auto", "gguf") and dest.name.endswith(".gguf"):
            ok, msg = verify_gguf(dest)
        results[rel] = {"ok": ok, "detail": msg, "size": head(dest)}
        all_ok = all_ok and ok
    # cross-check safetensors index if present
    tdir = pathlib.Path(task["target"])
    for _, _, _, idx, _ in plan_for(task, pend_meta):
        if not idx.name.endswith("index.json") or not idx.is_file():
            continue
        try:
            wm = json.loads(idx.read_text()).get("weight_map", {})
        except (json.JSONDecodeError, OSError):
            continue
        missing = [t for t, shard in wm.items()
                   if not (idx.parent / shard).resolve().is_relative_to(tdir.resolve())
                   or not (idx.parent / shard).exists()]
        results[f"index:{idx.relative_to(tdir).as_posix()}"] = {
            "ok": not missing, "detail": f"tensors={len(wm)} "
            f"missing={len(missing)}"}
        all_ok = all_ok and not missing
    if not all_ok and wipe_on_fail:
        for rel, r in results.items():
            if not r.get("ok"):
                p = tdir / (rel[6:] if rel.startswith("index:") else rel)
                p.unlink(missing_ok=True)
                p.with_name(p.name + ".complete").unlink(missing_ok=True)
                log(f"verify wiped {rel} ({r['detail']})")
    return all_ok, results


def workers_running(repo):
    with STATE_LOCK:
        workers = [p for p in PROCESSES.get(repo, []) if p.poll() is None]
        PROCESSES[repo] = workers
        return workers


def have_total(task):
    tdir = pathlib.Path(task["target"])
    total = 0
    for rel, _u, _s, dest, _r in plan_for(task,
                                          repo_meta(task["repo"], tdir, source=task.get("source", "mirror"))):
        total += head(dest)
    return total


def probe_speed(task):
    tdir = pathlib.Path(task["target"])
    pend = pending_for(task)
    if not pend:
        return 0.0
    rel, url, size, dest, rev = pend[0]
    r = subprocess.run(
        ["curl", "--disable", "-s", "-o", "/dev/null", "-w", "%{speed_download}",
         "--noproxy", "*", "-m", str(PROBE_SECONDS), "-r", "0-", "-L", url],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def kill_workers(task):
    for process in workers_running(task["repo"]):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def spawn_workers(task):
    repo = task["repo"]
    pend = pending_for(task)
    before = have_total(task)
    with STATE_LOCK:
        current = load_tasks().get(repo)
        if STOP.is_set() or current != task or current.get("paused") or workers_running(repo):
            return False
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / f"hf-xet-fetch.{slugify(repo)}.workers.log").open("a") as log_file:
            for _, url, size, dest, rev in pend[:int(task.get("workers", 12))]:
                dest.parent.mkdir(parents=True, exist_ok=True)
                process = subprocess.Popen(
                    ["/bin/bash", str(WORKER), url, str(dest), str(size), rev,
                     str(int(task.get("conn_time", 300))), str(os.getpid())],
                    stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True,
                )
                PROCESSES[repo].append(process)
    wait_chunk(CONFIRM_SECONDS, STOP)
    grown = have_total(task) - before
    if grown < CONFIRM_MIN_BYTES:
        # v3.6: a worker restarting a partial file (range-unsupported
        # resume) can show net-NEGATIVE bytes while other streams are
        # healthy — re-measure one more window before declaring the bucket
        # empty (2026-11-05 Jundot: healthy streams killed on every spawn
        # because one wiping shard dominated the delta).
        wait_chunk(CONFIRM_SECONDS, STOP)
        grown2 = have_total(task) - before
        if grown2 > grown:
            grown = grown2
    if STOP.is_set():
        return False
    if grown < CONFIRM_MIN_BYTES:
        log(f"{task['repo']}: spawned but grew {grown}B in "
            f"{2 * CONFIRM_SECONDS}s — drained; killing, will retry later")
        kill_workers(task)
        write_status(task, "bucket-empty-waiting",
                     note="no growth after spawn")
        return False
    log(f"{task['repo']}: workers confirmed +{grown / 1e6:.0f}MB in "
        f"{CONFIRM_SECONDS}s")
    return True


def finalize(task):
    ok, res = verify_task(task, wipe_on_fail=True)
    cp = complete_path(pathlib.Path(task["target"]), task)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(
        {"repo": task["repo"], "phase": "complete" if ok else "corrupt",
         "completed_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
         "target": task["target"], "files": res}, indent=2))
    log(f"{task['repo']}: verify all_ok={ok}")
    if ok:
        write_status(task, "complete", note="verified; task closed")
        record_history(task)
    return ok


def history_path():
    return REPORT_DIR / "history.json"


def record_history(task):
    try:
        hist = json.loads(history_path().read_text()) \
            if history_path().exists() else []
    except json.JSONDecodeError:
        hist = []
    files = sum(1 for _ in pathlib.Path(task["target"]).rglob("*")
                if _.is_file() and not _.name.endswith(".complete")) \
        if pathlib.Path(task["target"]).is_dir() else 0
    entry = {"repo": task["repo"], "target": task["target"],
             "bytes": task.get("bytes", 0), "files": files,
             "verify": task.get("verify", "auto"),
             "note": task.get("note", ""),
             "finished_at": time.strftime("%Y-%m-%d %H:%M %z")}
    hist = [h for h in hist if h.get("repo") != task["repo"]] + [entry]
    history_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = history_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=1))
    tmp.replace(history_path())


def load_history():
    p_ = history_path()
    hist = []
    if p_.exists():
        try:
            hist = json.loads(p_.read_text())
        except json.JSONDecodeError:
            hist = []
    # 回填：老任务只有 COMPLETE.json 的补进历史（只补一次，之后以 history.json 为准）
    known = {h.get("repo") for h in hist}
    for cf in sorted(REPORT_DIR.glob("*.COMPLETE.json")):
        try:
            st = json.loads(cf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        repo = st.get("repo")
        if not repo or repo in known:
            continue
        hist.append({"repo": repo, "target": st.get("target", ""),
                     "bytes": 0, "files": None,
                     "verify": st.get("note", ""), "note": "（历史记录回填）",
                     "finished_at": time.strftime(
                         "%Y-%m-%d %H:%M %z",
                         time.localtime(cf.stat().st_mtime))})
        known.add(repo)
    return sorted(hist, key=lambda h: h.get("finished_at", ""), reverse=True)


LAST_PROBE = {}


def supervise_loop(stop=None, idle_sleep=None):
    log("supervisor start")
    last_probe = LAST_PROBE
    slow_ticks = {}  # repo -> consecutive sub-threshold probes
    stall = {}       # repo -> [have_bytes, stalled_seconds] (stream watchdog)
    idle_logged = False  # v3.5: log once when entering all-done idle
    # repo -> [high_bytes, last_bytes, last_fast_time]: low-rate window since
    # last high burst; drives v3.4 connection recycling below CONN_RECYCLE_BPS
    rate_state = {}

    def rotate_to_tail(repo):
        # A repo bucket throttled to a trickle must not block the queue:
        # push it behind everything else; healthy buckets take the head.
        try:
            with STATE_LOCK:
                fresh = load_tasks()
                if repo not in fresh or fresh[repo].get("done"):
                    return
                fresh[repo]["added"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S +0800", time.localtime(time.time() + 10))
                save_tasks(fresh)
                log(f"{repo}: slow bucket, rotated to queue tail")
        except Exception as e:  # noqa: BLE001
            log(f"{repo}: rotate failed: {e}")
    while stop is None or not stop.is_set():
        active = False
        try:
            tasks = load_tasks()
        except Exception as e:  # noqa: BLE001 — never kill the supervisor
            log(f"load_tasks error: {e}")
            wait_chunk(TICK, stop)
            continue
        order = sorted(tasks.items(), key=lambda kv: kv[1].get("added", ""))
        # paused tasks: kill any live streams, then skip
        for r, tt in order:
            if tt.get("paused") and not tt.get("done") and workers_running(r):
                kill_workers(tt)
                write_status(tt, "paused")
                log(f"{r}: paused task had live workers; killed")
        # serial queue: only the oldest unfinished, unpaused task runs;
        # concentrating streams on one repo bucket is also the fastest way
        queue = [(r, tt) for r, tt in order
                 if not tt.get("done") and not tt.get("paused")]
        for repo, task in queue[:1]:
            active = True
            try:
                pend = pending_for(task)
            except Exception as e:  # noqa: BLE001 — network blips
                log(f"{repo}: metadata error {e}")
                continue
            # connection recycling (v3.4): xet stamps a connection's speed at
            # handshake and keeps it there — a trickle-phase socket never
            # upgrades, but a fresh one may draw the full-speed burst (same
            # minute: old socket 0B vs new socket 4.6MB/s). Re-roll the dice
            # while throughput stays below burst level; fast streams are kept.
            now2 = time.time()
            cur = have_total(task)
            hb, lb, lt = rate_state.setdefault(repo, (cur, cur, now2))
            bps = (cur - lb) / (now2 - lt) if now2 > lt else 0.0
            if bps >= CONN_RECYCLE_BPS:
                rate_state[repo] = (cur, cur, now2)
            else:
                rate_state[repo] = (hb, max(lb, cur), lt)
            if workers_running(repo) and now2 - rate_state[repo][2] \
                    >= CONN_RECYCLE_S and bps < CONN_RECYCLE_BPS:
                log(f"{repo}: stream at {bps / 1e6:.2f} MB/s for "
                    f"{int(now2 - rate_state[repo][2])}s — recycling "
                    "connections for a fresh speed draw")
                kill_workers(task)
                last_probe[repo] = now2 - PROBE_INTERVAL  # probe right away
                continue
            if not pend:
                if finalize(task):
                    with STATE_LOCK:
                        rows = load_tasks()
                        if repo in rows and rows[repo]["target"] == task["target"]:
                            rows[repo]["done"] = True
                            save_tasks(rows)
                continue
            if workers_running(repo):
                # watchdog: a connection can stay open yet deliver nothing
                # once the burst moves on — kill the zombie, re-probe next tick
                now2 = time.time()
                hb = have_total(task)
                if stall.setdefault(repo, [hb, 0])[0] == hb:
                    stall[repo][1] += TICK
                    if stall[repo][1] >= STALL_SECONDS:
                        log(f"{repo}: stream stalled "
                            f"({STALL_SECONDS}s, no growth) — killing zombie, "
                            "will re-spawn into the next burst")
                        kill_workers(task)
                        stall[repo] = [hb, 0]
                        last_probe[repo] = now2 - PROBE_INTERVAL + 30
                        continue
                else:
                    stall[repo] = [hb, 0]
                write_status(task, "running", pending=len(pend),
                             pending_bytes=sum(p[2] - head(p[3]) for p in pend))
                continue
            now = time.time()
            # If another of our tasks is already streaming, our probe is
            # distorted by local bandwidth sharing — trust spawn-confirm
            # instead of the probe threshold (avoids starving small tasks).
            if any(workers_running(r) for r in tasks if r != repo):
                if now - last_probe.get(repo, 0) < 300 and repo in last_probe:
                    continue
                last_probe[repo] = now
                log(f"{repo}: peer task streaming; spawn w/o probe gate")
                if not spawn_workers(task):
                    # burst died mid-connect: retry in 2min, not full cooldown
                    last_probe[repo] = time.time() - PROBE_INTERVAL + 120
                continue
            if now - last_probe.get(repo, 0) < PROBE_INTERVAL \
                    and repo in last_probe:
                continue
            last_probe[repo] = now
            speed = probe_speed(task)
            log(f"{repo}: probe {speed / 1e6:.2f} MB/s, "
                f"pending={len(pend)}")
            if speed >= PROBE_THRESHOLD:
                write_status(task, "probing-clear",
                             probe_MBps=round(speed / 1e6, 2))
                if not spawn_workers(task):
                    # probe showed water but the stream got nothing
                    # (burst vanished mid-connect): retry in 2min, not 15
                    last_probe[repo] = time.time() - PROBE_INTERVAL + 120
                else:
                    slow_ticks.pop(repo, None)
            elif speed >= 100_000:
                # dribbling bucket: still worth streaming — trickle-download
                # while waiting for the burst instead of idling.
                # Refill bursts usually follow soon — retry in 180s
                # instead of waiting out the full cooldown.
                spawn_workers(task)
                last_probe[repo] = now - PROBE_INTERVAL + 180
            else:
                slow_ticks[repo] = slow_ticks.get(repo, 0) + 1
                if slow_ticks[repo] >= 3:
                    slow_ticks[repo] = 0
                    rotate_to_tail(repo)
        if not active:
            if idle_sleep is None:
                if any(not tt.get("done") for _, tt in order):
                    # only paused tasks remain: stay alive, resume needs us
                    wait_chunk(max(TICK, 30), stop)
                    continue
                # v3.5: all tasks done — stay alive and keep re-scanning the
                # task file; panel/CLI additions must start without a manual
                # service restart (2026-11-05 Jundot stuck at 0% bug: the
                # supervisor thread had exited, so a newly added task was
                # never picked up)
                if not idle_logged:
                    log("all tasks done; supervisor idle-waiting for new tasks")
                    idle_logged = True
                wait_chunk(60, stop)
                continue
            idle_logged = False
            wait_chunk(idle_sleep, stop)
            continue
        wait_chunk(TICK, stop)
    return 0


def wait_chunk(seconds, stop=None):
    end = time.time() + seconds
    while time.time() < end:
        if stop is not None and stop.is_set():
            return
        time.sleep(min(5, end - time.time()))


def acquire_lock():
    global LOCK
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOCK = open(REPORT_DIR / "supervisor.lock", "a")
    try:
        fcntl.flock(LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        LOCK.close()
        LOCK = None
        return False


def register_task(repo, target, prefix="", workers=12, conn_time=300,
                  verify="auto", note="", source="auto"):
    if not target:
        target = str(LAB / "models"
                     / repo.rstrip("/").split("/")[-1])
    target = str(pathlib.Path(target).expanduser().resolve())
    task = {"repo": repo, "target": target, "prefix": prefix or "", "source": select_source(source, repo),
            "workers": int(workers), "conn_time": int(conn_time),
            "verify": verify, "note": note or "",
            "added": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    with STATE_LOCK:
        rows = load_tasks()
        if any(slugify(name) == slugify(repo) or row["target"] == target for name, row in rows.items()):
            raise FileExistsError("Model repository or destination already registered")
    if not pathlib.Path(target).is_relative_to(LAB / "models"):
        raise ValueError("Download destination must be inside the model directory")
    meta = repo_meta(repo, pathlib.Path(target), source=task["source"])
    sync_sizes(task)
    plan = plan_for(task, meta)
    if not plan:
        raise ValueError("No file matches the requested prefix")
    total = sum(p[2] for p in plan)
    have = sum(head(p[3]) for p in plan)
    free = shutil.disk_usage(target).free
    with STATE_LOCK:
        tasks = load_tasks()
        if any(slugify(name) == slugify(repo) or t["target"] == target for name, t in tasks.items()):
            raise FileExistsError("Model repository or destination already registered")
        tasks[repo] = task
        save_tasks(tasks)
    return task, {"repo": repo, "rev": meta["sha"][:12],
                  "files": len(plan), "total_bytes": total,
                  "have_bytes": have, "free_bytes": free,
                  "enough_disk": free > max(total - have, 0) * 1.02}


def search_repos(q, limit=15, source="auto"):
    url = (f"{endpoint_for(select_source(source))}/api/models?search={urllib.parse.quote(q)}"
           f"&sort=downloads&direction=-1&limit={int(limit)}")
    r = subprocess.run(["curl", "--disable", "-sL", "--noproxy", "*", "-m", "15", url],
                       capture_output=True, text=True)
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []
    out = []
    for m in data:
        if isinstance(m, dict) and m.get("id"):
            out.append({"id": m["id"], "downloads": m.get("downloads", 0),
                        "likes": m.get("likes", 0),
                        "tags": (m.get("tags") or [])[:5]})
    return out


VIEW_HIST = {}  # repo -> [(ts, have_bytes)], sliding window for rate/ETA


def rate_eta(repo, have, total):
    """Sample per API view call (panel refreshes every 5s); estimate speed
    over the oldest sample at least 45s back so the number stays stable."""
    now = time.time()
    h = VIEW_HIST.setdefault(repo, [])
    if not h or now - h[-1][0] >= 3:
        h.append((now, have))
        del h[:-40]
    base = None
    for ts, by in h:
        if now - ts >= 45:
            base = (ts, by)
        else:
            break
    if base is None or now - base[0] < 30 or have < base[1]:
        return None, None
    rate = (have - base[1]) / (now - base[0])
    if rate <= 0:
        return 0.0, None
    return rate, (total - have) / rate if total > have else 0


def task_view(tasks, repo, task):
    st = {}
    try:
        st = json.loads(task_status_path(task).read_text())
    except (OSError, json.JSONDecodeError):
        pass
    have = total = pending_n = 0
    try:
        # view path is cache-only: a slow mirror must never hang /api/tasks
        plan = plan_for(task, repo_meta(repo, pathlib.Path(task["target"]), fresh=False))
        total = sum(p[2] for p in plan)
        have = sum(head(p[3]) for p in plan)
        pending_n = sum(1 for _ in pending_for(task, fresh=False))
    except Exception:  # noqa: BLE001 — view must never break
        pass
    order = sorted(tasks.items(), key=lambda kv: kv[1].get("added", ""))
    q = [r for r, tt in order if not tt.get("done") and not tt.get("paused")]
    qpos = (q.index(repo) + 1) if repo in q else None
    r, eta = rate_eta(repo, have, total)
    phase = "complete" if task.get("done") else st.get("phase", "queued")
    # resume does not rewrite the status snapshot; never show stale paused
    if phase == "paused" and not task.get("paused") and not task.get("done"):
        phase = "queued"
    return {"slug": slugify(repo), "repo": repo, "target": task["target"],
            "prefix": task.get("prefix", ""), "source": task.get("source", "mirror"), "done": task.get("done", False),
            "phase": phase,
            "note": task.get("note", ""), "paused": bool(task.get("paused")),
            "queue_pos": None if task.get("done") or task.get("paused") else qpos,
            "have_bytes": have, "total_bytes": total,
            "pct": round(100.0 * have / total, 2) if total else 0.0,
            "rate_MBps": round(r / 1e6, 2) if r is not None else None,
            "eta_minutes": round(eta / 60) if eta is not None else None,
            "pending": pending_n, "workers_running": len(workers_running(repo)),
            "probe_MBps": st.get("probe_MBps"),
            "updated_at": st.get("updated_at")}




def start():
    if not acquire_lock():
        return None
    STOP.clear()
    def run():
        while not STOP.is_set():
            try:
                supervise_loop(STOP, idle_sleep=5)
            except Exception as exc:
                log(f"supervisor iteration failed: {exc}")
                wait_chunk(TICK, STOP)
    thread = Thread(target=run, name="model-downloads", daemon=True)
    thread.start()
    return thread


def stop(thread):
    global LOCK
    STOP.set()
    with STATE_LOCK:
        for repo in list(PROCESSES):
            kill_workers({"repo": repo})
    thread.join(timeout=6)
    if not thread.is_alive() and LOCK is not None:
        LOCK.close()
        LOCK = None
