"""subgen_transcribe: eligibility (acceptance table) and the strictly-serial worker against a fake subgen."""
import json
import os
import time

import pytest

from anotherr import actions, db, settings, subgen


# ---------------------------------------------------------------- helpers

def T(i, lang, tag=None, default=False, confident=True, agree=True):
    return {"a_index": i, "tag": tag if tag is not None else lang, "default": default, "effective": lang,
            "confident": confident, "all_agree": agree, "windows": [{"lang": lang, "prob": 0.97}] * 3,
            "commentary": False, "channels": 2}


def S(lang="eng", kind="full", external=False):
    return {"lang": lang, "kind": kind, "external": external, "s_index": None if external else 0}


@pytest.fixture
def lib(tmp_path, monkeypatch):
    settings.save({"languages": {"subtitle_language": "eng"},
                   "subgen": {"url": "http://subgen.test", "poll_s": 0, "pause_between_s": 0, "timeout_min_s": 0,
                              "timeout_factor": 1000}})
    settings.state_set("subgen_pilot_done", True)
    monkeypatch.setattr(subgen, "kick", lambda: None)  # tests drive the worker synchronously
    monkeypatch.setattr(subgen, "_sleep", lambda s: time.sleep(0.001))
    return tmp_path


def show(title="Run BTS!", orig="kor", arr_id=None):
    return db.x("INSERT INTO items(source, arr_id, title, kind, original_language) VALUES ('sonarr', ?, ?, 'series', ?)",
                (arr_id or int(time.time() * 1e6) % 10**9, title, orig))


def episode(lib, iid, label, tracks, subs=(), codes=("subs_missing",), season=1, duration=1200.0, date_added=""):
    path = str(lib / f"{iid}-{label}.mp4")
    open(path, "wb").write(b"x")
    ev = {"scan_started": time.time() - 60, "tracks": tracks, "subs": list(subs)}
    fid = db.x("INSERT INTO files(item_id, arr_file_id, arr_path, local_path, label, episodes, first_seen, duration, "
               "date_added, audio_evidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (iid, abs(hash(path)) % 10**8, path, path, label, json.dumps([{"id": 1, "season": season}]), time.time(),
                duration, date_added, json.dumps(ev)))
    for c in codes:
        db.x("INSERT INTO issues(file_id, code, severity, message, status, created_at) VALUES (?,?,'error','m','open',?)",
             (fid, c, time.time()))
    return fid


def failed_before(iid, fid):
    """Bazarr's search ran for this series and the problem came back."""
    old = time.time() - 3600
    db.x("INSERT INTO actions(file_id, kind, status, codes, created_at, updated_at) VALUES (?, 'resub', 'done', "
         "'[\"subs_missing\"]', ?, ?)", (fid, old, old))
    db.x("UPDATE issues SET created_at=? WHERE file_id=?", (time.time(), fid))


def plan(fid, manual=True):
    return subgen.explain(fid, manual=manual)


ENGLISH = ["I think we should go there now", "Okay, let's do it", "What are you doing here?",
           "We can't stop now, come on", "That's right, I know"]


class FakeSubgen:
    """One job processing at a time (concurrent_transcriptions=1); writes <stem>.subgen.small.<src>.srt like v4.38."""

    def __init__(self):
        self.queued, self.processing = [], []
        self.ticks: dict[str, int] = {}
        self.submits: list[tuple] = []
        self.cancels: list[str] = []
        self.max_in_flight = 0
        self.mode: dict[str, str] = {}      # path -> ok | korean | nofile | slow | refuse
        self.attempts: dict[str, int] = {}
        self.log: list[tuple] = []

    def _advance(self):
        if self.processing:
            p = self.processing[0]
            self.ticks[p] -= 1
            if self.ticks[p] <= 0:
                self.processing.pop(0)
                self._write(p)
        if not self.processing and self.queued:
            self.processing.append(self.queued.pop(0))
        self.max_in_flight = max(self.max_in_flight, len(self.queued) + len(self.processing))

    def _write(self, p):
        if p.startswith("/other/"):
            self.log.append(("other_done", p, time.time()))
            return
        m = self.mode.get(p, "ok")
        n = self.attempts.get(p, 0)
        if m == "nofile" or (m == "nofile_once" and n == 1):
            self.log.append(("lost", p, time.time()))
            return
        lines = ["안녕하세요 여러분 반갑습니다"] * 5 if m == "korean" else ENGLISH
        out = os.path.splitext(p)[0] + ".subgen.small.ko.srt"
        with open(out, "w") as fh:
            for k in range(240):
                s, e = k * 5, k * 5 + 3
                fh.write(f"{k + 1}\n00:{s // 60:02}:{s % 60:02},000 --> 00:{e // 60:02}:{e % 60:02},000\n{lines[k % 5]}\n\n")
        self.log.append(("written", p, time.time()))

    def queue(self):
        self._advance()
        return {"queued": [{"path": p, "type": "transcribe"} for p in self.queued],
                "processing": [{"path": p, "type": "transcribe"} for p in self.processing],
                "idle": not self.queued and not self.processing, "version": "2026.08.1", "model": "small"}

    def submit(self, path, lang, task):
        self.submits.append((path, lang, task))
        self.log.append(("submit", path, time.time()))
        if self.mode.get(path) == "refuse":
            return 403, {"walked": 0, "error": "directory is outside the allowed media root"}
        self.attempts[path] = self.attempts.get(path, 0) + 1
        self.queued.append(path)
        self.ticks[path] = 200 if self.mode.get(path) == "slow" else 3
        self.max_in_flight = max(self.max_in_flight, len(self.queued) + len(self.processing))
        return 200, {"walked": 1, "queued": 1, "skipped": 0}

    def cancel(self, path):
        self.cancels.append(path)
        if path in self.queued:
            self.queued.remove(path)
            return {"cancelled": True}
        return {"cancelled": False, "reason": "already processing"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeSubgen()
    monkeypatch.setattr(subgen, "Client", lambda *a, **k: f)
    return f


def drain(limit=20):
    for _ in range(limit):
        a = subgen._next_action()
        if not a:
            return
        subgen.run_action(a["id"])


def batch(iid):
    subgen.refresh_item(iid)
    return db.scalar("SELECT id FROM actions WHERE kind='subgen_transcribe' AND status='proposed' ORDER BY id DESC")


def files_of(aid):
    return json.loads(db.scalar("SELECT payload FROM actions WHERE id=?", (aid,)))["files"]


# ---------------------------------------------------------------- acceptance: who gets a proposal

def test_korean_episode_translates_from_korean(lib):
    iid = show()
    fid = episode(lib, iid, "S04E01", [T(0, "kor", default=True)])
    p, why = plan(fid)
    assert p and p["task"] == "translate" and p["language"] == "ko", why


def test_detected_language_wins_over_the_tag(lib):
    iid = show()
    fid = episode(lib, iid, "S09E03", [T(0, "kor", tag="eng", default=True)])  # tagged English, Korean in every window
    p, _ = plan(fid)
    assert p["language"] == "ko" and p["overridden"] and p["task"] == "translate"


def test_english_show_is_transcribed(lib):
    iid = show("Bob's Burgers", "eng")
    fid = episode(lib, iid, "S16E02", [T(0, "eng", default=True)])
    p, _ = plan(fid)
    assert p["task"] == "transcribe" and p["language"] == "en"


def test_existing_external_english_means_no_proposal(lib):
    iid = show("NCIS", "eng")
    fid = episode(lib, iid, "S23E02", [T(0, "eng", default=True)], subs=[S("eng", external=True)])
    assert plan(fid)[0] is None and "already has" in plan(fid)[1]


def test_embedded_english_means_no_proposal_and_a_correct_whisper_message(lib):
    iid = show("Frog and Toad", "eng")
    fid = episode(lib, iid, "S02E06", [T(0, "ger", default=True), T(1, "eng")], subs=[S(), S()])
    assert plan(fid)[0] is None
    assert "already has English subtitles" in actions.whisper_blocker(fid)


def test_almost_no_speech_means_no_proposal(lib):
    iid = show("Baraka", "eng")
    fid = episode(lib, iid, "film", [T(0, "eng", default=True)], codes=("subs_missing", "no_dialogue"))
    assert plan(fid)[0] is None and "speech" in plan(fid)[1]


def test_unconfident_audio_or_wrong_track_choice_means_no_proposal(lib):
    iid = show()
    a = episode(lib, iid, "S01E01", [T(0, "kor", default=True, agree=False)])
    assert "confidently" in plan(a)[1]
    # dual audio: Korean is track 2 but tagged 'und' -> subgen would take track 1 (English)
    b = episode(lib, iid, "S01E02", [T(0, "eng", default=True), T(1, "kor", tag="und")])
    assert "subgen would transcribe audio track 1" in plan(b)[1]


def test_automatic_proposals_wait_for_a_failed_search_and_skip_new_episodes(lib):
    iid = show()
    fid = episode(lib, iid, "S04E01", [T(0, "kor", default=True)])
    assert "hasn't failed" in plan(fid, manual=False)[1]
    failed_before(iid, fid)
    assert plan(fid, manual=False)[0]
    new = episode(lib, iid, "S04E09", [T(0, "kor", default=True)], date_added=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    assert "new episode" in plan(new, manual=False)[1]


def test_proposals_replace_resubs_and_are_not_duplicated(lib, fake):
    iid = show()
    fids = [episode(lib, iid, f"S03E0{k}", [T(0, "kor", default=True)], season=3) for k in (1, 2, 3)]
    failed_before(iid, fids[0])
    resub = actions._propose(fids[1], "resub", "Issues: subs_missing", ["subs_missing"])
    aid = batch(iid)
    assert len(files_of(aid)) == 3
    assert db.scalar("SELECT status FROM actions WHERE id=?", (resub,)) == "withdrawn"
    assert not actions.propose_for_file(fids[2])  # subgen covers it: no new resub/Whisper proposal
    assert subgen.approve(aid)["ok"]
    subgen.refresh_item(iid)  # files are queued now: no second proposal
    assert db.scalar("SELECT COUNT(*) FROM actions WHERE kind='subgen_transcribe' AND status='proposed'") == 0


# ---------------------------------------------------------------- the worker: one file at a time

def _approved(lib, n=3, modes=None, fake=None, iid=None, season=3):
    iid = iid or show()
    fids = [episode(lib, iid, f"S0{season}E{k:02}", [T(0, "kor", default=True)], season=season) for k in range(1, n + 1)]
    aid = batch(iid) if False else None
    subgen.request(fids)
    aid = db.scalar("SELECT id FROM actions WHERE kind='subgen_transcribe' AND status='proposed' ORDER BY id DESC")
    for k, m in (modes or {}).items():
        fake.mode[db.scalar("SELECT local_path FROM files WHERE id=?", (fids[k],))] = m
    assert subgen.approve(aid)["ok"]
    return aid, fids


def test_strictly_one_file_in_flight_and_renamed_to_en_srt(lib, fake):
    aid, fids = _approved(lib, 3, fake=fake)
    drain()
    assert fake.max_in_flight == 1
    subs = [e for e in fake.log if e[0] in ("submit", "written")]
    assert [e[0] for e in subs] == ["submit", "written"] * 3  # next file only after the previous .srt exists
    assert all(task == "translate" and lang == "ko" for _, lang, task in fake.submits)
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    assert a["status"] == "done" and "3 of 3 done" in a["result"]
    for fid in fids:
        stem = os.path.splitext(db.scalar("SELECT local_path FROM files WHERE id=?", (fid,)))[0]
        assert os.path.exists(stem + ".en.srt") and not os.path.exists(stem + ".subgen.small.ko.srt")
    e = files_of(aid)[0]
    assert e["machine_generated"] and e["check"]["english_score"] > 0.3
    assert db.scalar("SELECT COUNT(*) FROM queue WHERE source='subgen'") == 3  # each file rescanned


def test_waits_for_another_clients_job_without_spending_the_budget(lib, fake):
    settings.save({**settings.get(), "subgen": {**settings.get()["subgen"], "timeout_factor": 0.00001, "timeout_min_s": 0.05}})
    fake.processing.append("/other/bazarr-job.mkv")
    fake.ticks["/other/bazarr-job.mkv"] = 300  # ~0.3 s of someone else's job vs a 0.05 s budget
    aid, _ = _approved(lib, 1, fake=fake)
    drain()
    kinds = [e[0] for e in fake.log]
    assert kinds.index("other_done") < kinds.index("submit")  # never submitted while subgen was busy
    assert files_of(aid)[0]["status"] == "done"  # ~30 polls of waiting didn't use up the tiny budget


def test_time_budget_exceeded_cancels_and_moves_on(lib, fake):
    settings.save({**settings.get(), "subgen": {**settings.get()["subgen"], "timeout_factor": 0.00001, "timeout_min_s": 0.02}})
    aid, fids = _approved(lib, 2, modes={0: "slow"}, fake=fake)
    drain()
    fs = files_of(aid)
    assert fs[0]["status"] == "failed" and "timeout" in fs[0]["outcome"] and fake.cancels
    assert fs[1]["status"] in ("done", "failed") and fs[1]["status"] != "waiting"
    assert fake.max_in_flight == 1  # the stuck job finished before the next file went in


def test_three_failures_in_a_row_stop_the_batch(lib, fake):
    aid, _ = _approved(lib, 5, modes={0: "korean", 1: "korean", 2: "korean"}, fake=fake)
    drain()
    fs = files_of(aid)
    assert [e["status"] for e in fs] == ["failed"] * 3 + ["not attempted"] * 2
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    assert a["status"] == "failed" and "3 failures in a row" in a["result"]
    assert subgen.control(aid, "resume")["ok"]
    assert [e["status"] for e in files_of(aid)][3:] == ["waiting", "waiting"]


def test_wrong_language_output_fails_and_is_left_alone(lib, fake):
    aid, fids = _approved(lib, 1, modes={0: "korean"}, fake=fake)
    drain()
    e = files_of(aid)[0]
    assert e["status"] == "failed" and "isn't English" in e["outcome"]
    stem = os.path.splitext(db.scalar("SELECT local_path FROM files WHERE id=?", (fids[0],)))[0]
    assert os.path.exists(stem + ".subgen.small.ko.srt") and not os.path.exists(stem + ".en.srt")


def test_lost_job_is_retried_once(lib, fake):
    aid, fids = _approved(lib, 2, modes={0: "nofile_once", 1: "nofile"}, fake=fake)
    drain()
    fs = files_of(aid)
    assert fs[0]["status"] == "done" and fs[0]["retried"]
    assert fs[1]["status"] == "failed" and "lost" in fs[1]["outcome"]
    assert len(fake.submits) == 4


def test_path_refused_by_subgen_fails_without_retrying(lib, fake):
    aid, _ = _approved(lib, 1, modes={0: "refuse"}, fake=fake)
    drain()
    e = files_of(aid)[0]
    assert e["status"] == "failed" and "outside the allowed media root" in e["outcome"] and len(fake.submits) == 1


def test_restart_mid_job_waits_instead_of_resubmitting(lib, fake):
    aid, fids = _approved(lib, 1, fake=fake)
    path = db.scalar("SELECT local_path FROM files WHERE id=?", (fids[0],))
    # Anotherr went down right after submitting: subgen still has the job
    p = json.loads(db.scalar("SELECT payload FROM actions WHERE id=?", (aid,)))
    p["files"][0].update(status="submitted", submitted_at=time.time())
    db.x("UPDATE actions SET status='running', payload=? WHERE id=?", (json.dumps(p), aid))
    fake.processing.append(path)
    fake.ticks[path] = 3
    drain()
    assert fake.submits == [] and files_of(aid)[0]["status"] == "done"


def test_two_batches_run_in_approval_order(lib, fake):
    a1, f1 = _approved(lib, 2, fake=fake, season=3)
    a2, f2 = _approved(lib, 2, fake=fake, season=4)
    drain()
    order = [p for p, _, _ in fake.submits]
    paths = [db.scalar("SELECT local_path FROM files WHERE id=?", (f,)) for f in f1 + f2]
    assert order == paths and fake.max_in_flight == 1


def test_pilot_pauses_after_the_first_file(lib, fake):
    settings.state_set("subgen_pilot_done", False)
    aid, _ = _approved(lib, 3, fake=fake)
    drain()
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    assert a["status"] == "queued" and "Pilot" in a["result"]
    assert [e["status"] for e in files_of(aid)] == ["done", "waiting", "waiting"]
    subgen.control(aid, "resume")
    drain()
    assert db.scalar("SELECT status FROM actions WHERE id=?", (aid,)) == "done"


def test_cancel_a_queued_batch(lib, fake):
    aid, _ = _approved(lib, 2, fake=fake)
    assert subgen.control(aid, "cancel")["ok"]
    a = db.q1("SELECT * FROM actions WHERE id=?", (aid,))
    assert a["status"] == "failed" and all(e["status"] == "not attempted" for e in files_of(aid))
    drain()
    assert fake.submits == []


# ---------------------------------------------------------------- a sidecar on another Unraid disk

MINE = "1\n00:00:01,000 --> 00:00:02,000\nHi\n"


def split_across_disks(lib, fid):
    """The video's physical copy (local_path, e.g. on the cache) is in one folder; its user-share path (arr_path)
    shows another folder that also holds the old .en.srt (e.g. from disk3)."""
    local = db.scalar("SELECT local_path FROM files WHERE id=?", (fid,))
    share_dir = lib / "user-share" / str(fid)
    share_dir.mkdir(parents=True)
    share = share_dir / os.path.basename(local)
    stem = os.path.splitext(os.path.basename(local))[0]
    (share_dir / f"{stem}.en.srt").write_text(MINE)
    db.x("UPDATE files SET arr_path=? WHERE id=?", (str(share), fid))
    return str(share_dir / f"{stem}.en.srt")


def test_path_mode_skips_a_file_whose_en_srt_is_on_another_disk(lib, fake):
    aid, fids = _approved(lib, 2, fake=fake)
    srt = split_across_disks(lib, fids[0])
    drain()
    fs = files_of(aid)
    assert fs[0]["status"] == "done" and "already exists" in fs[0]["outcome"] and srt in fs[0]["outcome"]
    paths = [p for p, _, _ in fake.submits]
    assert paths == [db.scalar("SELECT local_path FROM files WHERE id=?", (fids[1],))]  # only the other file went
    with open(srt) as fh:
        assert fh.read() == MINE  # untouched
