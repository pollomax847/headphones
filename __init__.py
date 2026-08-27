import fcntl
import json
import math
import os
import re
import threading
import time
import unicodedata
from urllib.parse import quote

import requests
from gi.repository import GLib

from pynicotine.pluginsystem import BasePlugin
from pynicotine.slskmessages import UserStatus

INBOX_DIR = os.path.expanduser('~/nicotine_inbox')
QUEUE_FILE = os.path.join(INBOX_DIR, 'queue.json')
STATUS_FILE = os.path.join(INBOX_DIR, 'status.json')

AUDIO_EXTENSIONS = (
    '.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.wma', '.dsf',
    '.ape', '.alac', '.aiff', '.opus',
)

# How long to back off retrying a (user, release) again after it failed
# outright (e.g. the user is unreachable). Without this, a release from a
# permanently-offline user gets retried every single poll cycle forever,
# since a failed push/download never shows up as "known" on its own.
PUSH_FAILURE_COOLDOWN = 1800

# How long to back off after successfully finding a replacement source.
# A dead original transfer never disappears from Nicotine+'s history on
# its own, so without a long cooldown here, the exact same replacement
# gets re-found and re-downloaded on a loop every PUSH_FAILURE_COOLDOWN --
# this gives it real time to actually finish before reconsidering it.
PUSH_SUCCESS_COOLDOWN = 21600

# Statuses that mean a Nicotine+ transfer is dead and won't finish on its
# own -- worth searching for a different source.
NICOTINE_DEAD_STATUSES = {
    'Cancelled', 'User logged off', 'Connection closed',
    'Connection timeout', 'Download folder error', 'Local file error',
}

# Placeholder failure-tracking key for the Nicotine+ side (there's no
# single "username" a dead Nicotine+ transfer belongs to once we're
# looking for a replacement for it).
NICOTINE_SIDE_KEY = '__nicotine__'


# Matches generic multi-disc folder names ("CD1", "Disc 2", "DVD3", ...)
# that collide across completely unrelated releases -- e.g. countless box
# sets have a folder literally named "CD2".
_GENERIC_DISC_RE = re.compile(r'^(cd|disc|disk|dvd)\s*\d{1,3}$', re.IGNORECASE)

# Matches a folder made up of nothing but disc-number tokens and connectors
# ("CD1 & CD2", "CD1-CD2", "Disc 1 & Disc 2", ...). Just as collision-prone
# as a bare "CD2" -- plenty of unrelated box sets split their discs into
# pairs the same way -- so it needs the same "keep climbing" treatment.
_DISC_TOKEN_RE = re.compile(r'(?:cd|disc|disk|dvd)\s*\d{1,3}', re.IGNORECASE)
_CONNECTOR_ONLY_RE = re.compile(r'^[\s&+\-,/]*$')


def _is_generic_disc_group(name):
    if not _DISC_TOKEN_RE.search(name):
        return False
    return bool(_CONNECTOR_ONLY_RE.match(_DISC_TOKEN_RE.sub('', name)))

# Strips a leading track-number token ("09 - ", "01. ", "CD1-09 ", ...) off
# a filename so what's left is the actual title/artist text -- the part
# that's actually worth sending to a Soulseek search and comparing against
# other peers' copies of the same track.
_TRACK_PREFIX_RE = re.compile(
    r'^\s*(?:cd\s*\d+\s*[-_.]*\s*)?\d{1,3}[\s\-_.\)]+\s*', re.IGNORECASE,
)


def _track_title(filename):
    base = filename.replace('/', '\\').rsplit('\\', 1)[-1]
    base = os.path.splitext(base)[0]
    base = _TRACK_PREFIX_RE.sub('', base, count=1)
    return base.strip()


_WORD_RE = re.compile(r'[a-z0-9]+')


def _track_tokens(filename):
    # A normalized, order-independent "fingerprint" of a track title:
    # different uploaders write the same song as "Title - Artist" or
    # "Artist - Title", with straight or curly apostrophes, accented or
    # plain letters, parentheses or brackets -- none of that should stop
    # two copies of the same track from being recognised as the same.
    title = _track_title(filename)
    decomposed = unicodedata.normalize('NFKD', title)
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return frozenset(w for w in _WORD_RE.findall(stripped.lower()) if len(w) >= 2)


def _tokens_match(candidate_tokens, target_tokens, threshold=0.7):
    # Candidate may carry extra words (format tags, remix credits, box-set
    # names, ...) -- what matters is that it contains most of the target
    # track's own words, regardless of order.
    if not target_tokens or not candidate_tokens:
        return False
    overlap = len(candidate_tokens & target_tokens)
    return overlap >= max(2, math.ceil(len(target_tokens) * threshold))


def _release_key(virtual_path):
    # Group by album/compilation folder name (or the track title itself
    # when there's no folder) instead of matching the exact (user, path)
    # pair -- avoids pulling the same release from two different sources
    # at once just because a different user happens to have it too.
    parts = [p for p in virtual_path.replace('/', '\\').split('\\') if p.strip()]
    if len(parts) < 2:
        return (parts[-1] if parts else '').strip().lower()

    # A bare disc-number folder ("CD1") isn't distinctive on its own --
    # keep it (it still tells CD1 apart from CD2 of the same release) but
    # look further up for the name that actually identifies the release.
    disc_part = None
    idx = len(parts) - 2

    if _GENERIC_DISC_RE.match(parts[idx].strip()):
        disc_part = parts[idx]
        idx -= 1

    # Keep climbing past folders that are themselves just disc tokens and
    # connectors ("CD1 & CD2", "CD1-CD2", ...) -- those collide across
    # completely unrelated box sets just as much as a bare "CD1" does.
    while idx >= 0 and _is_generic_disc_group(parts[idx].strip()):
        idx -= 1

    if idx < 0:
        name = disc_part or parts[-2]
    elif disc_part:
        name = f"{parts[idx]}\\{disc_part}"
    else:
        name = parts[idx]

    return name.strip().lower()


class Plugin(BasePlugin):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settings = {
            'poll_interval': 15,
            'slskd_api_url': '',
            'slskd_api_key': '',
            'slskd_sync_max_per_run': 60,
            'slskd_alt_search_max_per_run': 5,
            'slskd_alt_search_wait_seconds': 30,
        }
        self.metasettings = {
            'poll_interval': {
                'description': 'Seconds between checks of the download queue file',
                'type': 'int',
                'minimum': 1,
            },
            'slskd_api_url': {
                'description': (
                    "Base URL of slskd's API, e.g. http://localhost:5030/api/v0. "
                    "Leave empty to disable syncing with slskd."
                ),
                'type': 'string',
            },
            'slskd_api_key': {
                'description': "API key for slskd (sent as the X-API-Key header).",
                'type': 'string',
            },
            'slskd_sync_max_per_run': {
                'description': (
                    'Max number of files pushed to slskd per poll cycle. '
                    'Keeps a big backlog from making one cycle run long.'
                ),
                'type': 'int',
                'minimum': 1,
            },
            'slskd_alt_search_max_per_run': {
                'description': (
                    'Max number of alternative-source searches (for releases '
                    'whose current source failed) per poll cycle. Each search '
                    'waits up to slskd_alt_search_wait_seconds, so this bounds '
                    'how long a cycle can take.'
                ),
                'type': 'int',
                'minimum': 0,
            },
            'slskd_alt_search_wait_seconds': {
                'description': (
                    'How long to wait for slskd search responses to come in '
                    'before picking an alternative source. Real searches on '
                    'a popular track commonly take 30-60+ seconds to gather '
                    'most of their responses -- cutting this too short means '
                    'missing peers who would otherwise have been found.'
                ),
                'type': 'int',
                'minimum': 1,
            },
        }
        self.running = False
        self.thread = None
        # (username, release) -> time.time() of last outright failure,
        # so we back off instead of retrying every poll cycle.
        self._slskd_push_failures = {}
        # (username, release) -> time.time() of last mirror attempt from
        # slskd into Nicotine+. enqueue_download() not raising doesn't mean
        # the transfer actually materializes (e.g. the peer is unreachable)
        # -- without this, a release that never shows up in known_transfers
        # gets re-enqueued every single poll cycle forever.
        self._slskd_mirror_attempts = {}
        self._alt_searches_this_cycle = 0
        # Releases already given an alternative-source search this cycle --
        # _sync_to_slskd's dead-source branch and _retry_dead_nicotine_transfers
        # both watch for the same dead Nicotine+ transfers, so without this
        # they'd each independently find and queue the same replacement
        # (once via slskd, once directly in Nicotine+) in the same cycle.
        self._alt_search_handled_this_cycle = set()
        # release -> (target, expiry) for releases just resolved via
        # alternative-source search. _sync_from_slskd and _sync_to_slskd's
        # normal (non-dead) push both mirror *any* pending download to the
        # other client for resilience -- but a replacement we just found on
        # purpose for exactly one target shouldn't immediately get mirrored
        # to the other one too, doubling the bandwidth for what was already
        # a fallback.
        self._alt_source_resolved = {}

    def init(self):
        os.makedirs(INBOX_DIR, exist_ok=True)
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.log("Auto Queue started, watching %s" % QUEUE_FILE)

    def disable(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._alt_searches_this_cycle = 0
                self._alt_search_handled_this_cycle = set()
                self._process_queue()
                transfers = self._collect_transfers()

                if transfers is not None:
                    self._write_status(transfers)
                    self._sync_from_slskd(transfers)
                    self._sync_to_slskd(transfers)

                    api_url = self._slskd_api_url()
                    if api_url:
                        self._retry_dead_nicotine_transfers(transfers, api_url)
            except Exception as e:
                self.log(f"Auto Queue error: {e}")

            time.sleep(self.settings.get('poll_interval', 15))

    def _get_file(self, user, filename, size=0, on_success=None, on_error=None):
        # Nicotine+'s core (self.core.transfers.downloads, a deque) is only
        # safe to mutate from the GLib main thread -- it's iterated without
        # any locking by the app's own internal timers (e.g.
        # check_download_queue_callback, on a 60s cycle). Calling get_file()
        # directly from this background thread races those timers and can
        # crash the whole app with "deque mutated during iteration" or
        # "NoneType has no attribute status" mid-iteration. Schedule the
        # actual call on the main loop instead of making it here.
        def _do():
            try:
                self.core.transfers.get_file(user, filename, size=size)
                if on_success:
                    on_success()
            except Exception as e:
                if on_error:
                    on_error(e)
                else:
                    self.log(f"Failed to queue download from {user} ({filename}): {e}")
            return False

        GLib.idle_add(_do)

    # -- external inbox (any program can drop [username, virtual_path] pairs here) --

    def _process_queue(self):
        if not os.path.exists(QUEUE_FILE):
            return

        with open(QUEUE_FILE, 'a+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            content = f.read()

            try:
                entries = json.loads(content) if content.strip() else []
            except json.JSONDecodeError:
                entries = []

            if entries:
                f.seek(0)
                f.truncate()
                json.dump([], f)

            fcntl.flock(f, fcntl.LOCK_UN)

        for entry in entries:
            if not isinstance(entry, list) or len(entry) < 2:
                continue

            user, virtual_path = entry[0], entry[1]

            self._get_file(
                user, virtual_path,
                on_success=lambda u=user, vp=virtual_path: self.log(f"Queued download from {u}: {vp}")
            )

    # -- status file (any program can read this to track transfer progress) --

    def _collect_transfers(self):
        # Reading self.core.transfers.downloads from this background thread
        # races the main thread's own mutations of that same deque (new
        # downloads, status updates, removals) -- rare, but if it happens
        # mid-iteration it raises RuntimeError rather than corrupting
        # anything. Skip this cycle's status write and try again next time
        # instead of crashing the whole app over a stale snapshot.
        transfers = []

        try:
            for transfer in self.core.transfers.downloads:
                transfers.append({
                    'username': transfer.user,
                    'virtual_path': transfer.filename,
                    'folder_path': transfer.path,
                    'status': transfer.status,
                    'size': transfer.size,
                })
        except RuntimeError as e:
            self.log(f"transfer list changed while reading it, skipping this cycle: {e}")
            return None

        return transfers

    def _write_status(self, transfers):
        tmp_path = STATUS_FILE + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(transfers, f)
        os.replace(tmp_path, STATUS_FILE)

    def download_finished_notification(self, user, virtual_path, real_path):
        self.log(f"Download finished from {user}: {virtual_path}")
        transfers = self._collect_transfers()
        if transfers is not None:
            self._write_status(transfers)

    # -- two-way sync with slskd, so both clients work on the same downloads --

    def _slskd_api_url(self):
        return self.settings.get('slskd_api_url', '').rstrip('/')

    def _slskd_headers(self):
        return {'X-API-Key': self.settings.get('slskd_api_key', '')}

    def _fetch_slskd_downloads(self, api_url):
        resp = requests.get(f'{api_url}/transfers/downloads', headers=self._slskd_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _sync_from_slskd(self, known_transfers):
        # Anything slskd already has queued/active gets mirrored into
        # Nicotine+, so Nicotine+ works on it too (soularr/Lidarr or
        # slskd's own web UI can queue downloads directly in slskd).
        api_url = self._slskd_api_url()
        if not api_url:
            return

        known_releases = {_release_key(t['virtual_path']) for t in known_transfers}

        try:
            slskd_downloads = self._fetch_slskd_downloads(api_url)
        except Exception as e:
            self.log(f"failed to fetch slskd downloads: {e}")
            return

        now = time.time()

        for transfer in slskd_downloads:
            user = transfer.get('username')
            if not user:
                continue

            for directory in transfer.get('directories', []):
                # Merge into known_releases only after the whole
                # directory (one release from this one peer) is done, so
                # sibling tracks of the same release/user don't block
                # each other -- only a *different* peer offering the
                # same release afterwards gets skipped.
                newly_queued = set()

                for file in directory.get('files', []):
                    if file.get('state', '').startswith('Completed'):
                        continue

                    filename = file.get('filename')
                    if not filename:
                        continue

                    release = _release_key(filename)
                    if release in known_releases:
                        continue

                    resolved = self._alt_source_resolved.get(release)
                    if resolved and resolved[0] == 'slskd' and now < resolved[1]:
                        # We deliberately routed this replacement to slskd
                        # only -- don't also duplicate it into Nicotine+.
                        continue

                    attempt_key = (user, release)
                    last_attempt = self._slskd_mirror_attempts.get(attempt_key)
                    if last_attempt and now - last_attempt < PUSH_FAILURE_COOLDOWN:
                        continue

                    self._slskd_mirror_attempts[attempt_key] = now

                    self._get_file(
                        user, filename, size=file.get('size', 0),
                        on_success=lambda u=user, fn=filename: self.log(f"mirrored from slskd: {u} - {fn}"),
                        on_error=lambda e, u=user, fn=filename: self.log(f"failed to queue {u} - {fn} from slskd: {e}")
                    )
                    newly_queued.add(release)

                known_releases |= newly_queued

    def _sync_to_slskd(self, known_transfers):
        # Anything Nicotine+ has pending gets pushed to slskd too, so
        # slskd's own connection also works on getting it.
        api_url = self._slskd_api_url()
        if not api_url:
            return

        pending = [
            t for t in known_transfers
            if t['status'] != 'Finished' and t['virtual_path'].lower().endswith(AUDIO_EXTENSIONS)
        ]
        if not pending:
            return

        try:
            slskd_downloads = self._fetch_slskd_downloads(api_url)
        except Exception as e:
            self.log(f"failed to fetch slskd downloads for sync: {e}")
            return

        slskd_known_releases = set()
        for transfer in slskd_downloads:
            for directory in transfer.get('directories', []):
                for file in directory.get('files', []):
                    filename = file.get('filename')
                    if filename:
                        slskd_known_releases.add(_release_key(filename))

        # Group by (user, release) so a whole album is pushed together --
        # marking the release as known only once its group is done means
        # sibling tracks don't block each other, while a second group for
        # the same release (a different user/source) still gets skipped.
        groups = {}
        for t in pending:
            key = (t['username'], _release_key(t['virtual_path']))
            groups.setdefault(key, []).append(t)

        max_per_run = self.settings.get('slskd_sync_max_per_run', 60)
        added = 0
        now = time.time()

        for (username, release), items in groups.items():
            if added >= max_per_run:
                break
            if release in slskd_known_releases:
                continue

            resolved = self._alt_source_resolved.get(release)
            if resolved and resolved[0] == 'nicotine' and now < resolved[1]:
                # We deliberately routed this replacement to Nicotine+
                # only -- don't also duplicate it into slskd.
                continue

            failure_key = (username, release)
            expiry = self._slskd_push_failures.get(failure_key)
            if expiry and now < expiry:
                continue

            # If Nicotine+ already knows this source is dead (peer logged
            # off, connection dropped, ...), pushing to slskd is a
            # guaranteed HTTP 500 ("user appears to be offline") -- skip
            # straight to searching for a different source instead of
            # spamming the log with failures we already know about.
            if all(t['status'] in NICOTINE_DEAD_STATUSES for t in items):
                if release in self._alt_search_handled_this_cycle:
                    continue
                self._alt_search_handled_this_cycle.add(release)

                sample_filenames = [t['virtual_path'] for t in items]
                found = self._try_alternative_source(
                    api_url, release, exclude_user=username, target='slskd',
                    sample_filenames=sample_filenames,
                )
                cooldown = PUSH_SUCCESS_COOLDOWN if found else PUSH_FAILURE_COOLDOWN
                self._slskd_push_failures[failure_key] = now + cooldown
                slskd_known_releases.add(release)
                continue

            group_succeeded = False

            for t in items:
                if added >= max_per_run:
                    break

                url = f"{api_url}/transfers/downloads/{quote(username, safe='')}"
                payload = [{'filename': t['virtual_path'], 'size': t.get('size', 0)}]

                try:
                    r = requests.post(url, headers=self._slskd_headers(), json=payload, timeout=5)
                    if r.ok:
                        self.log(f"mirrored to slskd: {username} - {t['virtual_path']}")
                        added += 1
                        group_succeeded = True
                    else:
                        self.log(
                            f"slskd rejected {username} - {t['virtual_path']}: "
                            f"HTTP {r.status_code} {r.text.strip()}"
                        )
                except requests.RequestException as e:
                    self.log(f"failed to push {username} - {t['virtual_path']} to slskd: {e}")

            if group_succeeded:
                self._slskd_push_failures.pop(failure_key, None)
            elif release not in self._alt_search_handled_this_cycle:
                # Nothing in this release got through -- likely an
                # unreachable user. Back off instead of hammering it
                # again next cycle, and try to find someone else who has
                # the same release instead.
                self._alt_search_handled_this_cycle.add(release)
                sample_filenames = [t['virtual_path'] for t in items]
                found = self._try_alternative_source(
                    api_url, release, exclude_user=username, target='slskd',
                    sample_filenames=sample_filenames,
                )
                cooldown = PUSH_SUCCESS_COOLDOWN if found else PUSH_FAILURE_COOLDOWN
                self._slskd_push_failures[failure_key] = now + cooldown

            slskd_known_releases.add(release)

    # -- alternative-source search: when a release's current source fails,
    # search slskd (the only side with a search API) for someone else who
    # has the same release, and queue that instead. --

    def _search_slskd(self, api_url, query, wait=30):
        headers = self._slskd_headers()

        resp = requests.post(f'{api_url}/searches', headers=headers,
                              json={'searchText': query}, timeout=10)
        resp.raise_for_status()
        search_id = resp.json().get('id')
        if not search_id:
            return []

        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(1)
            state_resp = requests.get(f'{api_url}/searches/{search_id}', headers=headers, timeout=10)
            if state_resp.ok and state_resp.json().get('isComplete'):
                break

        resp = requests.get(f'{api_url}/searches/{search_id}/responses', headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _track_already_shared(self, track_tokens):
        # Reuses Nicotine+'s own local share word index (the same one it
        # uses to answer other peers' searches) to check whether we
        # already have a matching track sitting in our own shares --
        # no point searching the network for a replacement we already have.
        if not track_tokens:
            return False

        try:
            shares = self.core.shares
            word_index = shares.share_dbs.get('words')
            file_path_index = shares.file_path_index
        except Exception:
            return False

        if not word_index or not file_path_index:
            return False

        # Narrow candidates down using the rarest (longest) words first,
        # same strategy Nicotine+ itself uses -- keeps this fast even
        # against a share with hundreds of thousands of files.
        indices = None
        for word in sorted(track_tokens, key=len, reverse=True):
            word_indices = word_index.get(word)
            if not word_indices:
                continue
            indices = set(word_indices) if indices is None else (indices & set(word_indices))
            if indices and len(indices) <= 20:
                break

        if not indices:
            return False

        public_files = shares.share_dbs.get('public_files', {})
        buddy_files = shares.share_dbs.get('buddy_files', {})
        trusted_files = shares.share_dbs.get('trusted_files', {})

        for index in indices:
            if index >= len(file_path_index):
                continue

            real_path = file_path_index[index]
            fileinfo = public_files.get(real_path) or buddy_files.get(real_path) or trusted_files.get(real_path)
            if not fileinfo:
                continue

            virtual_name = fileinfo[0]
            if not virtual_name.lower().endswith(AUDIO_EXTENSIONS):
                continue

            if _tokens_match(_track_tokens(virtual_name), track_tokens):
                return True

        return False

    def _is_known_online(self, user):
        # Best-effort: Nicotine+ only knows a user's live status if we've
        # watched them before (a prior transfer, browse, or chat). A search
        # result being present just means they answered a query at some
        # point during the search window -- by the time we get around to
        # requesting the file, seconds to tens of seconds later, they may
        # already be gone. This can't be perfect, but it lets us prefer a
        # candidate we have real reason to believe is still around over one
        # we know nothing about.
        return self.core.user_statuses.get(user) == UserStatus.ONLINE

    def _find_alternative_source(self, search_results, track_token_sets, exclude_user, release):
        # Prefer whichever other user offers the most matching tracks
        # (closest to a complete release) with a free upload slot, ranking
        # candidates we already know are online above ones we don't. Match
        # on normalized track-title word overlap rather than an exact
        # string or folder path -- different uploaders write the same
        # song as "Title - Artist" or "Artist - Title", with different
        # apostrophes/accents/brackets, so exact matching almost never
        # hits even when the file is genuinely the same track.
        # (Falls back to folder-key matching if no track titles were
        # given to match against.)
        best = None

        for result in search_results:
            user = result.get('username')
            if not user or user == exclude_user:
                continue

            candidates = [
                f for f in result.get('files', [])
                if f.get('filename', '').lower().endswith(AUDIO_EXTENSIONS)
            ]

            if track_token_sets:
                matches = [
                    f for f in candidates
                    if any(
                        _tokens_match(_track_tokens(f.get('filename', '')), target)
                        for target in track_token_sets
                    )
                ]
            else:
                matches = [
                    f for f in candidates
                    if _release_key(f.get('filename', '')) == release
                ]
            if not matches:
                continue

            rank = (self._is_known_online(user), result.get('hasFreeUploadSlot', False), len(matches))
            if best is None or rank > best[0]:
                best = (rank, user, matches)

        if best is None:
            return None

        _, user, matches = best
        return user, matches

    def _try_alternative_source(self, api_url, release, exclude_user, target, sample_filenames=()):
        max_searches = self.settings.get('slskd_alt_search_max_per_run', 5)

        sample_filenames = [f for f in sample_filenames if _track_title(f)]

        if sample_filenames:
            # Don't spend a search (and a download slot) chasing a
            # replacement for a track we already have sitting in our own
            # shares -- only look for whichever of these tracks are
            # actually missing.
            sample_filenames = [
                f for f in sample_filenames
                if not self._track_already_shared(_track_tokens(f))
            ]
            if not sample_filenames:
                self.log(f"already have {release} in local shares, skipping alternative search")
                return False

        track_token_sets = [_track_tokens(f) for f in sample_filenames]
        # Try a few different tracks from the release as search queries --
        # one mistagged/rare track shouldn't sink the whole release, and a
        # peer might not have every track we're looking for.
        queries = sample_filenames[:3] or [release]

        alternative = None
        for query_file in queries:
            if self._alt_searches_this_cycle >= max_searches:
                break
            self._alt_searches_this_cycle += 1

            search_text = _track_title(query_file) if query_file in sample_filenames \
                else release.replace('\\', ' ').strip()

            wait = self.settings.get('slskd_alt_search_wait_seconds', 30)

            try:
                results = self._search_slskd(api_url, search_text, wait=wait)
            except Exception as e:
                self.log(f"alternative search failed for '{search_text}' ({release}): {e}")
                continue

            alternative = self._find_alternative_source(results, track_token_sets, exclude_user, release)
            if alternative:
                break

        if not alternative:
            self.log(f"no alternative source found for {release}")
            return False

        alt_user, alt_files = alternative

        if target == 'slskd':
            url = f"{api_url}/transfers/downloads/{quote(alt_user, safe='')}"
            payload = [{'filename': f['filename'], 'size': f.get('size', 0)} for f in alt_files]

            try:
                r = requests.post(url, headers=self._slskd_headers(), json=payload, timeout=5)
            except requests.RequestException as e:
                self.log(f"failed to push alternative source {alt_user} for {release} to slskd: {e}")
                return False

            if r.ok:
                self.log(f"found alternative source for {release}: {alt_user} ({len(alt_files)} files) -> slskd")
                self._alt_source_resolved[release] = ('slskd', time.time() + PUSH_SUCCESS_COOLDOWN)
                return True

            self.log(
                f"alternative source {alt_user} for {release} also rejected by slskd: "
                f"HTTP {r.status_code} {r.text.strip()}"
            )
            return False

        # target == 'nicotine'
        queued_any = False
        for f in alt_files:
            filename = f['filename']
            self._get_file(
                alt_user, filename, size=f.get('size', 0),
                on_error=lambda e, u=alt_user, fn=filename: self.log(f"failed to queue alternative {u} - {fn}: {e}")
            )
            queued_any = True

        if queued_any:
            self.log(f"found alternative source for {release}: {alt_user} ({len(alt_files)} files) -> Nicotine+")
            self._alt_source_resolved[release] = ('nicotine', time.time() + PUSH_SUCCESS_COOLDOWN)

        return queued_any

    def _retry_dead_nicotine_transfers(self, known_transfers, api_url):
        # A download Nicotine+ already has can also be dead-ended (peer
        # logged off, connection dropped, ...) regardless of whether it
        # originated locally or was mirrored in from slskd. Search for a
        # different source for it too.
        now = time.time()
        seen_releases = set()

        audio_transfers = [
            t for t in known_transfers if t['virtual_path'].lower().endswith(AUDIO_EXTENSIONS)
        ]
        release_filenames = {}
        for t in audio_transfers:
            release_filenames.setdefault(_release_key(t['virtual_path']), []).append(t['virtual_path'])

        for t in audio_transfers:
            if t['status'] not in NICOTINE_DEAD_STATUSES:
                continue

            release = _release_key(t['virtual_path'])
            if release in seen_releases:
                continue
            seen_releases.add(release)

            failure_key = (NICOTINE_SIDE_KEY, release)
            expiry = self._slskd_push_failures.get(failure_key)
            if expiry and now < expiry:
                continue

            if release in self._alt_search_handled_this_cycle:
                # _sync_to_slskd already searched for this exact release
                # this cycle (it watches the same dead transfers) -- don't
                # queue a second, redundant copy of whatever it found.
                continue
            self._alt_search_handled_this_cycle.add(release)

            found = self._try_alternative_source(
                api_url, release, exclude_user=t['username'], target='nicotine',
                sample_filenames=release_filenames[release],
            )

            # The original dead transfer never disappears from Nicotine+'s
            # history on its own, so it'd be found again next cycle no
            # matter what -- cool down (longer on success) or a successful
            # find just gets re-searched and re-queued into Nicotine+
            # again and again, downloading the same replacement file
            # repeatedly instead of giving it time to actually finish.
            cooldown = PUSH_SUCCESS_COOLDOWN if found else PUSH_FAILURE_COOLDOWN
            self._slskd_push_failures[failure_key] = now + cooldown
