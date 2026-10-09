"""Microsoft Edge Online TTS; bounded parallel requests, disk cache, streaming ffmpeg."""
from array import array
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, asdict
import asyncio
from contextlib import asynccontextmanager
import fcntl
import os
import time
from parser import validate_pairs
from control import check, guarded, run_process, MAX_AUDIO_SECONDS, MAX_UNIQUE

import edge_tts

ROOT = Path(__file__).resolve().parent
SAMPLE_RATE = 24000


@dataclass
class Settings:
    pl_rate: int = -20
    ru_rate: int = 5
    repetitions: int = 3
    language_pause: int = 600
    repeat_pause: int = 900
    card_pause: int = 1500
    pl_voice: str = "pl-PL-ZofiaNeural"
    ru_voice: str = "ru-RU-SvetlanaNeural"

    def __post_init__(self):
        if not all(type(v) is int and -50 <= v <= 50 for v in (self.pl_rate, self.ru_rate)):
            raise ValueError("Скорости должны быть от −50 до +50%")
        if type(self.repetitions) is not int or not 1 <= self.repetitions <= 5:
            raise ValueError("Количество повторений: 1–5")
        if not all(type(v) is int and 0 <= v <= 5000 for v in (self.language_pause, self.repeat_pause, self.card_pause)):
            raise ValueError("Паузы должны быть от 0 до 5000 мс")


def run(command):
    return run_process(command)


def ffmpeg(*args):
    return run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y", "-threads", "1", *map(str, args)])


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))


def validate_mp3(path):
    data = probe(path)
    if not path.is_file() or not path.stat().st_size or data["streams"][0]["codec_name"] != "mp3":
        raise ValueError(f"Invalid MP3: {path}")
    ffmpeg("-i", path, "-f", "null", "-")
    return data


def load_pairs(path):
    pairs = json.loads(path.read_text(encoding="utf-8"))
    validate_pairs(pairs)
    return pairs


async def voices_list(refresh=False):
    catalog = ROOT / 'cache' / 'voices.json'
    if not refresh and catalog.exists():
        try:
            data = json.loads(catalog.read_text())
            if data and all(v['Locale'] in ('pl-PL','ru-RU') for v in data):
                return data
        except (ValueError,KeyError,TypeError):
            pass
    voices = await guarded(edge_tts.list_voices())
    result = sorted([v for v in voices if v['Locale'] in ('pl-PL','ru-RU')],key=lambda v:v['ShortName'])
    catalog.parent.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',dir=catalog.parent,delete=False,encoding='utf-8') as file:
        json.dump(result,file,ensure_ascii=False)
        temporary = Path(file.name)
    temporary.replace(catalog)
    return result


def choose_voice(voices, locale, requested):
    candidates = [v for v in voices if v["Locale"] == locale and v["ShortName"].endswith("Neural")]
    if requested:
        candidates = [v for v in candidates if v["ShortName"] == requested]
    else:
        candidates = [v for v in candidates if v["Gender"] == "Female"] or candidates
    if not candidates:
        raise ValueError(f"No available Neural voice for {locale}: {requested}")
    return candidates[0]


@asynccontextmanager
async def cache_lock(path, shared=False):
    with path.open('a') as lock:
        while True:
            check()
            try:
                fcntl.flock(lock,(fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(.1)
        try:
            yield
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


async def synthesize(text, language, voice, rate, cache):
    settings = dict(text=text, language=language, voice=voice, rate=rate, pitch='+0Hz', volume='+0%')
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    path = cache / f'{language}_{digest}.mp3'
    locks = cache / '.locks'
    locks.mkdir(exist_ok=True)
    # Fixed 256 shards: stable across processes, without one lock per phrase.
    async with cache_lock(locks / (digest[:2] + '.lock')):
        cache_hit = path.exists()
        if cache_hit:
            try:
                validate_mp3(path)
            except (subprocess.CalledProcessError,ValueError,KeyError):
                path.unlink(missing_ok=True)
                cache_hit = False
        if not cache_hit:
            # Unique temporary names plus a cross-process lock protect web/CLI writers.
            with tempfile.NamedTemporaryFile(dir=cache,suffix='.partial.mp3',delete=False) as file:
                partial = Path(file.name)
            try:
                await guarded(edge_tts.Communicate(text,voice,rate=rate,pitch='+0Hz',volume='+0%',connect_timeout=15,receive_timeout=60).save(str(partial)))
                validate_mp3(partial)
                partial.replace(path)
            finally:
                partial.unlink(missing_ok=True)
        print(f"{'CACHE' if cache_hit else 'TTS'} {language} {voice} {rate} {digest[:12]}",flush=True)
    return path, dict(settings,cache_hit=cache_hit,cache_file=path.name)


def frames(path):
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes()


def silence(path, milliseconds):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * (SAMPLE_RATE * milliseconds // 1000))


NORMALIZATION = "loudnorm=I=-20:TP=-2:LRA=7"
NORMALIZATION_VERSION = "norm-v1"


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        while block := file.read(1024 * 1024):
            check()
            digest.update(block)
    return digest.hexdigest()


def normalized_clip(path):
    """Content-verified persistent PCM cache; same shards synchronize CLI/web."""
    target = path.with_suffix('.' + NORMALIZATION_VERSION + '.wav')
    manifest = target.with_suffix('.json')
    locks = path.parent / '.locks'
    locks.mkdir(exist_ok=True)
    digest = hashlib.sha256(target.name.encode()).hexdigest()
    with (locks / (digest[:2] + '.lock')).open('a') as lock:
        while True:
            check()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.1)
        try:
            source_hash = file_hash(path)
            try:
                info = json.loads(manifest.read_text())
                with wave.open(str(target), 'rb') as audio:
                    valid = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) == (1, 2, SAMPLE_RATE, 'NONE')
                    count = audio.getnframes()
                if valid and count > 0 and info == dict(source=source_hash, wav=file_hash(target), frames=count):
                    return target, True
            except (OSError, ValueError, KeyError, EOFError, wave.Error):
                pass
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.partial.wav', delete=False) as file:
                partial = Path(file.name)
            metadata = None
            try:
                ffmpeg('-i', path, '-af', NORMALIZATION, '-ac', '1', '-ar', SAMPLE_RATE, '-c:a', 'pcm_s16le', partial)
                count = frames(partial)
                if count <= 0:
                    raise ValueError('Empty normalized clip')
                info = dict(source=source_hash, wav=file_hash(partial), frames=count)
                with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, suffix='.partial.json', delete=False) as file:
                    json.dump(info, file)
                    metadata = Path(file.name)
                partial.replace(target)
                metadata.replace(manifest)
            finally:
                partial.unlink(missing_ok=True)
                if metadata:
                    metadata.unlink(missing_ok=True)
            return target, False
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def assemble(clips, directory, output, settings, normalization_stats=None):
    normalized = {}
    for pair in clips:
        for path in pair.values():
            if path not in normalized:
                target, hit = normalized_clip(path)
                normalized[path] = target
                if normalization_stats is not None:
                    normalization_stats.append(dict(cache_file=target.name, cache_hit=hit))
    pauses = {}
    for ms in {settings.language_pause, settings.repeat_pause, settings.card_pause}:
        path = directory / f"silence_{ms}.wav"
        silence(path, ms)
        pauses[ms] = path
    sequence, timeline = [], []
    position = 0
    def add(path, kind, **metadata):
        nonlocal position
        count = frames(path)
        if count == 0:
            return
        sequence.append(path)
        timeline.append(dict(kind=kind, start_frame=position, end_frame=position+count, **metadata))
        position += count
    for index, pair in enumerate(clips):
        for repeat in range(settings.repetitions):
            add(normalized[pair["pl"]], "speech", pair=index+1, repeat=repeat+1, language="pl")
            add(pauses[settings.language_pause], "pause", milliseconds=settings.language_pause)
            add(normalized[pair["ru"]], "speech", pair=index+1, repeat=repeat+1, language="ru")
            if repeat < settings.repetitions - 1:
                add(pauses[settings.repeat_pause], "pause", milliseconds=settings.repeat_pause)
        if index < len(clips)-1:
            add(pauses[settings.card_pause], "pause", milliseconds=settings.card_pause)
    # Only internally generated, absolute hash filenames enter this list.
    concat = directory / "concat.txt"
    concat.write_text("".join("file '" + str(p).replace("'", "'\\''") + "'\n" for p in sequence))
    ffmpeg("-f", "concat", "-safe", "0", "-i", concat, "-c:a", "libmp3lame", "-b:a", "128k", output)
    return timeline


def check_pauses(output, timeline, directory):
    decoded = directory / "decoded.wav"
    ffmpeg("-i", output, "-ac", "1", "-ar", SAMPLE_RATE, "-c:a", "pcm_s16le", decoded)
    with wave.open(str(decoded), "rb") as audio:
        if abs(audio.getnframes() - timeline[-1]["end_frame"]) > SAMPLE_RATE // 10:
            raise ValueError("Unexpected decoded duration")
        for entry in timeline:
            if entry["kind"] != "pause":
                continue
            length = entry["end_frame"] - entry["start_frame"]
            padding = min(int(SAMPLE_RATE * 0.06), length // 4)
            count = entry["end_frame"] - entry["start_frame"] - 2*padding
            if count <= 0:
                continue
            audio.setpos(entry["start_frame"] + padding)
            samples = array("h", audio.readframes(count))
            rms = math.sqrt(sum(s*s for s in samples) / len(samples))
            if rms > 32768 * 10**(-55/20):
                raise ValueError("Pause validation failed")


def validate_budget(pairs, settings):
    validate_pairs(pairs)
    unique = {(language,pair[language]) for pair in pairs for language in ('pl','ru')}
    if len(unique) > MAX_UNIQUE:
        raise ValueError('Максимум 200 уникальных речевых фрагментов за задание; разбейте список')
    estimate = sum(sum(len(pair[k]) / (8 * (1 + rate / 100)) + 1.5 for k,rate in (('pl',settings.pl_rate),('ru',settings.ru_rate))) for pair in pairs) * settings.repetitions
    estimate += len(pairs) * (settings.repetitions * settings.language_pause + (settings.repetitions-1) * settings.repeat_pause) / 1000
    estimate += (len(pairs)-1) * settings.card_pause / 1000
    if estimate > MAX_AUDIO_SECONDS:
        raise ValueError('Расчётная длительность больше 30 минут; уменьшите список или повторения')
    return estimate


async def synthesize_pairs(pairs, voices, rates, cache, progress):
    # Deduplicate before scheduling. Cancel and await every child on any failure.
    keys = list(dict.fromkeys((language, pair[language]) for pair in pairs for language in ('pl', 'ru')))
    semaphore = asyncio.Semaphore(3)
    completed = 0
    async def one(key):
        nonlocal completed
        async with semaphore:
            check()
            language, text = key
            result = await synthesize(text, language, voices[language]['ShortName'], rates[language], cache)
            completed += 1
            progress('Синтез речи', completed, len(keys))
            return result
    progress('Синтез речи', 0, len(keys))
    tasks = [asyncio.create_task(one(key)) for key in keys]
    try:
        results = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    paths = {key: result[0] for key, result in zip(keys, results)}
    return [{language: paths[language, pair[language]] for language in ('pl', 'ru')} for pair in pairs], [r[1] for r in results]


async def generate_audio(pairs, output, settings=None, progress=None):
    cache = ROOT / 'cache'
    cache.mkdir(exist_ok=True)
    # Cross-process shared lease protects cached clips until assembly has finished.
    async with cache_lock(cache / '.usage.lock', shared=True):
        return await _generate_audio(pairs, output, settings, progress)


async def _generate_audio(pairs, output, settings=None, progress=None):
    settings = settings or Settings()
    validate_budget(pairs,settings)
    check()
    progress = progress or (lambda stage, done=0, total=0: None)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing {tool}")
    available = await voices_list()
    voices = {"pl": choose_voice(available, "pl-PL", settings.pl_voice), "ru": choose_voice(available, "ru-RU", settings.ru_voice)}
    rates = {"pl": f"{settings.pl_rate:+d}%", "ru": f"{settings.ru_rate:+d}%"}
    cache = ROOT / "cache"
    cache.mkdir(exist_ok=True)
    started = time.monotonic()
    clips, requests = await synthesize_pairs(pairs, voices, rates, cache, progress)
    synthesis_seconds = time.monotonic() - started
    assembly_started = time.monotonic()
    normalization_stats = []
    progress("Сборка аудио", len(requests), len(requests))
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tts-", dir=output.parent) as temporary:
        directory = Path(temporary)
        pending = directory / "assembled.mp3"
        timeline = assemble(clips, directory, pending, settings, normalization_stats)
        details = probe(pending)  # check_pauses below performs the full decode once.
        if float(details["format"]["duration"]) > MAX_AUDIO_SECONDS:
            raise ValueError("Максимальная длительность аудио — 30 минут")
        check_pauses(pending, timeline, directory)
        pending.replace(output)
    report = dict(service="Microsoft Edge Online TTS", edge_tts_version=edge_tts.__version__, voices=voices, settings=asdict(settings), requests=requests,
                  sample_rate=SAMPLE_RATE, timeline=timeline, duration_seconds=float(details["format"]["duration"]),
                  size_bytes=output.stat().st_size, ffmpeg_decode="PASS", pauses="PASS",
                  normalization_cache=normalization_stats, timings=dict(synthesis_seconds=synthesis_seconds, assembly_seconds=time.monotonic()-assembly_started))
    output.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
