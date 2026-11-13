import argparse
import base64
import json
import sys
import sysconfig


def send(protocol, value):
    protocol.write(json.dumps(value, separators=(',', ':')) + '\n')
    protocol.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--assets', required=True)
    parser.add_argument('--ffmpeg', required=True)
    args = parser.parse_args()
    protocol = sys.stdout
    sys.stdout = sys.stderr
    try:
        if sysconfig.get_config_var('Py_GIL_DISABLED') != 1:
            raise RuntimeError('worker interpreter is not a free-threaded CPython build')
        from pathlib import Path
        from sentence_mixer import LiveSentenceMixer

        mixer = LiveSentenceMixer(Path(args.assets), args.ffmpeg)
        mixer.enabled = True
        mixer.started = True
        mixer._load_engine()
        if not mixer.ready:
            raise RuntimeError('sentence mixer did not finish loading')
        from pydub import AudioSegment

        gil_disabled = not sys._is_gil_enabled()
        if not gil_disabled:
            raise RuntimeError('a dependency enabled the GIL; free-threaded mixing is unavailable')
        send(protocol, {'ready': True, 'gil_disabled': True})
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request.get('command') == 'stop':
                    break
                if request.get('command') != 'mix':
                    send(protocol, {'ok': False, 'error': 'unknown worker command'})
                    continue
                media = mixer._mix_in_process(
                    request.get('text', ''), bool(request.get('with_video'))
                )
                if media is None:
                    send(protocol, {'ok': True, 'media': None})
                elif isinstance(media, dict):
                    send(protocol, {'ok': True, 'media': {
                        'audio': base64.b64encode(media['audio']).decode('ascii'),
                        'video': str(media['video']),
                        'text': media['text'],
                    }})
                else:
                    send(protocol, {'ok': True, 'media': {
                        'audio': base64.b64encode(media).decode('ascii'),
                    }})
            except Exception as exc:
                send(protocol, {'ok': False,
                                'error': f'{type(exc).__name__}: {exc}'})
    except Exception as exc:
        send(protocol, {'ready': False,
                        'error': f'{type(exc).__name__}: {exc}'})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
