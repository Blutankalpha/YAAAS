#!/usr/bin/env python3
"""YAAAS: grab whole albums from YouTube Music and build word-synced lyrics.

Each input line is either "Artist - Title", "Title - Artist" or a YouTube /
YouTube Music link (video, album or playlist). A .json file works too: a list
of objects with title, artist, album and optionally kind ("track" or "album"). Lyric text comes from lrclib, vocals are split out
with Demucs, and WhisperX's forced aligner puts a timestamp on every word.
Output is an enhanced LRC file next to each track.

When lrclib has nothing for a song, the lyrics are transcribed from the
isolated vocals with Whisper instead (turn off with --no-transcribe).

With --scan it skips the downloading and instead walks a folder tree, adding
word-synced lyrics to every audio file that has no .lrc yet.

Needs ffmpeg and:
    pip install yt-dlp ytmusicapi rapidfuzz requests mutagen numpy demucs whisperx faster-whisper

    yaaas.py songs.txt --dry-run     show what would be downloaded
    yaaas.py songs.txt -o ~/Music    show it, ask, then go
    yaaas.py --scan ~/Music          add lyrics to files that lack them
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from rapidfuzz import fuzz

LRCLIB = "https://lrclib.net/api"
HEADERS = {"User-Agent": "yaaas/1.1"}
BAD_WORDS = ("live", "remix", "karaoke", "instrumental", "cover", "acoustic",
             "sped up", "slowed", "reverb", "8d", "nightcore", "tribute")

_log = print


def set_logger(fn):
    global _log
    _log = fn


def log(msg=""):
    _log(msg)


def warn(msg):
    _log("  ! " + msg)


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


@dataclass
class PlanTrack:
    video_id: str
    title: str
    artist: str
    number: int = 1
    duration: int = None
    requested: bool = True    # asked for directly, not just part of the album
    selected: bool = True     # ticked in the GUI
    score: float = None       # fuzzy score, if it came from a text line
    source: str = ""

    @property
    def url(self):
        return "https://music.youtube.com/watch?v=" + self.video_id


@dataclass
class PlanAlbum:
    title: str
    artist: str
    year: str
    tracks: list
    key: str = ""


@dataclass
class Options:
    out: str = "music"
    fmt: str = "mp3"
    lyrics: str = "all"            # all, requested or none
    demucs_model: str = "htdemucs"
    whisper_model: str = "small"   # only used to detect the language
    language: str = None
    device: str = "auto"
    force: bool = False
    transcribe: bool = True        # make lyrics with whisper when lrclib has none
    transcribe_model: str = "auto"  # auto = large-v3 on gpu, medium on cpu


@dataclass
class Track:
    path: Path
    title: str
    artist: str
    album: str
    number: int
    duration: int
    requested: bool
    redo: bool = False   # replace an lrc that already exists


@dataclass
class LocalTrack:
    """An audio file already on disk (folder mode)."""
    path: Path
    title: str
    artist: str
    album: str
    number: int = 0
    duration: int = None
    source: str = ""     # why it is in the list
    redo: bool = False
    selected: bool = True
    requested: bool = True
    score: float = None

    @property
    def url(self):
        return str(self.path)


@dataclass
class Line:
    text: str
    start: float = None
    end: float = None
    words: list = field(default_factory=list)


def parse_candidates(line, order="auto"):
    """Possible (artist, title) readings of a text line.

    "A - B" could be either way round, so with order="auto" both are returned
    and the caller keeps whichever matches better.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return []
    for sep in ("\t", "|", " - ", " \u2013 ", " \u2014 "):
        if sep in line:
            a, b = (x.strip() for x in line.split(sep, 1))
            if order == "artist-title":
                return [(a, b)]
            if order == "title-artist":
                return [(b, a)]
            return [(a, b), (b, a)]
    m = re.match(r"^(.*?)\s+by\s+(.*)$", line, re.I)
    if m:
        return [(m.group(2).strip(), m.group(1).strip())]
    return [("", line)]


def looks_like_json(path, text):
    if str(path).lower().endswith(".json"):
        return True
    if text.lstrip().startswith(("[", "{")):
        try:
            json.loads(text)
            return True
        except ValueError:
            pass
    return False


def parse_json_entries(text):
    """Entries from a json list like [{"title": ..., "artist": ..., "album": ..., "kind": "track"}].

    Returns (entries, skipped). Raises ValueError if it isn't json or has no list in it.
    """
    data = json.loads(text)
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list):
        raise ValueError("expected a json list of objects")

    def text_of(v):
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        return str(v or "").strip()

    entries, skipped = [], 0
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue
        kind = "album" if text_of(item.get("kind")).lower() in ("album", "release", "release_group") else "track"
        title = text_of(item.get("title") or item.get("name"))
        album = text_of(item.get("album"))
        if not (title or (kind == "album" and album)):
            skipped += 1
            continue
        entries.append({"kind": kind, "title": title, "artist": text_of(item.get("artist")), "album": album,
                        "mbid": text_of(item.get("recording_mbid")), "url": text_of(item.get("musicbrainz_url"))})
    return entries, skipped


def entry_line(e):
    """The text line an imported entry is shown as."""
    name = (e["album"] or e["title"]) if e["kind"] == "album" else e["title"]
    return f"{e['artist']} - {name}" if e["artist"] else name


def strip_details(title):
    """'Song (live, 2003: Sheffield) [remaster]' -> 'Song'"""
    return re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()


def norm(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
    s = re.sub(r"\s-\s.*(remaster|version|mono|stereo|edit|mix).*$", "", s)
    s = re.sub(r"[^a-z0-9\u0080-\uffff]+", " ", s)
    return s.strip()


def has_word(word, text):
    return re.search(r"\b" + re.escape(word) + r"\b", text) is not None


def score_candidate(c, artist, title, album=None):
    ctitle = c.get("title", "")
    names = " ".join(a["name"] for a in c.get("artists") or [])
    if artist:
        t = (fuzz.token_sort_ratio(norm(title), norm(ctitle)) +
             fuzz.token_set_ratio(norm(title), norm(ctitle))) / 2
        a = fuzz.token_set_ratio(norm(artist), norm(names))
        s = 0.65 * t + 0.35 * a
    else:
        s = fuzz.token_set_ratio(norm(title), norm(names + " " + ctitle))
    if album:
        calbum = (c.get("album") or {}).get("name", "")
        s = 0.9 * s + 0.1 * (fuzz.token_set_ratio(norm(album), norm(calbum)) if calbum else 0)
    query = (artist + " " + title).lower()
    for w in BAD_WORDS:
        in_title, in_query = has_word(w, ctitle.lower()), has_word(w, query)
        if in_title and not in_query:
            s -= 25
        elif in_title and in_query:
            s += 3
    return s


def find_match(yt, artist, title, album=None):
    queries = [(artist + " " + title).strip()]
    if artist:
        queries.append(title)
    best = None
    for q in queries:
        try:
            results = yt.search(q, filter="songs", limit=10)
        except Exception as e:
            warn(f"search failed for '{q}': {e}")
            continue
        for r in results:
            if r.get("videoId"):
                s = score_candidate(r, artist, title, album)
                if best is None or s > best[0]:
                    best = (s, r)
        if best and best[0] >= 95:
            break
    return best


def find_album(yt, artist, album):
    best = None
    for q in dict.fromkeys([f"{artist} {album}".strip(), album]):
        try:
            results = yt.search(q, filter="albums", limit=10)
        except Exception as e:
            warn(f"album search failed for '{q}': {e}")
            continue
        for r in results:
            if not r.get("browseId"):
                continue
            t = fuzz.token_set_ratio(norm(album), norm(r.get("title", "")))
            a = fuzz.token_set_ratio(norm(artist), norm(names_of(r.get("artists")))) if artist else t
            s = 0.65 * t + 0.35 * a
            if best is None or s > best[0]:
                best = (s, r)
    return best


def names_of(artists):
    return ", ".join(a["name"] for a in artists or [] if a.get("name"))


def parse_duration(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        secs = 0
        for part in str(v).split(":"):
            secs = secs * 60 + int(part)
        return secs
    except ValueError:
        return None


def is_url(s):
    return bool(re.match(r"^(https?://|(www\.|music\.|m\.)?youtube\.com|youtu\.be)", s.strip(), re.I))


def classify_url(url):
    """Returns (kind, id) with kind one of album, albumplaylist, playlist, video."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    u = urlparse(url)
    host = u.netloc.lower()
    if not (host == "youtu.be" or host.endswith("youtube.com")):
        return None
    qs = parse_qs(u.query)
    m = re.match(r"^/browse/(MPRE[\w-]+)", u.path)
    if m:
        return "album", m.group(1)
    lst = (qs.get("list") or [None])[0]
    if lst and lst.startswith("OLAK5uCy"):
        return "albumplaylist", lst
    if lst and not lst.startswith(("RD", "LM", "WL")):
        return "playlist", lst
    vid = (qs.get("v") or [None])[0]
    if not vid and host == "youtu.be":
        vid = u.path.strip("/") or None
    if not vid:
        m = re.match(r"^/(?:shorts|embed|live)/([\w-]{11})", u.path)
        vid = m.group(1) if m else None
    return ("video", vid) if vid else None


def clean_channel(name):
    return re.sub(r"\s*-\s*Topic$", "", name or "").strip()


def video_meta(video_id):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/watch?v=" + video_id, download=False)
    title = info.get("track") or info.get("title") or video_id
    artist = info.get("artist") or clean_channel(info.get("channel") or info.get("uploader"))
    if not info.get("track") and " - " in title:
        a, t = title.split(" - ", 1)
        artist = a.strip()
        title = re.sub(r"\s*[\(\[](official|lyric|audio|video).*?[\)\]]", "", t, flags=re.I).strip()
    return {"title": title, "artist": artist or "Unknown", "duration": parse_duration(info.get("duration"))}


def plan_playlist(list_id):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/playlist?list=" + list_id, download=False)
    tracks = []
    for i, e in enumerate(info.get("entries") or [], 1):
        if not e or not e.get("id") or re.match(r"^\[(Private|Deleted) video\]", e.get("title") or ""):
            continue
        artist = clean_channel(e.get("channel") or e.get("uploader"))
        title = e.get("title") or e["id"]
        if artist and title.lower().startswith(artist.lower() + " - "):
            title = title[len(artist) + 3:]
        tracks.append(PlanTrack(e["id"], title, artist or "Unknown", i,
                                parse_duration(e.get("duration")), source="playlist"))
    artists = {t.artist for t in tracks}
    return PlanAlbum(info.get("title") or "Playlist",
                     artists.pop() if len(artists) == 1 else "Various Artists",
                     None, tracks, key="playlist:" + list_id)


def make_album_plan(yt, key, titles=(), vids=(), all_requested=False):
    info = yt.get_album(key)
    tracks = []
    for i, t in enumerate(info.get("tracks", []), 1):
        vid, title = t.get("videoId"), t.get("title", f"Track {i}")
        if not vid:
            warn(f"'{title}' isn't available, skipping")
            continue
        wanted = all_requested or vid in vids or any(
            fuzz.token_set_ratio(norm(x), norm(title)) >= 90 for x in titles)
        artist = names_of(t.get("artists")) or names_of(info.get("artists")) or "Unknown"
        tracks.append(PlanTrack(vid, title, artist, t.get("trackNumber") or i,
                                t.get("duration_seconds"), requested=wanted))
    return PlanAlbum(info["title"], names_of(info.get("artists")) or "Unknown",
                     info.get("year"), tracks, key=key)


def build_plan(yt, inputs, threshold=70, expand_album=True, order="auto"):
    """Turn text lines and links into albums/tracks. Nothing is downloaded here."""
    pending = {}   # album browse id -> what we know about it
    direct = []    # singles and playlists
    unmatched = []

    def add_pending(key, titles=(), vids=(), all_req=False, note=None):
        p = pending.setdefault(key, {"titles": [], "vids": set(), "all": False, "notes": []})
        p["titles"] += list(titles)
        p["vids"] |= set(vids)
        p["all"] |= all_req
        if note:
            p["notes"].append(note)

    def add_single(vid, title, artist, duration, source, score=None):
        track = PlanTrack(vid, title, artist, 1, duration, source=source, score=score)
        direct.append(PlanAlbum(title, artist, None, [track], key="single:" + vid))

    items = []
    for r in inputs:
        if isinstance(r, dict):
            items.append(r)
        elif r.strip() and not r.strip().startswith("#"):
            items.append(r.strip())
    for n, raw in enumerate(items, 1):
        entry = raw if isinstance(raw, dict) else None   # entries come from a json import
        label = entry_line(entry) if entry else raw
        log(f"[{n}/{len(items)}] {label}")
        try:
            if entry is None and is_url(raw):
                kind = classify_url(raw)
                if not kind:
                    warn("not a link I can use")
                    unmatched.append(raw)
                    continue
                k, val = kind
                if k == "albumplaylist":
                    try:
                        bid = yt.get_album_browse_id(val)
                    except Exception:
                        bid = None
                    k, val = ("album", bid) if bid else ("playlist", val)
                if k == "album":
                    add_pending(val, all_req=True)
                elif k == "playlist":
                    pl = plan_playlist(val)
                    if pl.tracks:
                        direct.append(pl)
                        log(f"    playlist '{pl.title}', {len(pl.tracks)} tracks")
                    else:
                        warn("playlist is empty or private")
                        unmatched.append(raw)
                else:
                    meta, album = None, None
                    try:
                        wp = yt.get_watch_playlist(videoId=val, limit=1)
                        t0 = (wp.get("tracks") or [None])[0]
                        if t0:
                            meta = {"title": t0.get("title") or val,
                                    "artist": names_of(t0.get("artists")) or "Unknown",
                                    "duration": parse_duration(t0.get("length"))}
                            album = t0.get("album")
                    except Exception as e:
                        warn(f"YouTube Music lookup failed ({e}), using plain video info")
                    if meta is None:
                        meta = video_meta(val)
                    if expand_album and album and album.get("id"):
                        log(f"    part of album '{album.get('name')}'")
                        add_pending(album["id"], titles=[meta["title"]], vids=[val],
                                    note=(val, meta["title"], "from link", None))
                    else:
                        add_single(val, meta["title"], meta["artist"], meta["duration"], "from link")
            elif entry is not None and entry["kind"] == "album":
                m = find_album(yt, entry["artist"], entry["album"] or entry["title"])
                if not m or m[0] < threshold:
                    warn(f"no confident album match (best {m[0]:.0f})" if m else "no album found")
                    unmatched.append(label)
                    continue
                score, hit = m
                log(f"    album: {names_of(hit.get('artists'))} - {hit['title']}  score {score:.0f}")
                add_pending(hit["browseId"], all_req=True)
            else:
                album_hint = None
                if entry is not None:
                    attempts = [[(entry["artist"], entry["title"])]]
                    short = strip_details(entry["title"])
                    if short and short != entry["title"]:
                        attempts.append([(entry["artist"], short)])   # e.g. no live version exists, take the studio one
                    album_hint = entry["album"] or None
                else:
                    attempts = [parse_candidates(raw, order)]
                    if not attempts[0]:
                        continue
                m, used = None, 0
                for used, readings in enumerate(attempts):
                    for artist, title in readings:
                        found = find_match(yt, artist, title, album_hint)
                        if found and (m is None or found[0] > m[0]):
                            m = found
                    if m and m[0] >= threshold:
                        break
                if not m or m[0] < threshold:
                    warn(f"no confident match (best {m[0]:.0f})" if m else "no results")
                    unmatched.append(label)
                    continue
                if used:
                    log("    matched without the bracketed part of the title")
                source = f"matched '{label}'" + (" (bracketed part dropped)" if used else "")
                score, song = m
                album = song.get("album") or {}
                log(f"    {names_of(song.get('artists'))} - {song['title']}  "
                    f"[{album.get('name', 'no album')}]  score {score:.0f}")
                if album.get("id"):
                    add_pending(album["id"], titles=[song["title"]], vids=[song["videoId"]],
                                note=(song["videoId"], song["title"], source, score))
                else:
                    add_single(song["videoId"], song["title"], names_of(song.get("artists")) or "Unknown",
                               song.get("duration_seconds"), source, score)
        except Exception as e:
            warn(f"couldn't resolve: {e}")
            unmatched.append(label)

    albums = []
    for key, p in pending.items():
        log("loading album " + key)
        try:
            a = make_album_plan(yt, key, p["titles"], p["vids"], p["all"])
        except Exception as e:
            warn(f"couldn't load album: {e}")
            continue
        for vid, title, source, score in p["notes"]:
            target = next((t for t in a.tracks if t.video_id == vid), None)
            if target is None and a.tracks:
                best = max(a.tracks, key=lambda t: fuzz.token_set_ratio(norm(title), norm(t.title)))
                if fuzz.token_set_ratio(norm(title), norm(best.title)) >= 90:
                    target = best
            if target and not (target.score is not None and score is None):
                target.source, target.score = source, score
        if a.tracks:
            albums.append(a)
    return albums + direct, unmatched


def print_plan(albums):
    count = sum(len(a.tracks) for a in albums)
    log(f"\n{count} track(s) in {len(albums)} album(s)/single(s)")
    for a in albums:
        log(f"\n{a.artist} - {a.title}" + (f" ({a.year})" if a.year else ""))
        for t in a.tracks:
            mark = "*" if t.requested else " "
            extra = ""
            if t.source:
                extra = "   [" + t.source + (f", {t.score:.0f}%" if t.score is not None else "") + "]"
            log(f"  {mark}{t.number:02d} {t.title}  {t.url}{extra}")
    log("\n* = asked for directly")


def safe(name):
    name = re.sub(r'[<>:"/\\|?*%\x00-\x1f]', "_", name).strip(" .")
    return name or "_"


def download_track(video_id, base, fmt):
    import yt_dlp
    final = Path(f"{base}.{fmt}")
    if final.exists():
        return final
    pp = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
    if fmt == "mp3":
        pp["preferredquality"] = "0"
    opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{base}.%(ext)s",
        "quiet": True, "no_warnings": True, "noprogress": True,
        "noplaylist": True, "retries": 5,
        "postprocessors": [pp],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download(["https://music.youtube.com/watch?v=" + video_id])
    if not final.exists():
        raise RuntimeError(f"expected {final} but it isn't there")
    return final


def tag_file(path, **tags):
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is None:
            return
        if f.tags is None:
            f.add_tags()
        for k, v in tags.items():
            f[k] = [str(v)]
        f.save()
    except Exception as e:
        warn(f"couldn't tag {path.name}: {e}")


def lrclib_fetch(artist, title, album, duration):
    def usable(d):
        return bool(d) and not d.get("instrumental") and (d.get("syncedLyrics") or d.get("plainLyrics"))

    try:
        params = {"artist_name": artist, "track_name": title, "album_name": album}
        if duration:
            params["duration"] = duration
        r = requests.get(LRCLIB + "/get", params=params, headers=HEADERS, timeout=20)
        if r.ok:
            if r.json().get("instrumental"):
                return {"instrumental": True}
            if usable(r.json()):
                return r.json()
    except requests.RequestException as e:
        warn(f"lrclib lookup failed: {e}")

    short = re.sub(r"\(.*?\)|\[.*?\]", "", title).strip() or title
    for params in ({"track_name": short, "artist_name": artist}, {"q": f"{artist} {short}"}):
        try:
            r = requests.get(LRCLIB + "/search", params=params, headers=HEADERS, timeout=20)
            found = [d for d in r.json() if usable(d)] if r.ok else []
        except (requests.RequestException, ValueError):
            continue
        if duration:
            found = [d for d in found if abs((d.get("duration") or 0) - duration) <= 4]
        if found:
            found.sort(key=lambda d: (bool(d.get("syncedLyrics")),
                                      fuzz.token_set_ratio(norm(title), norm(d.get("trackName", ""))),
                                      fuzz.token_set_ratio(norm(artist), norm(d.get("artistName", "")))),
                       reverse=True)
            return found[0]
    return None


TS_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
WORD_TAG_RE = re.compile(r"<\d+:\d+(?:\.\d+)?>")


def parse_synced(text):
    entries = []
    for raw in text.splitlines():
        stamps = TS_RE.findall(raw)
        if not stamps:
            continue
        body = WORD_TAG_RE.sub("", TS_RE.sub("", raw)).strip()
        for m, s in stamps:
            entries.append((int(m) * 60 + float(s), body))
    entries.sort(key=lambda e: e[0])
    lines = []
    for i, (t, body) in enumerate(entries):
        if body:  # blank stamps only mark where the previous line ends
            end = entries[i + 1][0] if i + 1 < len(entries) else None
            lines.append(Line(body, t, end))
    return lines


def parse_plain(text):
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if raw and not re.fullmatch(r"\[.*\]", raw):
            lines.append(Line(raw))
    return lines


def pick_device(requested):
    if requested != "auto":
        return requested
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def free_gpu(models):
    """Drop the cached whisper / alignment models so demucs gets the GPU's memory to itself."""
    import gc
    _whisper.clear()
    models.clear()
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _run_demucs(audio, workdir, model, device, extra, cancel):
    cmd = [sys.executable, "-m", "demucs.separate", "-n", model, "--two-stems", "vocals",
           "-d", device, *extra, "-o", str(workdir), str(audio)]
    env = dict(os.environ, PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
    errlog = workdir / "demucs.log"
    with open(errlog, "w") as ef:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=ef, env=env)
        while True:
            try:
                rc = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    proc.terminate()
                    proc.wait()
                    raise Cancelled()
    return rc, errlog.read_text(errors="ignore")


# Once demucs has had to fall back, later tracks start from there instead of failing the same way again.
_demucs = {"level": 0, "free_first": False}


def separate_vocals(audio, workdir, model, device, cancel=None, free=None):
    """Split out the vocals. On a GPU out-of-memory error: free cached models, then smaller chunks, then cpu."""
    levels = [(device, []), (device, ["--segment", "4"]), ("cpu", [])] if device == "cuda" else [("cpu", [])]
    level = min(_demucs["level"], len(levels) - 1)
    freed = False
    if _demucs["free_first"] and free:
        free()
    while True:
        dev, extra = levels[level]
        rc, text = _run_demucs(audio, workdir, model, dev, extra, cancel)
        if rc == 0:
            break
        if "out of memory" not in text.lower() or level == len(levels) - 1:
            raise RuntimeError(f"demucs exited with {rc}: {text[-600:]}")
        if level == 0 and not freed:
            freed = True
            log("  gpu out of memory, freeing cached models and trying again")
            if free:
                free()
        else:
            level += 1
            log("  still out of memory, " + ("using smaller chunks" if level == 1 else "running demucs on the cpu"))
    if freed and level == 0:
        _demucs["free_first"] = True   # freeing was enough; do it up front from now on
    _demucs["level"] = max(_demucs["level"], level)
    vocals = workdir / model / audio.stem / "vocals.wav"
    if not vocals.exists():
        raise RuntimeError("demucs didn't produce vocals.wav")
    return vocals


# languages written in a non-latin script
NON_LATIN = {"ja", "zh", "ko", "ar", "fa", "he", "el", "ru", "uk", "hi", "ur", "te", "ka", "ml", "bg", "mr",
             "ta", "bn", "th", "sr", "mk", "be", "kk", "hy", "ne", "si", "km", "lo", "my", "ps", "yue"}

_whisper = {}


def align_languages():
    """Languages whisperx has an alignment model for (None if we can't tell)."""
    try:
        from whisperx.alignment import DEFAULT_ALIGN_MODELS_HF, DEFAULT_ALIGN_MODELS_TORCH
        return set(DEFAULT_ALIGN_MODELS_TORCH) | set(DEFAULT_ALIGN_MODELS_HF)
    except Exception:
        return None


def latin_ratio(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if c.isascii() or "LATIN" in unicodedata.name(c, "")) / len(letters)


def rank_languages(totals, text, supported):
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    if supported:
        ranked = [kv for kv in ranked if kv[0] in supported] or ranked
    if text:
        latin = latin_ratio(text) > 0.8
        ranked = [kv for kv in ranked if (kv[0] not in NON_LATIN) == latin] or ranked
    return ranked


def detect_language(audio, device, size, text=None, alignable=True):
    # Whisper often guesses wrong on singing, so average a few loud windows
    # and only accept languages we can actually align.
    import numpy as np
    from faster_whisper import WhisperModel
    if "m" not in _whisper:
        _whisper["m"] = WhisperModel(size, device=device,
                                     compute_type="float16" if device == "cuda" else "int8")
    sr, win = 16000, 30 * 16000
    if len(audio) <= win:
        clips = [audio]
    else:
        blocks = np.abs(audio[: len(audio) // sr * sr]).reshape(-1, sr).mean(1)
        sums = np.convolve(blocks, np.ones(30), "valid")
        clips, taken = [], []
        for idx in np.argsort(-sums):
            idx = int(idx)
            if all(abs(idx - t) >= 30 for t in taken):
                taken.append(idx)
                clips.append(audio[idx * sr: idx * sr + win])
            if len(clips) == 3:
                break

    totals = {}
    for clip in clips:
        _, info = _whisper["m"].transcribe(clip, beam_size=1)
        probs = getattr(info, "all_language_probs", None) or [(info.language, info.language_probability)]
        for lang, p in probs:
            totals[lang] = totals.get(lang, 0.0) + p / len(clips)

    raw_top = max(totals, key=totals.get)
    lang, p = rank_languages(totals, text, align_languages() if alignable else None)[0]
    if lang != raw_top:
        log(f"  whisper guessed '{raw_top}', which can't be aligned here; using '{lang}'")
    log(f"  language: {lang} ({p:.0%})")
    return lang


def transcribe_lines(audio, device, opts, lang, cancel=None):
    """Write lyrics from the isolated vocals. Returns lines that already carry whisper's word times."""
    from faster_whisper import WhisperModel
    name = opts.transcribe_model
    if name == "auto":
        name = "large-v3" if device == "cuda" else "medium"
    key = "t:" + name
    if key not in _whisper:
        log(f"  loading whisper '{name}' (first use downloads it)")
        _whisper[key] = WhisperModel(name, device=device,
                                     compute_type="float16" if device == "cuda" else "int8")
    segments, _ = _whisper[key].transcribe(audio, language=lang, beam_size=5, word_timestamps=True,
                                           vad_filter=True, condition_on_previous_text=False)
    lines = []
    for seg in segments:
        check_cancel(cancel)
        if seg.no_speech_prob > 0.6 or seg.compression_ratio > 2.4:  # silence or a hallucinated loop
            continue
        words = []
        for w in seg.words or []:
            word = re.sub(r"\s+", "", w.word)
            if word:
                words.append({"word": word, "start": w.start, "end": w.end})
        if words:
            lines.append(Line(" ".join(w["word"] for w in words), seg.start, seg.end, words))
    return lines


def even_words(text, lo, hi):
    ws = text.split()
    step = (hi - lo) / max(len(ws), 1)
    return [{"word": w, "start": lo + i * step, "end": lo + (i + 1) * step} for i, w in enumerate(ws)]


def fill_gaps(words, lo, hi):
    """Spread untimed words (numbers, symbols) between their timed neighbours."""
    n, i = len(words), 0
    while i < n:
        if words[i].get("start") is None or words[i].get("end") is None:
            j = i
            while j < n and (words[j].get("start") is None or words[j].get("end") is None):
                j += 1
            a = words[i - 1]["end"] if i > 0 else lo
            b = max(a, words[j]["start"] if j < n else hi)
            step = (b - a) / (j - i)
            for k in range(i, j):
                words[k]["start"] = a + step * (k - i)
                words[k]["end"] = a + step * (k - i + 1)
            i = j
        else:
            i += 1


def clean_words(raw, text):
    if len(raw) != len(text.split()):
        return None
    return [{"word": w["word"], "start": w.get("start"), "end": w.get("end")} for w in raw]


def align_lines(lines, audio, duration, lang, device, models):
    import whisperx
    if lang not in models:
        models[lang] = whisperx.load_align_model(language_code=lang, device=device)
    model, meta = models[lang]

    def align_one(text, start, end, cap=30):
        """Words with timings for one chunk of text, or None if whisperx couldn't place it."""
        if cap:
            end = min(end, start + cap)
        seg = {"text": text, "start": start, "end": end}
        res = whisperx.align([seg], model, meta, audio, device, return_char_alignments=False)["segments"]
        return clean_words(res[0].get("words", []), text) if res else None

    # whisperx warns about every line it can't place. We retry those and report once instead.
    wlog = logging.getLogger("whisperx.alignment")
    old_level = wlog.level
    wlog.setLevel(logging.ERROR)
    try:
        if all(l.start is not None for l in lines):
            align_timed(lines, duration, align_one)
        else:
            # plain lyrics: aligned as one block, then cut back into lines
            full = " ".join(l.text for l in lines)
            flat = align_one(full, 0.0, duration, cap=None) or even_words(full, 0.0, duration)
            fill_gaps(flat, 0.0, duration)
            i = 0
            for l in lines:
                n = len(l.text.split())
                l.words = flat[i:i + n]
                i += n
    finally:
        wlog.setLevel(old_level)


def align_timed(lines, duration, align_one):
    windows = []
    for l in lines:
        start = max(0.0, l.start)
        end = min(duration, l.end if l.end is not None else duration)
        windows.append((start, max(end, start + 0.5)))

    # first pass uses the exact window; lines that fail get progressively wider ones
    results = [align_one(l.text, lo, hi) for l, (lo, hi) in zip(lines, windows)]
    widened = 0
    for pad in (0.5, 1.0, 2.0):
        for i, l in enumerate(lines):
            if results[i] is None:
                lo, hi = windows[i]
                results[i] = align_one(l.text, max(0.0, lo - pad), min(duration, hi + pad))
                widened += results[i] is not None

    failed = [i for i, r in enumerate(results) if r is None]
    for i, l in enumerate(lines):
        lo, hi = windows[i]
        words = results[i] or l.words or even_words(l.text, lo, hi)  # l.words = whisper's own times
        fill_gaps(words, lo, hi)
        l.words = words
    if widened:
        log(f"  {widened} line(s) only aligned with a wider window")
    if failed:
        sample = "; ".join(lines[i].text[:40] for i in failed[:3])
        warn(f"{len(failed)} of {len(lines)} lines couldn't be aligned, so their words are only "
             f"timed roughly (e.g. {sample})")
        if len(failed) > len(lines) / 2:
            warn("most lines failed, so these lyrics probably don't match this recording")


def cs_ts(cs):
    return f"{cs // 6000:02d}:{(cs % 6000) // 100:02d}.{cs % 100:02d}"


def normalize_lines(lines):
    """Round to centiseconds and make sure time never goes backwards."""
    out, prev = [], 0
    for l in lines:
        ws = []
        for w in l.words:
            word = re.sub(r"\s+", "", re.sub(r"[<>\[\]]", "", w["word"]))
            if not word:
                continue
            start = max(round(w["start"] * 100), prev)
            end = max(round(w["end"] * 100), start)
            ws.append((word, start, end))
            prev = end
        if ws:
            out.append({"text": " ".join(x[0] for x in ws), "words": ws})
    return out


def header_tag(key, value):
    value = re.sub(r"[\r\n\]]+", " ", str(value)).strip()
    return f"[{key}:{value}]"


def format_elrc(track, lines, by="YAAAS"):
    # [mm:ss.xx] <mm:ss.xx> word <mm:ss.xx> word <mm:ss.xx>
    # line start, then a start tag before every word, then the end of the last word
    head = [header_tag("ti", track.title), header_tag("ar", track.artist), header_tag("al", track.album)]
    if track.duration:
        head.append(f"[length:{track.duration // 60:02d}:{track.duration % 60:02d}]")
    head += [header_tag("by", by), header_tag("re", "YAAAS"), header_tag("ve", "1.1")]
    body = []
    for l in lines:
        ws = l["words"]
        words = " ".join(f"<{cs_ts(s)}> {w}" for w, s, _ in ws)
        body.append(f"[{cs_ts(ws[0][1])}] {words} <{cs_ts(ws[-1][2])}>")
    return "\n".join(head + body) + "\n"


ELRC_LINE = re.compile(r"^\[\d{2,}:[0-5]\d\.\d{2}\](?: <\d{2,}:[0-5]\d\.\d{2}> [^<>\[\]\s]+)+ <\d{2,}:[0-5]\d\.\d{2}>$")
HEADER = re.compile(r"^\[[A-Za-z]+:[^\]]*\]$")
STAMP = re.compile(r"[\[<](\d+):(\d\d)\.(\d\d)[\]>]")


def validate_elrc(text):
    problems, prev = [], -1
    for n, line in enumerate(text.splitlines(), 1):
        if not line or HEADER.match(line):
            continue
        if not ELRC_LINE.match(line):
            problems.append(f"line {n}: malformed")
            continue
        for m, sec, cs in STAMP.findall(line):
            t = int(m) * 6000 + int(sec) * 100 + int(cs)
            if t < prev:
                problems.append(f"line {n}: time goes backwards")
                break
            prev = t
    return problems


def write_outputs(track, lines, by="YAAAS"):
    lines = normalize_lines(lines)
    text = format_elrc(track, lines, by)
    for p in validate_elrc(text):
        warn("elrc check: " + p)
    # name the files after the audio file; titles can contain dots
    track.path.with_suffix(".lrc").write_text(text, encoding="utf-8")
    data = [{"text": l["text"], "start": l["words"][0][1] / 100, "end": l["words"][-1][2] / 100,
             "words": [{"word": w, "start": s / 100, "end": e / 100} for w, s, e in l["words"]]}
            for l in lines]
    track.path.with_name(track.path.stem + ".words.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def sync_track(track, opts, device, models, cancel=None):
    out_lrc = track.path.with_suffix(".lrc")
    if out_lrc.exists() and not (opts.force or track.redo):
        log("  lyrics already there, skipping")
        return

    lines = []
    data = lrclib_fetch(track.artist, track.title, track.album, track.duration)
    if data and data.get("instrumental"):
        log("  lrclib lists this as an instrumental, skipping")
        return
    if data:
        if data.get("syncedLyrics"):
            lines, mode = parse_synced(data["syncedLyrics"]), "synced"
        else:
            lines, mode = parse_plain(data["plainLyrics"]), "plain, timing will be rougher"
        if lines:
            log(f"  lrclib: {mode}, {len(lines)} lines")
    transcribing = not lines
    if transcribing:
        if not opts.transcribe:
            warn("no lyrics on lrclib (transcribing is switched off)")
            return
        log("  no lyrics on lrclib, will transcribe the vocals")
    check_cancel(cancel)

    import whisperx
    with tempfile.TemporaryDirectory(prefix="yaaas_") as tmp:
        log("  separating vocals")
        vocals = separate_vocals(track.path, Path(tmp), opts.demucs_model, device, cancel,
                                 free=lambda: free_gpu(models))
        check_cancel(cancel)
        audio = whisperx.load_audio(str(vocals))
        duration = len(audio) / 16000
        supported = align_languages()

        if transcribing:
            lang = opts.language or detect_language(audio, device, opts.whisper_model, alignable=False)
            lines = transcribe_lines(audio, device, opts, lang, cancel)
            if not lines:
                warn("couldn't hear any lyrics")
                return
            log(f"  transcribed {len(lines)} lines")
        else:
            lang = opts.language
            if lang and supported and lang not in supported:
                warn(f"no alignment model for '{lang}', detecting instead")
                lang = None
            if not lang:
                lang = detect_language(audio, device, opts.whisper_model, " ".join(l.text for l in lines))

        if transcribing and supported and lang not in supported:
            log(f"  no alignment model for '{lang}', keeping whisper's own word times")
        else:
            log(f"  aligning ({lang})")
            try:
                align_lines(lines, audio, duration, lang, device, models)
            except Exception as e:
                if transcribing:
                    warn(f"alignment failed ({e}), keeping whisper's own word times")
                else:
                    warn(f"alignment failed ({e}), spacing words evenly per line")
                    for i, l in enumerate(lines):
                        nxt = lines[i + 1].start if i + 1 < len(lines) else None
                        lo = l.start if l.start is not None else 0.0
                        hi = l.end if l.end is not None else (nxt or duration)
                        l.words = even_words(l.text, lo, hi)
    if out_lrc.exists():
        shutil.copy2(out_lrc, out_lrc.with_name(track.path.stem + ".lrc.bak"))
    write_outputs(track, lines, "YAAAS (transcribed)" if transcribing else "YAAAS")
    log("  wrote " + out_lrc.name)


def sync_many(tracks, opts, step=None, cancel=None):
    """Sync lyrics for each track. step(done, total, title) is called before each one."""
    device = pick_device(opts.device)
    log(f"\nsyncing lyrics for {len(tracks)} track(s) on {device}")
    models = {}
    for n, tr in enumerate(tracks, 1):
        check_cancel(cancel)
        if step:
            step(n - 1, len(tracks), tr.title)
        log(f"\n[{n}/{len(tracks)}] {tr.artist} - {tr.title}")
        try:
            sync_track(tr, opts, device, models, cancel)
        except Cancelled:
            raise
        except Exception as e:
            warn(f"failed: {e}")


AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav", ".aac", ".wma"}


def is_enhanced(path):
    try:
        return re.search(r"<\d+:\d\d\.\d\d>", Path(path).read_text(encoding="utf-8", errors="ignore")) is not None
    except OSError:
        return False


def read_local_track(path):
    title = artist = album = ""
    number, duration = 0, None
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is not None:
            tags = f.tags or {}

            def first(key):
                try:
                    return str((tags.get(key) or [""])[0]).strip()
                except Exception:
                    return ""

            title, album = first("title"), first("album")
            artist = first("artist") or first("albumartist")
            m = re.match(r"\d+", first("tracknumber"))
            number = int(m.group()) if m else 0
            length = getattr(f.info, "length", None)
            duration = int(length) if length else None
    except Exception:
        pass
    # fall back to the file name ("03 - Artist - Title") and folder name
    stem = path.stem
    m = re.match(r"^(\d+)\s*[-._)]\s*(.*)$", stem)
    if m:
        number = number or int(m.group(1))
        stem = m.group(2)
    if not artist and " - " in stem:
        artist, stem = (x.strip() for x in stem.split(" - ", 1))
    return LocalTrack(path, title or stem.strip(), artist, album or path.parent.name, number, duration)


def scan_folder(root, upgrade=False):
    """Find audio files under root that have no .lrc next to them.

    With upgrade, also pick up .lrc files that carry no per-word timing.
    Returns (groups, audio_files_seen, skipped).
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        raise ValueError(f"not a folder: {root}")
    log(f"scanning {root}")
    groups, seen, skipped = {}, 0, 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            p = Path(dirpath) / name
            if p.suffix.lower() not in AUDIO_EXTS:
                continue
            seen += 1
            lrc = p.with_suffix(".lrc")
            redo = False
            if lrc.exists():
                if upgrade and not is_enhanced(lrc):
                    redo = True
                else:
                    skipped += 1
                    continue
            t = read_local_track(p)
            t.redo = redo
            t.source = "line timing only, will redo" if redo else "no lyrics file"
            groups.setdefault(Path(dirpath), []).append(t)
    albums = []
    for d, tracks in groups.items():
        name = d.name if d == root else str(d.relative_to(root))
        albums.append(PlanAlbum(name, "", None, tracks, key=str(d)))
    return albums, seen, skipped


def execute(albums, opts, progress=None, cancel=None):
    """Download the selected tracks, then sync lyrics.

    progress gets (dl_done, dl_total, sync_done, sync_total, stage, text).
    """
    out_root = Path(opts.out).expanduser()
    chosen = [(a, [t for t in a.tracks if t.selected]) for a in albums]
    chosen = [(a, ts) for a, ts in chosen if ts]

    def wants(t):
        return opts.lyrics == "all" or (opts.lyrics == "requested" and t.requested)

    n_dl = sum(len(ts) for _, ts in chosen)
    n_sync = 0 if opts.lyrics == "none" else sum(1 for _, ts in chosen for t in ts if wants(t))
    dl_done = sync_done = 0

    def report(stage, text):
        if progress:
            progress(dl_done, n_dl, sync_done, n_sync, stage, text)

    report("download", "starting")
    got = []
    for a, tracks in chosen:
        folder = out_root / safe(a.title)
        folder.mkdir(parents=True, exist_ok=True)
        log(f"\n{a.artist} - {a.title} ({len(tracks)} tracks)")
        for t in tracks:
            check_cancel(cancel)
            report("download", t.title)
            log(f"downloading {t.number:02d} {t.title}")
            try:
                path = download_track(t.video_id, folder / f"{t.number:02d} - {safe(t.title)}", opts.fmt)
                tag_file(path, title=t.title, artist=t.artist, album=a.title,
                         albumartist=a.artist, tracknumber=t.number)
                got.append((Track(path, t.title, t.artist, a.title, t.number, t.duration, t.requested), t))
            except Cancelled:
                raise
            except Exception as e:
                warn(f"download failed: {e}")
            dl_done += 1

    if opts.lyrics != "none":
        report("sync", "starting")

        def step(done, total, title):
            nonlocal sync_done
            sync_done = done
            report("sync", title)

        sync_many([tr for tr, pt in got if wants(pt)], opts, step, cancel)
        sync_done = n_sync  # tracks that failed to download still finish the bar
    report("done", "finished")
    log("\nDone.")


def check_install():
    """Used by the setup scripts: can everything be imported, is ffmpeg there, is the GPU visible?"""
    import importlib
    bad = []
    for m in ("yt_dlp", "ytmusicapi", "rapidfuzz", "requests", "mutagen", "numpy",
              "torch", "demucs.separate", "whisperx", "faster_whisper"):
        try:
            importlib.import_module(m)
        except Exception as e:
            bad.append(f"  {m}: {e}")
    if bad:
        print("these packages failed to import:\n" + "\n".join(bad))
        return 1
    import torch
    print("all packages import fine, cuda available:", torch.cuda.is_available())
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("warning: tkinter is missing, so the GUI won't start (the command line still works)")
    if not shutil.which("ffmpeg"):
        print("warning: ffmpeg isn't on the PATH")
    else:
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        if "libmp3lame" not in enc:
            print("warning: this ffmpeg can't encode mp3, pick opus, m4a or flac as the format")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Download albums and build word-synced lyrics.")
    ap.add_argument("input", nargs="?", help="text file of songs and/or links, one per line, or a .json list ('-' reads stdin)")
    ap.add_argument("--scan", metavar="FOLDER", help="add lyrics to audio files under FOLDER that lack a .lrc")
    ap.add_argument("--upgrade", action="store_true", help="with --scan, also redo .lrc files without word timing")
    ap.add_argument("-o", "--out", default="music")
    ap.add_argument("--order", choices=["auto", "artist-title", "title-artist"], default="auto",
                    help="which way round 'A - B' lines are (auto tries both)")
    ap.add_argument("--threshold", type=float, default=70)
    ap.add_argument("--no-expand", action="store_true", help="a video link downloads only that video")
    ap.add_argument("--format", default="mp3", choices=["mp3", "m4a", "opus", "flac"])
    ap.add_argument("--lyrics", default="all", choices=["all", "requested", "none"])
    ap.add_argument("--demucs-model", default="htdemucs")
    ap.add_argument("--whisper-model", default="small")
    ap.add_argument("--language")
    ap.add_argument("--no-transcribe", action="store_true",
                    help="don't make lyrics with whisper when lrclib has none")
    ap.add_argument("--transcribe-model", default="auto", help="whisper model for transcribing (auto, small, medium, large-v3)")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--check", action="store_true", help="check that everything is installed and exit")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    ap.add_argument("--force", action="store_true", help="redo lyrics that already exist")
    a = ap.parse_args()

    if a.check:
        return check_install()
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found")
    if not a.input and not a.scan:
        ap.error("give a song list or --scan FOLDER")

    if a.scan:
        groups, seen, skipped = scan_folder(a.scan, a.upgrade)
        files = [t for g in groups for t in g.tracks]
        log(f"\n{seen} audio file(s), {skipped} already have lyrics, {len(files)} to do")
        for g in groups:
            log(f"\n{g.title}")
            for t in g.tracks:
                log(f"  {t.path.name}  [{t.source}]")
        if a.dry_run or not files:
            return 0
        if not a.yes:
            try:
                if input("\nAdd lyrics to these? [y/N] ").strip().lower() not in ("y", "yes"):
                    return 1
            except EOFError:
                sys.exit("can't ask for confirmation here, pass --yes")
        sync_many(files, Options(demucs_model=a.demucs_model, whisper_model=a.whisper_model,
                                 language=a.language, device=a.device, force=a.force,
                                 transcribe=not a.no_transcribe, transcribe_model=a.transcribe_model))
        log("\nDone.")
        return 0

    text = sys.stdin.read() if a.input == "-" else Path(a.input).read_text(encoding="utf-8")

    if looks_like_json(a.input, text):
        try:
            inputs, skipped = parse_json_entries(text)
        except ValueError as e:
            sys.exit(f"couldn't read the json: {e}")
        log(f"{len(inputs)} entries from the json" + (f" ({skipped} skipped, no title)" if skipped else ""))
    else:
        inputs = text.splitlines()

    from ytmusicapi import YTMusic
    albums, unmatched = build_plan(YTMusic(), inputs, a.threshold, not a.no_expand, a.order)
    print_plan(albums)
    if unmatched:
        log("\nnot resolved:\n  " + "\n  ".join(unmatched))
    if a.dry_run or not albums:
        return 0
    if not a.yes:
        try:
            if input("\nDownload these? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        except EOFError:
            sys.exit("can't ask for confirmation here, pass --yes")

    execute(albums, Options(a.out, a.format, a.lyrics, a.demucs_model, a.whisper_model,
                            a.language, a.device, a.force, not a.no_transcribe, a.transcribe_model))
    return 0


if __name__ == "__main__":
    sys.exit(main())
