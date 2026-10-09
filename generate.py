"""CLI and web share the same speech/audio implementation."""
import argparse
import asyncio
from pathlib import Path
from control import Control, CURRENT
from tts import ROOT, Settings, generate_audio, load_pairs, voices_list

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, default=ROOT / 'phrases.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/polski_slowa_3x.mp3')
    parser.add_argument('--list-voices', action='store_true')
    parser.add_argument('--pl-voice', default='pl-PL-ZofiaNeural')
    parser.add_argument('--ru-voice', default='ru-RU-SvetlanaNeural')
    parser.add_argument('--pl-rate', type=int, default=-20)
    parser.add_argument('--ru-rate', type=int, default=5)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--language-pause', type=int, default=600)
    parser.add_argument('--repeat-pause', type=int, default=900)
    parser.add_argument('--card-pause', type=int, default=1500)
    args = parser.parse_args()
    if args.list_voices:
        for v in await voices_list(refresh=True):
            print(v['ShortName'], v['Locale'], v['Gender'])
        return
    settings = Settings(**{k: getattr(args, k) for k in Settings.__dataclass_fields__})
    CURRENT.set(Control())
    report = await generate_audio(load_pairs(args.input), args.output, settings)
    print(f"{args.output.resolve()}: {report['duration_seconds']:.3f}s, {report['size_bytes']} bytes")

if __name__ == '__main__':
    asyncio.run(main())
