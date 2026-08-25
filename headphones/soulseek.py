from collections import defaultdict, namedtuple
import fcntl
import json
import os
import time

try:
    import slskd_api
except ImportError:  # pragma: no cover - optional dependency handled at runtime
    slskd_api = None

import headphones
from headphones import logger

Result = namedtuple('Result', ['title', 'size', 'user', 'provider', 'type', 'matches', 'bandwidth', 'hasFreeUploadSlot', 'queueLength', 'files', 'kind', 'url', 'folder'])

# Downloads (not searches) are handed off to Nicotine+ via a small file-based
# inbox, consumed by the "autoqueue" Nicotine+ plugin
# (~/.local/share/nicotine/plugins/autoqueue). Nicotine+ does the actual
# transfer and reports live transfer state back via NICOTINE_STATUS_FILE.
NICOTINE_INBOX_DIR = os.path.expanduser('~/nicotine_inbox')
NICOTINE_QUEUE_FILE = os.path.join(NICOTINE_INBOX_DIR, 'queue.json')
NICOTINE_STATUS_FILE = os.path.join(NICOTINE_INBOX_DIR, 'status.json')

# pynicotine.transfers.TransferStatus values that mean the transfer is dead
# and won't finish on its own.
NICOTINE_ERROR_STATUSES = {
    'Cancelled', 'Filtered', 'User logged off', 'Connection closed',
    'Connection timeout', 'Download folder error', 'Local file error',
}


def initialize_soulseek_client():
    host = headphones.CONFIG.SOULSEEK_API_URL
    api_key = headphones.CONFIG.SOULSEEK_API_KEY
    return slskd_api.SlskdClient(host=host, api_key=api_key)


def _folder_name_from_virtual_path(virtual_path):
    # Soulseek virtual paths are backslash-separated, e.g.
    # "Music\Artist\Album\01 - Track.mp3". The album folder is the last
    # component of the directory portion.
    directory = virtual_path.rsplit('\\', 1)[0] if '\\' in virtual_path else ''
    return directory.rsplit('\\', 1)[-1] if directory else directory


def _append_to_nicotine_queue(entries):
    if not entries:
        return

    os.makedirs(NICOTINE_INBOX_DIR, exist_ok=True)

    with open(NICOTINE_QUEUE_FILE, 'a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        content = f.read()

        try:
            queue = json.loads(content) if content.strip() else []
        except json.JSONDecodeError:
            queue = []

        queue.extend(entries)

        f.seek(0)
        f.truncate()
        json.dump(queue, f)
        fcntl.flock(f, fcntl.LOCK_UN)


def _read_nicotine_status():
    if not os.path.exists(NICOTINE_STATUS_FILE):
        return []

    try:
        with open(NICOTINE_STATUS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Soulseek: failed to read Nicotine+ status file: {e}")
        return []

    # Search logic, calling search and processing fucntions
def search(artist, album, year, num_tracks, losslessOnly, allow_lossless, user_search_term):
    client = initialize_soulseek_client()

    # override search string with user provided search term if entered
    if user_search_term:
        artist = user_search_term
        album = ''
        year = ''
    
    # Stage 1: Search with artist, album, year, and num_tracks
    logger.info(f"Searching Soulseek using term: {artist} {album} {year}")
    results = execute_search(client, artist, album, year, losslessOnly, allow_lossless)
    processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks)
    if processed_results or user_search_term or album.lower() == artist.lower():
        return processed_results
    
    # Stage 2: If Stage 1 fails, search with artist, album, and num_tracks (excluding year)
    logger.info("Soulseek search stage 1 did not meet criteria. Retrying without year...")
    results = execute_search(client, artist, album, None, losslessOnly, allow_lossless)
    processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks)
    if processed_results or artist == "Various Artists":
        return processed_results
    
    # Stage 3: Final attempt, search only with artist and album
    logger.info("Soulseek search stage 2 did not meet criteria. Final attempt with only artist and album.")
    results = execute_search(client, artist, album, None, losslessOnly, allow_lossless)
    processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks, ignore_track_count=True)

    return processed_results

def execute_search(client, artist, album, year, losslessOnly, allow_lossless):
    search_text = f"{artist} {album}"
    if year:
        search_text += f" {year}"

    if losslessOnly:
        search_text += " flac"
    elif not allow_lossless:
            search_text += " mp3"

    # Actual search
    search_response = client.searches.search_text(searchText=search_text, filterResponses=True)
    search_id = search_response.get('id')

    # Wait for search completion and return response
    while not client.searches.state(id=search_id).get('isComplete'):
        time.sleep(2)
    
    return client.searches.search_responses(id=search_id)

# Processing the search result passed
def process_results(results, losslessOnly, allow_lossless, num_tracks, ignore_track_count=False):

    if losslessOnly:
        valid_extensions = {'.flac'}
    elif allow_lossless:
        valid_extensions = {'.mp3', '.flac'}
    else:
        valid_extensions = {'.mp3'}

    albums = defaultdict(lambda: {'files': [], 'user': None, 'hasFreeUploadSlot': None, 'queueLength': None, 'uploadSpeed': None})

    # Extract info from the api response and combine files at album level
    for result in results:
        user = result.get('username')
        hasFreeUploadSlot = result.get('hasFreeUploadSlot')
        queueLength = result.get('queueLength')
        uploadSpeed = result.get('uploadSpeed')

        # Only handle .mp3 and .flac
        for file in result.get('files', []):
            filename = file.get('filename')
            file_extension = os.path.splitext(filename)[1].lower()
            if file_extension in valid_extensions:
                #album_directory = os.path.dirname(filename)
                album_directory = filename.rsplit('\\', 1)[0]
                albums[album_directory]['files'].append(file)

                # Update metadata only once per album_directory
                if albums[album_directory]['user'] is None:
                    albums[album_directory].update({
                        'user': user,
                        'hasFreeUploadSlot': hasFreeUploadSlot,
                        'queueLength': queueLength,
                        'uploadSpeed': uploadSpeed,
                    })

    # Filter albums based on num_tracks, add bunch of useful info to the compiled album
    final_results = []
    for directory, album_data in albums.items():
        if ignore_track_count and len(album_data['files']) > 1 or len(album_data['files']) == num_tracks:
            #album_title = os.path.basename(directory)
            album_title = directory.rsplit('\\', 1)[1]
            total_size = sum(file.get('size', 0) for file in album_data['files'])
            final_results.append(Result(
                title=album_title,
                size=int(total_size),
                user=album_data['user'],
                provider="soulseek",
                type="soulseek",
                matches=True,
                bandwidth=album_data['uploadSpeed'],
                hasFreeUploadSlot=album_data['hasFreeUploadSlot'],
                queueLength=album_data['queueLength'],
                files=album_data['files'],
                kind='soulseek',
                url='http://' + album_data['user'] + album_title, # URL is needed in other parts of the program.
                #folder=os.path.basename(directory)
                folder = album_title
            ))

    return final_results


def download(user, filelist):
    # Hand off to Nicotine+ (via the "autoqueue" plugin) instead of slskd:
    # slskd is only used for searching here, Nicotine+ does the transfer.
    entries = [[user, f['filename']] for f in filelist if f.get('filename')]
    _append_to_nicotine_queue(entries)


def download_completed():
    transfers = _read_nicotine_status()
    album_completion_tracker = {}  # Tracks completion state of each album's songs
    album_errored_tracker = {}  # Tracks albums with errored downloads

    for transfer in transfers:
        album_part = _folder_name_from_virtual_path(transfer.get('virtual_path', ''))
        status = transfer.get('status', '')

        if album_part not in album_completion_tracker:
            album_completion_tracker[album_part] = {'total': 0, 'completed': 0, 'errored': 0}
            album_errored_tracker[album_part] = False

        album_completion_tracker[album_part]['total'] += 1

        if status == 'Finished':
            album_completion_tracker[album_part]['completed'] += 1
        elif status in NICOTINE_ERROR_STATUSES:
            album_completion_tracker[album_part]['errored'] += 1
            album_errored_tracker[album_part] = True

    errored_albums = {album for album, errored in album_errored_tracker.items() if errored}
    completed_albums = {album for album, counts in album_completion_tracker.items() if counts['total'] == counts['completed']}

    return completed_albums, errored_albums


def download_completed_album(username, foldername):
    transfers = _read_nicotine_status()

    total_count = 0
    completed_count = 0
    errored_count = 0

    for transfer in transfers:
        if transfer.get('username') != username:
            continue
        if _folder_name_from_virtual_path(transfer.get('virtual_path', '')) != foldername:
            continue

        total_count += 1
        status = transfer.get('status', '')

        if status == 'Finished':
            completed_count += 1
        elif status in NICOTINE_ERROR_STATUSES:
            errored_count += 1

    completed = total_count > 0 and completed_count == total_count
    errored = errored_count > 0

    return completed, errored


def active_downloads():
    # Aggregates the raw per-file Nicotine+ transfer status into one summary
    # row per release, for display on the "Downloads" activity page.
    transfers = _read_nicotine_status()
    releases = {}

    for transfer in transfers:
        username = transfer.get('username', '')
        folder = _folder_name_from_virtual_path(transfer.get('virtual_path', ''))
        key = (username, folder)

        if key not in releases:
            releases[key] = {
                'username': username,
                'folder': folder,
                'total': 0,
                'completed': 0,
                'errored': 0,
                'size': 0,
            }

        release = releases[key]
        release['total'] += 1
        release['size'] += transfer.get('size', 0) or 0

        status = transfer.get('status', '')
        if status == 'Finished':
            release['completed'] += 1
        elif status in NICOTINE_ERROR_STATUSES:
            release['errored'] += 1

    result = []
    for release in releases.values():
        release['in_progress'] = release['total'] - release['completed'] - release['errored']

        if release['errored']:
            release['overall_status'] = 'Errored'
        elif release['completed'] == release['total']:
            release['overall_status'] = 'Finished'
        else:
            release['overall_status'] = 'Downloading'

        result.append(release)

    return result