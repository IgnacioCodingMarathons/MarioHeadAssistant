VIDEO_EXTENSIONS = {'.avi', '.mp4', '.mov', '.mkv', '.webm', '.m4v', '.wmv'}
AUDIO_EXTENSIONS = {
    '.wav', '.wave', '.mp3', '.mp2', '.ogg', '.oga', '.opus', '.flac',
    '.aac', '.m4a', '.m4b', '.wma', '.aif', '.aiff', '.aifc', '.amr',
    '.au', '.snd', '.caf', '.ape', '.alac', '.mka', '.ac3', '.aiffc',
    '.a52', '.dts', '.tta', '.wv', '.weba', '.m4r', '.mpc', '.spx',
}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS


def discover_clips(assets):
    clips = [path for path in assets.iterdir()
             if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS]
    extra = assets / 'sentencemixvids'
    if extra.is_dir():
        clips.extend(path for path in extra.rglob('*')
                     if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS)
    return sorted(clips)


def source_fingerprint(assets):
    return {path.relative_to(assets).as_posix(): [path.stat().st_size, path.stat().st_mtime_ns]
            for path in discover_clips(assets)}


def archive_needs_update(index, assets):
    current = source_fingerprint(assets)
    if 'source_files' in index:
        return index['source_files'] != current
    return set(current) != {source['name'] for source in index.get('sources', [])}
