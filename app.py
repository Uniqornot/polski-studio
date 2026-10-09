"""LAN-only service: shared normalization, cancellable bounded queue, durable status."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import json
import logging
import os
import re
import threading
import uuid
import time
import tempfile
import fcntl
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
import uvicorn

from control import Control, CURRENT, JobStopped
from parser import normalize_input
from tts import ROOT, Settings, generate_audio, validate_budget
from video import create_subtitles, render_video, preflight

ALLOWED_ORIGINS = frozenset(x.strip().rstrip('/') for x in os.getenv('TTS_ALLOWED_ORIGINS', '').split(',') if x.strip())

OUTPUT = ROOT / 'output'
OUTPUT.mkdir(exist_ok=True)
JOB_PATTERN = re.compile(r'\d{8}-\d{6}-[a-f0-9]{12}')
executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='generation')
slots = threading.BoundedSemaphore(4)
registry = {}
status_memory = {}
registry_lock = threading.RLock()


def save_status(directory, **status):
    with registry_lock:
        status_memory[directory.name] = status
        try:
            temporary = directory / 'status.tmp'
            temporary.write_text(json.dumps(status,ensure_ascii=False),encoding='utf-8')
            temporary.replace(directory / 'status.json')
            status_memory.pop(directory.name,None)
        except OSError:
            # A disk error must not leave the UI polling 'running' forever.
            logging.error('Status write failed for %s; using memory fallback',directory.name)


def read_status(directory):
    with registry_lock:
        if directory.name in status_memory:
            return status_memory[directory.name]
        try:
            data = json.loads((directory / 'status.json').read_text())
            if not isinstance(data,dict) or data.get('state') not in ('queued','running','done','failed','cancelled') or not isinstance(data.get('stage'),str):
                raise ValueError('Bad status')
            return data
        except (OSError,ValueError):
            return dict(state='failed',stage='Статус повреждён. Запустите задание заново.')


def recover_statuses():
    for directory in OUTPUT.iterdir():
        if not directory.is_dir() or directory.is_symlink() or not JOB_PATTERN.fullmatch(directory.name):
            continue
        status = read_status(directory)
        if status.get('state') in ('queued','running'):
            save_status(directory,state='failed',stage='Генерация прервана перезапуском. Запустите снова.')
        elif not (directory / 'status.json').is_file() or status.get('stage','').startswith('Статус повреждён'):
            save_status(directory,**status)


DOWNLOAD_TTL = 600
active_downloads = {}


def read_retention(directory):
    try:
        data = json.loads((directory / 'retention.json').read_text())
        return {k: float(v) for k, v in data.items() if k in ('mp3', 'mp4')}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


CACHE_CLIP_PATTERN = re.compile(r'(?:pl|ru)_[a-f0-9]{64}\.mp3')


def write_json_atomic(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, suffix='.tmp', delete=False) as file:
            temporary = Path(file.name)
            json.dump(data, file)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def prepare_cleanup(directory, deadline):
    path = directory / 'cache-retention.json'
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get('clips'), list) and isinstance(data.get('deadline'), (int, float)):
            return data
    except (OSError, ValueError):
        pass
    clips = []
    try:
        report = json.loads((directory / 'audio.report.json').read_text())
        clips = sorted({request['cache_file'] for request in report.get('requests', [])
                        if isinstance(request, dict) and isinstance(request.get('cache_file'), str)
                        and CACHE_CLIP_PATTERN.fullmatch(request['cache_file'])})
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    data = dict(deadline=deadline, clips=clips, reports_cleaned=False, cache_cleaned=False)
    write_json_atomic(path, data)
    return data


def evict_clips(clips):
    if not clips:
        return True
    cache = ROOT / 'cache'
    cache.mkdir(exist_ok=True)
    with (cache / '.usage.lock').open('a') as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            for name in clips:
                if not isinstance(name, str) or not CACHE_CLIP_PATTERN.fullmatch(name):
                    continue
                stem = Path(name).stem
                paths = [cache / name]
                paths += [path for path in cache.glob(stem + '.norm-v*')
                          if re.fullmatch(re.escape(stem) + r'\.norm-v\d+\.(?:wav|json)', path.name)]
                for path in paths:
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
            return True
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)


def mark_download(directory, kind):
    with registry_lock:
        deadlines = read_retention(directory)
        if kind in deadlines:
            return  # First successful download starts the timer; repeats do not extend it.
        deadlines[kind] = time.time() + DOWNLOAD_TTL
        write_json_atomic(directory / 'retention.json', deadlines)
        prepare_cleanup(directory, min(deadlines.values()))



def sweep_downloads(now=None):
    now = time.time() if now is None else now
    with registry_lock:
        for directory in OUTPUT.iterdir():
            if directory.is_symlink() or not directory.is_dir() or not JOB_PATTERN.fullmatch(directory.name):
                continue
            if read_status(directory)['state'] in ('queued', 'running'):
                continue
            deadlines = read_retention(directory)
            cleanup = prepare_cleanup(directory, min(deadlines.values())) if deadlines else None
            for kind, deadline in deadlines.items():
                if deadline > now or active_downloads.get((directory.name, kind), 0):
                    continue
                for name in (result_name(directory.name, kind), 'audio.mp3' if kind == 'mp3' else 'video.mp4'):
                    path = directory / name
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
            if cleanup and cleanup['deadline'] <= now and (not cleanup.get('reports_cleaned') or not cleanup.get('cache_cleaned')):
                before = json.dumps(cleanup, sort_keys=True)
                if not cleanup.get('reports_cleaned'):
                    for name in ('audio.report.json', 'input.txt', 'request.json', 'subtitles.ass', 'video.partial.mp4'):
                        path = directory / name
                        if path.is_file() and not path.is_symlink():
                            path.unlink()
                    cleanup['reports_cleaned'] = True
                if not cleanup.get('cache_cleaned'):
                    cleanup['cache_cleaned'] = evict_clips(cleanup['clips'])
                    if cleanup['cache_cleaned']:
                        cleanup['clips'] = []
                if json.dumps(cleanup, sort_keys=True) != before:
                    write_json_atomic(directory / 'cache-retention.json', cleanup)



async def retention_worker():
    while True:
        try:
            await asyncio.to_thread(sweep_downloads)
        except OSError:
            logging.error('Downloaded artifact cleanup failed; will retry')
        await asyncio.sleep(1)


class DownloadResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        key = (self.directory.name, self.kind)
        with registry_lock:
            deadline = read_retention(self.directory).get(self.kind)
            expired = deadline is not None and deadline <= time.time()
            if not expired:
                active_downloads[key] = active_downloads.get(key, 0) + 1
        if expired:
            return await JSONResponse({'detail':'Файл удалён через 10 минут после скачивания'}, status_code=410)(scope, receive, send)
        # Track body completion even if an ASGI server offers offloaded pathsend.
        scope = {**scope, 'extensions': {k:v for k,v in scope.get('extensions', {}).items() if k != 'http.response.pathsend'}}
        completed = False
        async def tracked_send(message):
            nonlocal completed
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                completed = True
        try:
            await super().__call__(scope, receive, tracked_send)
            headers = dict(scope.get('headers', []))
            if completed and scope['method'] == 'GET' and b'range' not in headers:
                try:
                    mark_download(self.directory, self.kind)
                except OSError:
                    logging.error('Could not persist download expiry')
        finally:
            with registry_lock:
                active_downloads[key] -= 1
                if not active_downloads[key]:
                    active_downloads.pop(key)


@asynccontextmanager
async def lifespan(app):
    recover_statuses()
    retention = asyncio.create_task(retention_worker())
    try:
        yield
    finally:
        retention.cancel()
        await asyncio.gather(retention, return_exceptions=True)
    with registry_lock:
        for entry in registry.values():
            entry['control'].cancelled.set()
    executor.shutdown(wait=True)


class BodyGuard:
    """Public ASGI receive replay, without private Starlette Request attributes."""
    def __init__(self,app):
        self.app = app

    async def __call__(self,scope,receive,send):
        if scope['type'] != 'http':
            return await self.app(scope,receive,send)
        headers = dict(scope['headers'])
        if scope['method'] in ('POST','DELETE'):
            host = headers.get(b'host',b'').decode()
            origin = headers.get(b'origin',b'').decode()
            if (origin and origin not in ALLOWED_ORIGINS and origin != f"{scope['scheme']}://{host}") or headers.get(b'sec-fetch-site') == b'cross-site':
                return await JSONResponse({'detail':'Недопустимый источник запроса'},status_code=403)(scope,receive,send)
            chunks, total = [], 0
            receive_deadline = asyncio.get_running_loop().time() + 15
            while True:
                try:
                    message = await asyncio.wait_for(receive(),timeout=max(.001,receive_deadline-asyncio.get_running_loop().time()))
                except asyncio.TimeoutError:
                    return await JSONResponse({'detail':'Истекло время передачи запроса'},status_code=408)(scope,receive,send)
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body',b'')
                total += len(chunk)
                if total > 700000:
                    return await JSONResponse({'detail':'Слишком большой запрос'},status_code=413)(scope,receive,send)
                chunks.append(chunk)
                if not message.get('more_body',False):
                    break
            replayed = False
            original_receive = receive
            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return dict(type='http.request',body=b''.join(chunks),more_body=False)
                return await original_receive()
            receive = replay
        async def safe_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = list(message.get('headers',[])) + [(b'x-content-type-options',b'nosniff')]
            await send(message)
        await self.app(scope,receive,safe_send)


app = FastAPI(title='Polski · карточки',lifespan=lifespan)
app.add_middleware(BodyGuard)
app.mount('/static',StaticFiles(directory=ROOT / 'static'),name='static')


class TextInput(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    text: str = Field(max_length=100000)

class Generation(TextInput):
    pl_rate: int = Field(default=-20,ge=-50,le=50)
    ru_rate: int = Field(default=5,ge=-50,le=50)
    repetitions: int = Field(default=3,ge=1,le=5)
    language_pause: int = Field(default=600,ge=0,le=5000)
    repeat_pause: int = Field(default=900,ge=0,le=5000)
    card_pause: int = Field(default=1500,ge=0,le=5000)


@app.get('/healthz')
def health():
    return {'status':'ok'}

@app.get('/')
def index():
    return FileResponse(ROOT / 'templates/index.html')

@app.post('/api/normalize')
def normalize(body: TextInput):
    return normalize_input(body.text).response()


def release_slot(job_id):
    with registry_lock:
        entry = registry.get(job_id)
        if entry and not entry['released']:
            entry['released'] = True
            slots.release()


def result_name(job_id, kind):
    date, clock, suffix = job_id.split('-')
    return f'polski_{date[:4]}-{date[4:6]}-{date[6:]}_{clock[:2]}-{clock[2:4]}-{clock[4:]}_{suffix}.{kind}'


def result_path(directory, kind):
    named = directory / result_name(directory.name, kind)
    return named if named.is_file() else directory / ('audio.mp3' if kind == 'mp3' else 'video.mp4')


def downloads(directory):
    result = {}
    for kind in ('mp3','mp4'):
        deadline = read_retention(directory).get(kind)
        if (deadline is None or deadline > time.time()) and result_path(directory, kind).is_file():
            result[kind] = f'/api/jobs/{directory.name}/download/{kind}'
    return result


def worker(directory,pairs,settings,control):
    token = CURRENT.set(control)
    def progress(stage,done=0,total=0):
        control.check()
        save_status(directory,state='running',stage=stage,done=done,total=total)
    try:
        control.check()
        report = asyncio.run(generate_audio(pairs,directory / 'audio.mp3',settings,progress))
        progress('Создание субтитров')
        create_subtitles(pairs,report,directory / 'subtitles.ass')
        progress('Создание видео')
        video = render_video(directory,report['duration_seconds'])
        for kind, name in (('mp3', 'audio.mp3'), ('mp4', 'video.mp4')):
            (directory / name).replace(directory / result_name(directory.name, kind))
        report['video'] = video
        (directory / 'audio.report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        save_status(directory,state='done',stage='Готово',video=video,downloads=downloads(directory))
    except JobStopped as error:
        save_status(directory,state='cancelled' if control.cancelled.is_set() else 'failed',stage=str(error),downloads=downloads(directory))
    except Exception as error:
        logging.error('Generation %s failed: %s',directory.name,type(error).__name__)
        save_status(directory,state='failed',stage='Не удалось создать видео. Попробуйте снова.',downloads=downloads(directory))
    finally:
        CURRENT.reset(token)
        release_slot(directory.name)
        with registry_lock:
            registry.pop(directory.name,None)


@app.post('/api/jobs',status_code=202)
def create_job(body: Generation):
    normalized = normalize_input(body.text)
    response = normalized.response()
    if normalized.errors:
        raise HTTPException(422,detail=response)
    settings = Settings(**body.model_dump(exclude={'text'}))
    try:
        validate_budget(normalized.pairs,settings)
    except ValueError as error:
        raise HTTPException(422,detail=str(error))
    errors = preflight(normalized.pairs)
    if errors:
        response.update(errors=errors,invalid_count=len(errors))
        raise HTTPException(422,detail=response)
    if not slots.acquire(blocking=False):
        raise HTTPException(429,detail='Очередь заполнена. Попробуйте позже.')
    job_id = datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:12]
    directory = OUTPUT / job_id
    try:
        directory.mkdir(mode=0o700)
        (directory / 'input.txt').write_text(normalized.normalized_text,encoding='utf-8')
        (directory / 'request.json').write_text(json.dumps(body.model_dump() | {'text':normalized.normalized_text},ensure_ascii=False,indent=2),encoding='utf-8')
        control = Control()
        with registry_lock:
            registry[job_id] = dict(control=control,future=None,released=False)
            save_status(directory,state='queued',stage='В очереди')
            registry[job_id]['future'] = executor.submit(worker,directory,normalized.pairs,settings,control)
    except Exception:
        if job_id in registry:
            release_slot(job_id)
            registry.pop(job_id,None)
        else:
            slots.release()
        raise
    return {'id':job_id,**response}


def job_directory(job_id):
    if not JOB_PATTERN.fullmatch(job_id):
        raise HTTPException(404,detail='Задание не найдено')
    directory = OUTPUT / job_id
    if not directory.is_dir() or directory.is_symlink():
        raise HTTPException(404,detail='Задание не найдено')
    return directory

@app.get('/api/jobs/{job_id}')
def job_status(job_id: str):
    directory = job_directory(job_id)
    status = read_status(directory)
    return {**status, 'downloads': downloads(directory)} if status['state'] not in ('queued', 'running') else status

@app.post('/api/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    directory = job_directory(job_id)
    with registry_lock:
        # Worker terminal writes use the same lock. Preserve done during its finally.
        status = read_status(directory)
        if status['state'] not in ('queued','running'):
            return status
        entry = registry.get(job_id)
        if entry:
            entry['control'].cancelled.set()
            if entry['future'] and entry['future'].cancel():
                save_status(directory,state='cancelled',stage='Задание отменено',downloads=downloads(directory))
                release_slot(job_id)
                registry.pop(job_id,None)
            else:
                save_status(directory,state='running',stage='Отмена…')
    return read_status(directory)

@app.get('/api/jobs/{job_id}/download/{kind}')
def download(job_id: str,kind: str):
    directory = job_directory(job_id)
    filenames = {'mp3':'audio.mp3','mp4':'video.mp4'}
    if kind not in filenames:
        raise HTTPException(404)
    deadline = read_retention(directory).get(kind)
    if deadline is not None and deadline <= time.time():
        raise HTTPException(410, detail='Файл удалён через 10 минут после скачивания')
    path = result_path(directory, kind)
    if read_status(directory)['state'] in ('queued','running') or not path.is_file():
        raise HTTPException(409,detail='Результат ещё не готов')
    response = DownloadResponse(path,media_type='audio/mpeg' if kind == 'mp3' else 'video/mp4',filename=result_name(job_id, kind))
    response.directory, response.kind = directory, kind
    return response


if __name__ == '__main__':
    uvicorn.run(app,host=os.getenv('TTS_HOST','127.0.0.1'),port=int(os.getenv('TTS_PORT','8080')))
