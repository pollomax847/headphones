from collections import defaultdict, namedtuple
from datetime import datetime, timedelta
import os
import time

import requests

import headphones
from headphones import logger

Result = namedtuple('Result', ['title', 'size', 'user', 'provider', 'type', 'matches', 'bandwidth', 'hasFreeUploadSlot', 'queueLength', 'files', 'kind', 'url', 'folder'])

# Downloads are driven through the "API Nicotine Plus" plugin
# (https://github.com/palaueb/api-nicotine-plus, installed locally under
# ~/.local/share/nicotine/plugins/api_nicotine_plus/ with local cancel/clear
# endpoints added on top since upstream doesn't have them yet) running
# inside the single, interactive Nicotine+ instance the user drives by hand.
# There is no separate headless daemon anymore: one Soulseek login serves
# both manual browsing and Headphones automation.

SEARCH_WAIT_SECONDS = 20  # Soulseek has no server-side "search complete" signal
RESULTS_PAGE_SIZE = 500

ERROR_STATUSES = {
    "Cancelled", "User logged off", "Connection closed",
    "Connection timeout", "Download folder error", "Local file error",
}


def _api_url():
    return headphones.CONFIG.SOULSEEK_API_URL.rstrip('/')


def _headers():
    token = headphones.CONFIG.SOULSEEK_API_KEY
    return {'X-API-Token': token} if token else {}


def _get(path, params=None):
    response = requests.get(f"{_api_url()}{path}", params=params, headers=_headers(), timeout=15)
    response.raise_for_status()
    return response.json()


def _post(path, payload=None):
    response = requests.post(f"{_api_url()}{path}", json=payload or {}, headers=_headers(), timeout=15)
    response.raise_for_status()
    return response.json()


def _album_folder_name(virtual_path):
    directory = virtual_path.rsplit('\\', 1)[0] if '\\' in virtual_path else ''
    return directory.rsplit('\\', 1)[-1] if directory else directory


def _own_username():
    try:
        return _get('/status').get('login_username')
    except Exception as e:
        logger.debug(f"Could not determine Nicotine+'s own username: {e}")
        return None


def server_state():
    try:
        status = _get('/status')
    except Exception as e:
        return {'error': str(e)}

    connected = bool(status.get('connected'))
    return {
        'isConnected': connected,
        'state': 'Connected, LoggedIn' if connected else 'Disconnected',
    }


def remove_completed_downloads():
    try:
        _post('/downloads/clear', {'statuses': ['Finished', 'Cancelled']})
        return True
    except Exception as e:
        logger.debug(f"Soulseek failed to clean up completed downloads: {e}")
        return False


# Search logic, calling search and processing functions
def search(artist, album, year, num_tracks, losslessOnly, allow_lossless, user_search_term):
    exclude_users = {name for name in [_own_username()] if name}

    # override search string with user provided search term if entered
    if user_search_term:
        artist = user_search_term
        album = ''
        year = ''

    # Stage 1: Search with artist, album, year, and num_tracks
    logger.info(f"Searching Soulseek using term: {artist} {album} {year}")
    results = execute_search(artist, album, year, losslessOnly, allow_lossless)
    processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks, exclude_users=exclude_users)
    if processed_results or user_search_term or album.lower() == artist.lower():
        return processed_results

    # Stage 2: If Stage 1 fails, search with artist, album, and num_tracks (excluding year)
    logger.info("Soulseek search stage 1 did not meet criteria. Retrying without year...")
    results = execute_search(artist, album, None, losslessOnly, allow_lossless)
    processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks, exclude_users=exclude_users)
    if processed_results or artist == "Various Artists":
        return processed_results

    # Stage 3: Final attempt, search only with artist and album
    logger.info("Soulseek search stage 2 did not meet criteria. Final attempt with only artist and album.")
    results = execute_search(artist, album, None, losslessOnly, allow_lossless)
    processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks, ignore_track_count=True, exclude_users=exclude_users)

    return processed_results


def execute_search(artist, album, year, losslessOnly, allow_lossless):
    search_text = f"{artist} {album}"
    if year:
        search_text += f" {year}"

    if losslessOnly:
        search_text += " flac"
    elif not allow_lossless:
        search_text += " mp3"

    response = _post('/search', {'query': search_text, 'mode': 'global'})
    token = response.get('token')

    # No "isComplete" signal exists for a Soulseek search; results trickle
    # in over time from every peer that matches, so just give it a window.
    time.sleep(SEARCH_WAIT_SECONDS)

    results = []
    offset = 0
    while True:
        page = _get('/search/results', {'token': token, 'limit': RESULTS_PAGE_SIZE, 'offset': offset})
        items = page.get('items', [])
        if not items:
            break
        results.extend(items)
        offset += len(items)
        if offset >= page.get('total', offset):
            break

    return results


# Processing the search result passed
def process_results(results, losslessOnly, allow_lossless, num_tracks, ignore_track_count=False, exclude_users=None):

    if losslessOnly:
        valid_extensions = {'.flac'}
    elif allow_lossless:
        valid_extensions = {'.mp3', '.flac'}
    else:
        valid_extensions = {'.mp3'}

    exclude_users = exclude_users or set()

    albums = defaultdict(lambda: {'files': [], 'user': None, 'hasFreeUploadSlot': None, 'queueLength': None, 'uploadSpeed': None})

    for result in results:
        user = result.get('username')
        if user in exclude_users:
            continue

        filename = result.get('file_path')
        if not filename:
            continue

        file_extension = os.path.splitext(filename)[1].lower()
        if file_extension in valid_extensions:
            album_directory = filename.rsplit('\\', 1)[0]
            albums[album_directory]['files'].append({
                'filename': filename,
                'size': result.get('size', 0),
            })

            # Update metadata only once per album_directory
            if albums[album_directory]['user'] is None:
                albums[album_directory].update({
                    'user': user,
                    'hasFreeUploadSlot': result.get('free_upload_slots'),
                    'queueLength': result.get('queue_position'),
                    'uploadSpeed': result.get('upload_speed'),
                })

    # Filter albums based on num_tracks, add bunch of useful info to the compiled album
    final_results = []
    for directory, album_data in albums.items():
        if ignore_track_count and len(album_data['files']) > 1 or len(album_data['files']) == num_tracks:
            album_title = directory.rsplit('\\', 1)[-1]
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
                url='http://' + album_data['user'] + album_title,  # URL is needed in other parts of the program.
                folder=album_title,
            ))

    return final_results


def download(user, filelist):
    for file in filelist:
        _post('/downloads/enqueue', {
            'username': user,
            'virtual_path': file['filename'],
            'size': file.get('size', 0),
        })


def download_completed():
    all_downloads = _get('/downloads', {'active_only': 'false'}).get('items', [])
    album_completion_tracker = {}  # Tracks completion state of each album's songs
    album_errored_tracker = {}  # Tracks albums with errored downloads

    # Identify errored and completed albums
    for download_item in all_downloads:
        album_part = _album_folder_name(download_item.get('virtual_path', ''))
        status = download_item.get('status', '')

        if album_part not in album_completion_tracker:
            album_completion_tracker[album_part] = {'total': 0, 'completed': 0, 'errored': 0}
        if album_part not in album_errored_tracker:
            album_errored_tracker[album_part] = False

        album_completion_tracker[album_part]['total'] += 1

        if status == 'Finished':
            album_completion_tracker[album_part]['completed'] += 1
        elif status in ERROR_STATUSES:
            album_completion_tracker[album_part]['errored'] += 1
            album_errored_tracker[album_part] = True

    # Identify errored albums
    errored_albums = {album for album, errored in album_errored_tracker.items() if errored}

    # Cancel downloads for errored albums
    for download_item in all_downloads:
        album_part = _album_folder_name(download_item.get('virtual_path', ''))
        if album_part in errored_albums:
            try:
                _post('/downloads/cancel', {
                    'username': download_item.get('username', ''),
                    'virtual_path': download_item.get('virtual_path', ''),
                })
            except Exception as e:
                logger.debug(f"Soulseek failed to cancel download for {album_part}: {e}")

    # Clear completed/canceled/errored stuff from client downloads
    remove_completed_downloads()

    # Identify completed albums
    completed_albums = {album for album, counts in album_completion_tracker.items() if counts['total'] == counts['completed']}

    # Return both completed and errored albums
    return completed_albums, errored_albums


def download_completed_album(username, foldername):
    downloads = _get('/downloads', {'user': username, 'active_only': 'false'}).get('items', [])

    total_count = 0
    completed_count = 0
    errored_count = 0
    matching = []

    for download_item in downloads:
        if _album_folder_name(download_item.get('virtual_path', '')) != foldername:
            continue

        matching.append(download_item)
        total_count += 1
        status = download_item.get('status', '')

        if status == 'Finished':
            completed_count += 1
        elif status in ERROR_STATUSES:
            errored_count += 1

    completed = total_count > 0 and completed_count == total_count
    errored = errored_count > 0

    # Cancel downloads for errored album
    if errored:
        for download_item in matching:
            try:
                _post('/downloads/cancel', {
                    'username': download_item.get('username', ''),
                    'virtual_path': download_item.get('virtual_path', ''),
                })
            except Exception:
                logger.debug(f"Soulseek failed to cancel download for folder: {foldername}")

    return completed, errored


def active_downloads():
    # Aggregates the raw per-file Nicotine+ transfer status into one summary
    # row per release, for display on the "Downloads" activity page.
    try:
        all_downloads = _get('/downloads', {'active_only': 'false'}).get('items', [])
    except Exception as e:
        logger.debug(f"Soulseek failed to fetch active downloads: {e}")
        return []

    releases = {}

    for download_item in all_downloads:
        username = download_item.get('username', '')
        folder = _album_folder_name(download_item.get('virtual_path', ''))
        key = (username, folder)

        if key not in releases:
            releases[key] = {
                'username': username,
                'folder': folder,
                'total': 0,
                'completed': 0,
                'errored': 0,
                'size': 0,
                'bytes_transferred': 0,
                'bytes_remaining': 0,
                'speed': 0,
            }

        release = releases[key]
        release['total'] += 1
        release['size'] += download_item.get('size', 0) or 0

        status = download_item.get('status', '')
        if status == 'Finished':
            release['completed'] += 1
        elif status in ERROR_STATUSES:
            release['errored'] += 1
        else:
            # Only count bytes/speed for files still actually moving -- a
            # finished file has nothing left, a dead one shouldn't count
            # toward ETA.
            current = download_item.get('current_byte_offset') or 0
            size = download_item.get('size') or 0
            release['bytes_transferred'] += current
            release['bytes_remaining'] += max(size - current, 0)
            release['speed'] += download_item.get('speed', 0) or 0

    result = []
    for release in releases.values():
        release['in_progress'] = release['total'] - release['completed'] - release['errored']

        if release['errored']:
            release['overall_status'] = 'Errored'
        elif release['completed'] == release['total']:
            release['overall_status'] = 'Finished'
        else:
            release['overall_status'] = 'Downloading'

        release['eta_seconds'] = int(release['bytes_remaining'] / release['speed']) if release['speed'] else None

        result.append(release)

    return result


def parse_datetime(datetime_string):
    # Parse the datetime api response
    if '.' in datetime_string:
        datetime_string = datetime_string[:datetime_string.index('.') + 7]
    return datetime.strptime(datetime_string, '%Y-%m-%dT%H:%M:%S.%f')
