import json
from pathlib import Path
import tempfile
import unittest
from parser import parse_text
from tts import Settings
from video import layout, ass_text, create_subtitles


class Tests(unittest.TestCase):
    def test_parser_preserves_hyphens_and_first_separator(self):
        pairs, errors = parse_text("\n  z sosem słodko-kwaśnym - с кисло-сладким соусом  \n3 dni - 3 дня - подряд\n")
        self.assertFalse(errors)
        self.assertEqual(pairs[0], {"pl": "z sosem słodko-kwaśnym", "ru": "с кисло-сладким соусом"})
        self.assertEqual(pairs[1]["ru"], "3 дня - подряд")

    def test_parser_errors_limits(self):
        pairs, errors = parse_text("szlaban - шлагбаум\nошибка\n - нет")
        self.assertEqual(len(pairs), 1)
        self.assertIn("Строка 2", errors[0])
        self.assertTrue(parse_text("x" * 100001)[1])
        self.assertTrue(parse_text("a - б\n" * 501)[1])

    def test_settings_boundaries(self):
        self.assertEqual(Settings().pl_rate, -20)
        self.assertEqual(Settings().ru_rate, 5)
        Settings(pl_rate=-50, ru_rate=50, repetitions=5, language_pause=0)
        for kwargs in ({"pl_rate": -51}, {"ru_rate": 51}, {"repetitions": 0}, {"card_pause": -1}):
            with self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_layout_and_ass_injection(self):
        from PIL import ImageFont
        from video import BOLD, FONT
        for language, text in (("pl", "pod wpływem narkotyków (naćpani) " * 8), ("ru", "сверхдлинноеслово" * 30)):
            lines, size = layout(text, language)
            font = ImageFont.truetype(str(BOLD if language == "pl" else FONT), size)
            self.assertTrue(all(font.getlength(line) <= 880 for line in lines))
            self.assertLessEqual(len(lines) * size * 1.4, 440)
        self.assertNotIn("{", ass_text([r"{\pos(0,0)}hello"]))

    def test_highlights_follow_sample_timeline(self):
        report = {"duration_seconds": 4, "timeline": [
            {"kind": "speech", "pair": 1, "language": "pl", "start_frame": 0, "end_frame": 24000},
            {"kind": "pause", "start_frame": 24000, "end_frame": 38400},
            {"kind": "speech", "pair": 1, "language": "ru", "start_frame": 38400, "end_frame": 72000},
            {"kind": "pause", "start_frame": 72000, "end_frame": 96000}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ass"
            create_subtitles([{"pl": "szlaban", "ru": "шлагбаум"}], report, path)
            text = path.read_text()
            self.assertIn("0:00:00.00,0:00:01.60", text)
            self.assertIn("0:00:01.60,0:00:04.00", text)
            self.assertEqual(text.count("Dialogue:"), 4)



# Review regression tests: no external speech requests are made by this suite.
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
import subprocess
import sys
import threading
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
import app as service
import tts
from parser import normalize_input
from control import Control, CURRENT, JobStopped, run_process

RAW = '''3 dni - 3 дня  
pod wpływem narkotyków (naćpani) - под воздействием наркотиков  
szlaban - шлагбаум  
jest otwarty - он открыт  
otwiera się - открывается  
cieszyć się - радоваться / наслаждаться  

z sosem słodko-kwaśnym - с кисло-сладким соусом  

żurek  
podkłady dla psa'''

class NormalizeTests(unittest.TestCase):
    def test_real_input(self):
        result = normalize_input(RAW)
        self.assertEqual(len(result.pairs),7)
        self.assertEqual([e['line'] for e in result.errors],[8,9])
        self.assertTrue(result.normalized_text.endswith('żurek\npodkłady dla psa'))
        self.assertNotIn('  ',result.normalized_text)

    def test_trailing_spaces_empty_crlf(self):
        result = normalize_input(' szlaban - шлагбаум  \r\n\r\n \rjest otwarty - он открыт  ')
        self.assertEqual(result.normalized_text,'szlaban - шлагбаум\njest otwarty - он открыт')

    def test_unicode_spaces_tabs(self):
        result = normalize_input('szlaban\u00a0\u202f-\u2009шлагбаум\nżurek\t-\tжурек')
        self.assertFalse(result.errors)
        self.assertEqual(result.pairs[0],{'pl':'szlaban','ru':'шлагбаум'})

    def test_dash_variants(self):
        for dash in ('-','–','—'):
            self.assertEqual(normalize_input(f'szlaban {dash} шлагбаум').normalized_text,'szlaban - шлагбаум')

    def test_hyphens_parentheses_slash_and_case(self):
        text = 'z sosem słodko-kwaśnym - с кисло-сладким соусом\nplanuję (bez się) - я планирую\nw fabryce - на фабрике / на заводе'
        self.assertEqual(normalize_input(text).normalized_text,text)
        self.assertFalse(normalize_input(text).errors)

    def test_untranslated_and_missing_polish(self):
        result = normalize_input('żurek\nzakwas -\n- закваска')
        self.assertEqual([e['message'] for e in result.errors],['Отсутствует перевод','Отсутствует перевод','Отсутствует польская фраза'])
        self.assertEqual(result.normalized_text,'żurek\nzakwas -\n- закваска')

    def test_markdown_lists(self):
        result = normalize_input('- szlaban - шлагбаум\n* jest otwarty - он открыт\n• otwiera się - открывается')
        self.assertEqual(len(result.pairs),3)
        self.assertFalse(result.errors)
        self.assertTrue(result.normalized_text.startswith('szlaban'))

    def test_numbered_lists_preserve_numeric_phrase(self):
        result = normalize_input('1. szlaban - шлагбаум\n2. jest otwarty - он открыт\n3 dni - 3 дня')
        self.assertEqual(result.pairs[-1]['pl'],'3 dni')
        self.assertEqual(result.pairs[0]['pl'],'szlaban')

    def test_polish_and_russian_characters(self):
        text='ą ć ę ł ń ó ś ź ż - ё й'
        self.assertEqual(normalize_input(text).normalized_text,text)

    def test_idempotence(self):
        first=normalize_input('  • szlaban\u00a0—\tшлагбаум  \r\nżurek')
        self.assertEqual(normalize_input(first.normalized_text),first)

    def test_first_separator_only(self):
        self.assertEqual(normalize_input('3 dni - 3 дня - подряд').pairs[0]['ru'],'3 дня - подряд')

    def test_empty_and_control_characters(self):
        self.assertTrue(normalize_input('  \n\t').errors)
        self.assertTrue(normalize_input('a\x00 - б').errors)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.stack=ExitStack()
        self.stack.enter_context(patch.object(service,'OUTPUT',Path(self.tmp.name)))
        self.stack.enter_context(patch.object(service,'registry',{}))
        self.stack.enter_context(patch.object(service,'status_memory',{}))
        self.stack.enter_context(patch.object(service,'slots',threading.BoundedSemaphore(4)))
        self.client=TestClient(service.app)

    def tearDown(self):
        self.stack.close();self.tmp.cleanup()

    def test_normalize_endpoint_real_input(self):
        r=self.client.post('/api/normalize',json={'text':RAW})
        self.assertEqual(r.status_code,200)
        self.assertEqual((r.json()['valid_count'],r.json()['invalid_count']),(7,2))

    def test_invalid_generate_does_not_submit(self):
        with patch.object(service.executor,'submit') as submit:
            r=self.client.post('/api/jobs',json={'text':RAW})
            self.assertEqual(r.status_code,422)
            self.assertEqual(r.json()['detail']['invalid_count'],2)
            submit.assert_not_called()

    def test_generate_normalizes_on_server(self):
        with patch.object(service.executor,'submit',return_value=Future()):
            r=self.client.post('/api/jobs',json={'text':' • szlaban\t—\u00a0шлагбаум  '})
            self.assertEqual(r.status_code,202)
            directory=service.OUTPUT/r.json()['id']
            self.assertEqual((directory/'input.txt').read_text(),'szlaban - шлагбаум')
            self.assertEqual(r.json()['valid_count'],1)
            service.cancel_job(r.json()['id'])

    def test_card_preflight_before_synthesis(self):
        with patch.object(service,'preflight',return_value=[dict(line=1,text='x',message='too long')]), patch.object(service.executor,'submit') as submit:
            self.assertEqual(self.client.post('/api/jobs',json={'text':'x - х'}).status_code,422)
            submit.assert_not_called()

    def test_http_body_limit(self):
        self.assertEqual(self.client.post('/api/normalize',content=b'x'*700001,headers={'content-type':'application/json'}).status_code,413)

    def test_strict_settings_and_origin(self):
        for options in ({'pl_rate':'-20'},{'pl_rate':-51},{'repetitions':6},{'voice':'custom'}):
            self.assertEqual(self.client.post('/api/jobs',json={'text':'x - х',**options}).status_code,422)
        self.assertEqual(self.client.post('/api/normalize',json={'text':'x - х'},headers={'origin':'https://example.com'}).status_code,403)

    def test_queue_and_queued_cancel_release(self):
        with patch.object(service.executor,'submit',side_effect=lambda *a: Future()):
            ids=[self.client.post('/api/jobs',json={'text':'x - х'}).json()['id'] for _ in range(4)]
            self.assertEqual(self.client.post('/api/jobs',json={'text':'x - х'}).status_code,429)
            r=self.client.post('/api/jobs/'+ids[1]+'/cancel')
            self.assertEqual(r.json()['state'],'cancelled')
            self.assertEqual(self.client.post('/api/jobs',json={'text':'x - х'}).status_code,202)

    def test_corrupt_and_interrupted_statuses(self):
        bad=service.OUTPUT/'20261008-150000-000000000001';bad.mkdir();(bad/'status.json').write_text('{')
        queued=service.OUTPUT/'20261008-150000-000000000002';queued.mkdir();(queued/'status.json').write_text('{"state":"running"}')
        service.recover_statuses()
        self.assertEqual(service.read_status(bad)['state'],'failed')
        self.assertEqual(service.read_status(queued)['state'],'failed')

    def test_disk_status_failure_falls_back_to_memory(self):
        d=service.OUTPUT/'20261008-150000-000000000003';d.mkdir()
        with patch.object(Path,'write_text',side_effect=OSError('disk full')):
            service.save_status(d,state='failed',stage='failed')
        self.assertEqual(service.read_status(d)['state'],'failed')

    def test_valid_audio_download_after_video_failure(self):
        d=service.OUTPUT/'20261008-150000-000000000004';d.mkdir();(d/'audio.mp3').write_bytes(b'audio')
        service.save_status(d,state='failed',stage='failed')
        r=self.client.get(f'/api/jobs/{d.name}/download/mp3')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.content,b'audio')

    def test_cancel_preserves_terminal_status_before_registry_cleanup(self):
        # Reproduce exactly: terminal state exists but worker has not popped registry.
        for state in ('done','failed','cancelled'):
            # Use accepted hex job IDs for actual HTTP regression.
            d=service.OUTPUT/('20261008-150000-'+{'done':'000000000010','failed':'000000000011','cancelled':'000000000012'}[state])
            d.mkdir();(d/'audio.mp3').write_bytes(b'finished audio')
            future=Future();future.set_running_or_notify_cancel()
            control=Control()
            service.registry[d.name]=dict(control=control,future=future,released=False)
            expected=dict(state=state,stage='terminal',downloads=service.downloads(d))
            service.save_status(d,**expected)
            r=self.client.post(f'/api/jobs/{d.name}/cancel')
            self.assertEqual(r.json(),expected)
            self.assertEqual(service.read_status(d),expected)
            self.assertFalse(control.cancelled.is_set())
            self.assertEqual(self.client.get(f'/api/jobs/{d.name}/download/mp3').status_code,200)

    def test_cancel_running_sets_event(self):
        d=service.OUTPUT/'20261008-150000-000000000013';d.mkdir()
        future=Future();future.set_running_or_notify_cancel();control=Control()
        service.registry[d.name]=dict(control=control,future=future,released=False)
        service.save_status(d,state='running',stage='render')
        r=self.client.post(f'/api/jobs/{d.name}/cancel')
        self.assertEqual(r.json()['stage'],'Отмена…')
        self.assertTrue(control.cancelled.is_set())

    def test_bad_status_schema(self):
        d=service.OUTPUT/'20261008-150000-000000000005';d.mkdir();(d/'status.json').write_text('{"state":17}')
        self.assertEqual(service.read_status(d)['state'],'failed')

    def test_submit_failure_releases_slot(self):
        with patch.object(service.executor,'submit',side_effect=RuntimeError('shutdown')):
            with self.assertRaises(RuntimeError):
                self.client.post('/api/jobs',json={'text':'x - х'})
        for _ in range(4):self.assertTrue(service.slots.acquire(blocking=False))

    def test_unknown_job_and_download_kind(self):
        self.assertEqual(self.client.get('/api/jobs/unknown').status_code,404)


class RuntimeTests(unittest.TestCase):
    def test_stderr_on_success_is_not_failure(self):
        out=run_process([sys.executable,'-c','import sys; print("ok"); print("warning",file=sys.stderr)'])
        self.assertEqual(out.strip(),'ok')

    def test_cancel_terminates_subprocess(self):
        control=Control(); timer=threading.Timer(.2,control.cancelled.set)
        token=CURRENT.set(control);timer.start()
        try:
            with self.assertRaises(JobStopped):run_process([sys.executable,'-c','import time; time.sleep(30)'])
        finally:
            timer.cancel();CURRENT.reset(token)

    def test_network_wait_cancelled(self):
        async def example():
            control=Control();token=CURRENT.set(control)
            asyncio.get_running_loop().call_later(.05,control.cancelled.set)
            try:
                from control import guarded
                with self.assertRaises(JobStopped):await guarded(asyncio.sleep(30))
            finally:CURRENT.reset(token)
        asyncio.run(example())

    def test_deadline(self):
        control=Control(seconds=-1)
        with self.assertRaises(JobStopped):control.check()

    def test_unique_and_duration_limits(self):
        with self.assertRaises(ValueError):tts.validate_budget([{'pl':str(i),'ru':'х'+str(i)} for i in range(101)],Settings())
        with self.assertRaises(ValueError):tts.validate_budget([{'pl':'x'*1000,'ru':'я'*1000}]*5,Settings(repetitions=5,pl_rate=-50,ru_rate=-50))

    def test_cli_shared_validation(self):
        with self.assertRaises(ValueError):tts.validate_budget([{'pl':'a\x00','ru':'б'}],Settings())

    def test_edge_ssml_escapes_xml(self):
        import edge_tts
        text=b''.join(edge_tts.Communicate('<tag> & ё','pl-PL-ZofiaNeural').texts).decode()
        self.assertIn('&lt;tag&gt;',text)
        self.assertIn('&amp;',text)
        self.assertIn('ё',text)

    def test_corrupt_cache_is_resynthesized(self):
        with tempfile.TemporaryDirectory() as d:
            cache=Path(d);calls=[]
            fixture=cache/'fixture.mp3'
            tts.ffmpeg('-f','lavfi','-i','sine=frequency=440:duration=0.3','-c:a','libmp3lame',fixture)
            class Fake:
                def __init__(self,*a,**kw):pass
                async def save(self,path):calls.append(1);Path(path).write_bytes(fixture.read_bytes())
            with patch.object(tts.edge_tts,'Communicate',Fake):
                path,_=asyncio.run(tts.synthesize('x','pl','pl-PL-ZofiaNeural','-20%',cache))
                path.write_bytes(b'broken')
                path,report=asyncio.run(tts.synthesize('x','pl','pl-PL-ZofiaNeural','-20%',cache))
                self.assertFalse(report['cache_hit']);self.assertEqual(len(calls),2)
                self.assertFalse(list(cache.glob('*.partial.mp3')))

    def test_zero_and_short_pauses_real_mp3(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);clip=directory/'tone.mp3'
            tts.ffmpeg('-f','lavfi','-i','sine=frequency=440:duration=0.3','-c:a','libmp3lame',clip)
            for pause in (0,50,600):
                settings=Settings(language_pause=pause,repeat_pause=pause,card_pause=pause,repetitions=2)
                output=directory/f'out{pause}.mp3'
                timeline=tts.assemble([{'pl':clip,'ru':clip}]*2,directory,output,settings)
                tts.validate_mp3(output);tts.check_pauses(output,timeline,directory)
                self.assertEqual(sum(t['kind']=='speech' for t in timeline),8)
                self.assertTrue(all(t['end_frame']>t['start_frame'] for t in timeline))

    def test_cache_lock_between_processes(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);fixture=directory/'fixture.mp3'
            tts.ffmpeg('-f','lavfi','-i','sine=frequency=440:duration=0.3','-c:a','libmp3lame',fixture)
            script='''import asyncio,sys
from pathlib import Path
import tts
cache=Path(sys.argv[1])
class Fake:
 def __init__(self,*a,**kw):pass
 async def save(self,path):
  with (cache/'calls').open('a') as f:f.write('1\\n')
  await asyncio.sleep(.3)
  Path(path).write_bytes((cache/'fixture.mp3').read_bytes())
tts.edge_tts.Communicate=Fake
asyncio.run(tts.synthesize('concurrent','pl','pl-PL-ZofiaNeural','-20%',cache))
'''
            processes=[subprocess.Popen([sys.executable,'-c',script,d],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
            for process in processes:
                out,err=process.communicate(timeout=20)
                self.assertEqual(process.returncode,0,err.decode())
            self.assertEqual((directory/'calls').read_text(),'1\n')

    def test_stored_voices_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'cache').mkdir();expected=[{'ShortName':'pl-PL-ZofiaNeural','Locale':'pl-PL'}]
            (root/'cache/voices.json').write_text(json.dumps(expected))
            with patch.object(tts,'ROOT',root),patch.object(tts.edge_tts,'list_voices',side_effect=RuntimeError('offline')):
                self.assertEqual(asyncio.run(tts.voices_list()),expected)

class SpeedTests(unittest.TestCase):
    def test_normalized_cache_hit_corruption_and_source_change(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'clip.mp3'
            tts.ffmpeg('-f','lavfi','-i','sine=frequency=440:duration=0.3','-c:a','libmp3lame',source)
            wav,hit=tts.normalized_clip(source)
            self.assertFalse(hit)
            original=wav.read_bytes()
            with patch.object(tts,'ffmpeg',side_effect=AssertionError('unexpected normalization')):
                self.assertTrue(tts.normalized_clip(source)[1])
            # Corrupt PCM payload without changing WAV headers or length.
            damaged=bytearray(original);damaged[-4]^=1;wav.write_bytes(damaged)
            self.assertFalse(tts.normalized_clip(source)[1])
            self.assertEqual(wav.read_bytes(),original)
            tts.ffmpeg('-f','lavfi','-i','sine=frequency=880:duration=0.3','-c:a','libmp3lame',source)
            self.assertFalse(tts.normalized_clip(source)[1])
            self.assertNotEqual(wav.read_bytes(),original)
            self.assertFalse(list(Path(d).glob('*.partial.*')))

    def test_parallel_synthesis_limit_dedup_and_order(self):
        async def scenario():
            active=peak=0;calls=[]
            async def fake(text,language,voice,rate,cache):
                nonlocal active,peak
                active+=1;peak=max(peak,active);calls.append((language,text))
                await asyncio.sleep(.03 if language=='pl' else .01)
                active-=1
                return Path(language+text),dict(language=language,text=text)
            pairs=[{'pl':'a','ru':'б'},{'pl':'c','ru':'д'},{'pl':'a','ru':'б'}]
            voices={l:{'ShortName':l} for l in ('pl','ru')}
            with patch.object(tts,'synthesize',fake):
                clips,requests=await tts.synthesize_pairs(pairs,voices,{'pl':'-20%','ru':'+5%'},Path('.'),lambda *a:None)
            self.assertEqual(peak,3);self.assertEqual(len(calls),4)
            self.assertEqual(clips[0],clips[2]);self.assertEqual(clips[1]['pl'],Path('plc'))
            self.assertEqual([(r['language'],r['text']) for r in requests],[('pl','a'),('ru','б'),('pl','c'),('ru','д')])
        asyncio.run(scenario())

    def test_parallel_failure_cancels_other_requests(self):
        async def scenario():
            stopped=[]
            async def fake(text,language,*args):
                if language=='pl':
                    await asyncio.sleep(.02)
                    raise ValueError('network failure')
                try:await asyncio.sleep(10)
                finally:stopped.append(language)
            with patch.object(tts,'synthesize',fake):
                with self.assertRaises(ValueError):
                    await tts.synthesize_pairs([{'pl':'a','ru':'б'}],{l:{'ShortName':l} for l in ('pl','ru')},{'pl':'0%','ru':'0%'},Path('.'),lambda *a:None)
            self.assertEqual(stopped,['ru'])
        asyncio.run(scenario())

    def test_parallel_control_cancellation(self):
        async def scenario():
            control=Control();token=CURRENT.set(control);stopped=[]
            async def fake(text,language,*args):
                async def wait():
                    try:await asyncio.sleep(10)
                    finally:stopped.append(language)
                from control import guarded
                return await guarded(wait())
            async def cancel():
                await asyncio.sleep(.03);control.cancelled.set()
            cancel_task=asyncio.create_task(cancel())
            try:
                with patch.object(tts,'synthesize',fake):
                    with self.assertRaises(JobStopped):
                        await tts.synthesize_pairs([{'pl':'a','ru':'б'}],{l:{'ShortName':l} for l in ('pl','ru')},{'pl':'0%','ru':'0%'},Path('.'),lambda *a:None)
                self.assertEqual(sorted(stopped),['pl','ru'])
            finally:
                await cancel_task;CURRENT.reset(token)
        asyncio.run(scenario())

class CleanupTests(unittest.TestCase):
    def test_normalized_cleanup_preview_apply_and_preserved_files(self):
        import cleanup, os, time
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cache=root/'cache';cache.mkdir()
            old=cache/('pl_'+'a'*64+'.norm-v1.wav');manifest=old.with_suffix('.json')
            recent=cache/('ru_'+'b'*64+'.norm-v1.wav')
            source=old.with_name('pl_'+'a'*64+'.mp3');lock=cache/'a.lock';other=cache/'manual.wav'
            for p in (old,manifest,recent,source,lock,other):p.write_bytes(b'data')
            for p in (old,manifest):os.utime(p,(time.time()-40*86400,)*2)
            linked=cache/('pl_'+'c'*64+'.norm-v1.wav');linked.symlink_to(old)
            self.assertEqual(cleanup.cleanup(root,normalized_cache_only=True),[old])
            self.assertTrue(old.exists());self.assertTrue(manifest.exists())
            cleanup.cleanup(root,apply=True,normalized_cache_only=True)
            self.assertFalse(old.exists());self.assertFalse(manifest.exists())
            for p in (recent,source,lock,other):self.assertTrue(p.exists())
            self.assertTrue(linked.is_symlink())

    def test_cleanup_missing_dirs_and_unknown_status(self):
        import cleanup, os, time
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.assertEqual(cleanup.cleanup(root),[])
            self.assertEqual(cleanup.cleanup(root,normalized_cache_only=True),[])
            job=root/'output'/'20261008-120000-aaaaaaaaaaaa';job.mkdir(parents=True)
            status=job/'status.json';status.write_text('{"state":"unknown"}')
            os.utime(status,(time.time()-40*86400,)*2)
            self.assertEqual(cleanup.cleanup(root,apply=True),[]);self.assertTrue(job.exists())

class ResultNameTests(unittest.TestCase):
    def test_named_result_and_legacy_download(self):
        with tempfile.TemporaryDirectory() as d, patch.object(service,'OUTPUT',Path(d)):
            job='20261008-150000-abcdef012345';directory=Path(d)/job;directory.mkdir()
            service.save_status(directory,state='done',stage='Готово')
            with TestClient(service.app) as client:
                for kind,legacy in [('mp3','audio.mp3'),('mp4','video.mp4')]:
                    (directory/legacy).write_bytes(b'legacy')
                    response=client.get(f'/api/jobs/{job}/download/{kind}')
                    name=f'polski_2026-10-08_15-00-00_abcdef012345.{kind}'
                    self.assertIn(name,response.headers['content-disposition'])
                    self.assertEqual(response.content,b'legacy')
                    (directory/name).write_bytes(b'new')
                    self.assertEqual(client.get(f'/api/jobs/{job}/download/{kind}').content,b'new')
            self.assertEqual(len(service.downloads(directory)),2)

class RetentionTests(unittest.TestCase):
    def test_successful_download_deadline_persistence_and_separate_files(self):
        with tempfile.TemporaryDirectory() as d, patch.object(service,'OUTPUT',Path(d)):
            job='20261008-150000-abcdef012345';directory=Path(d)/job;directory.mkdir()
            service.save_status(directory,state='done',stage='Готово')
            (directory/'audio.mp3').write_bytes(b'audio');(directory/'video.mp4').write_bytes(b'video')
            start=service.time.time()
            with TestClient(service.app) as client:
                self.assertEqual(client.head(f'/api/jobs/{job}/download/mp3').status_code,405)
                client.get(f'/api/jobs/{job}/download/mp3',headers={'Range':'bytes=0-1'})
                self.assertEqual(service.read_retention(directory),{})
                with patch.object(service.time,'time',return_value=start):
                    self.assertEqual(client.get(f'/api/jobs/{job}/download/mp3').content,b'audio')
                self.assertEqual(service.read_retention(directory),{'mp3':start+600})
                with patch.object(service.time,'time',return_value=start+100):
                    client.get(f'/api/jobs/{job}/download/mp3')
                self.assertEqual(service.read_retention(directory),{'mp3':start+600})
                service.active_downloads[(job,'mp3')]=1
                service.sweep_downloads(start+601);self.assertTrue((directory/'audio.mp3').exists())
                service.active_downloads.clear()
                service.sweep_downloads(start+599);self.assertTrue((directory/'audio.mp3').exists())
                service.recover_statuses();service.sweep_downloads(start+600)
                self.assertFalse((directory/'audio.mp3').exists());self.assertTrue((directory/'video.mp4').exists())
                with patch.object(service.time,'time',return_value=start+601):
                    self.assertEqual(client.get(f'/api/jobs/{job}/download/mp3').status_code,410)
                    self.assertNotIn('mp3',client.get(f'/api/jobs/{job}').json()['downloads'])

    def test_interrupted_response_does_not_start_timer(self):
        async def scenario(directory):
            response=service.DownloadResponse(directory/'audio.mp3')
            response.directory=directory;response.kind='mp3'
            async def receive():return {'type':'http.disconnect'}
            async def send(message):
                if message['type']=='http.response.body':raise ConnectionError('disconnected')
            with self.assertRaises(ConnectionError):
                await response({'type':'http','method':'GET','headers':[]},receive,send)
            self.assertEqual(service.read_retention(directory),{})
            self.assertFalse(service.active_downloads)
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);(directory/'audio.mp3').write_bytes(b'audio')
            asyncio.run(scenario(directory))

class PublicOriginTests(unittest.TestCase):
    def test_https_origin_behind_http_proxy(self):
        with patch.object(service,'ALLOWED_ORIGINS',frozenset({'https://polski.moon-lodz.keenetic.pro'})), TestClient(service.app) as client:
            body={'text':'szlaban - шлагбаум'}
            self.assertEqual(client.post('/api/normalize',json=body,headers={'Origin':'https://polski.moon-lodz.keenetic.pro','Sec-Fetch-Site':'same-origin'}).status_code,200)
            self.assertEqual(client.post('/api/normalize',json=body,headers={'Origin':'https://untrusted.example'}).status_code,403)
            self.assertEqual(client.post('/api/normalize',json=body,headers={'Origin':'https://polski.moon-lodz.keenetic.pro','Sec-Fetch-Site':'cross-site'}).status_code,403)

class CacheExpiryTests(unittest.TestCase):
    def test_reports_and_clips_expire_but_shared_lease_protects_active_work(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d, patch.object(service,'ROOT',Path(d)), patch.object(service,'OUTPUT',Path(d)/'output'):
            root=Path(d);cache=root/'cache';cache.mkdir();service.OUTPUT.mkdir()
            directory=service.OUTPUT/'20261009-100000-abcdef012345';directory.mkdir()
            service.save_status(directory,state='done',stage='Готово')
            clip='pl_'+'a'*64+'.mp3';stem=Path(clip).stem
            related=[cache/clip,cache/(stem+'.norm-v1.wav'),cache/(stem+'.norm-v1.json')]
            unrelated=cache/('ru_'+'b'*64+'.mp3');unrelated.write_bytes(b'keep')
            for p in related:p.write_bytes(b'cached')
            for name in ('input.txt','request.json','subtitles.ass','video.partial.mp4','audio.mp3','video.mp4'):(directory/name).write_bytes(b'data')
            (directory/'audio.report.json').write_text(json.dumps({'requests':[{'cache_file':clip},{'cache_file':'../../outside.mp3'}]}))
            start=service.time.time()
            with patch.object(service.time,'time',return_value=start):service.mark_download(directory,'mp3')
            with (cache/'.usage.lock').open('a') as lease:
                fcntl.flock(lease,fcntl.LOCK_SH)
                service.sweep_downloads(start+599)
                self.assertTrue((directory/'audio.report.json').exists())
                service.sweep_downloads(start+600)
                for name in ('input.txt','request.json','subtitles.ass','video.partial.mp4','audio.report.json','audio.mp3'):self.assertFalse((directory/name).exists())
                self.assertTrue((directory/'video.mp4').exists())
                self.assertTrue(all(p.exists() for p in related))
                self.assertFalse(json.loads((directory/'cache-retention.json').read_text())['cache_cleaned'])
                fcntl.flock(lease,fcntl.LOCK_UN)
            service.sweep_downloads(start+601)
            self.assertFalse(any(p.exists() for p in related));self.assertTrue(unrelated.exists())
            marker=json.loads((directory/'cache-retention.json').read_text())
            self.assertTrue(marker['cache_cleaned']);self.assertEqual(marker['clips'],[])
            self.assertTrue((directory/'status.json').exists());self.assertTrue((directory/'retention.json').exists())

    def test_generate_audio_acquires_cross_process_cache_lease(self):
        async def scenario(root):
            import fcntl
            async def fake(*args):
                with (root/'cache/.usage.lock').open('a') as lock:
                    with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                return {'ok':True}
            with patch.object(tts,'ROOT',root),patch.object(tts,'_generate_audio',fake):
                self.assertEqual(await tts.generate_audio([],root/'out.mp3'),{'ok':True})
            with (root/'cache/.usage.lock').open('a') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory() as d:asyncio.run(scenario(Path(d)))

if __name__ == "__main__":
    unittest.main()
