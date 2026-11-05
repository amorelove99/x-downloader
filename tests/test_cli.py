import contextlib
from email.message import Message
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xdownload import cli, jobs, media, notify, runner, state
from xdownload.progress import Reporter


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root/'data'
        self.out = self.root/'downloads'
        for target, name, value in ((state,'ROOT',self.root), (state,'DATA',self.data), (jobs,'LOCK',self.root/'lock')):
            patcher = patch.object(target,name,value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.item = {'id':'1','author':'test','url':'https://x.com/test/status/1','hasVideo':True,'images':[]}
        self.snapshot = {'status':'ok','account':'alice','items':[self.item]}
        self.config = {**state.DEFAULTS,'out':str(self.out)}
        for name,value in (('nas_config',{'mount_root':str(self.root/'nas')}),('nas_mounted',True)):
            patcher = patch.object(runner,name,return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None,None,None)

    def seed(self, snapshot=None, value=None):
        state.atomic(state.DATA/'account.json',{'account':'alice','confirmed':state.now()})
        state.atomic(runner.snapshot_path('alice'), snapshot or self.snapshot)
        state.atomic(state.DATA/'state.json', value or state.fresh())

    def execute(self, **config):
        with patch.object(runner,'scrape',return_value=self.snapshot), patch.object(runner,'deliveries',return_value=(0,None)):
            return runner.run({**self.config,**config})

    def image(self, item, source, out, report):
        target,_ = media.image_target(item,source,out)
        target.write_bytes(b'photo')
        return str(target)

    def video(self,item,out,archive,quality,report,completed,repair=False,known_files=()):
        path=out/'test-1.mp4'
        path.write_bytes(b'video')
        completed(str(path))
        return [str(path)]

    def test_direct_video_variants_selected_by_quality(self):
        item = {
            **self.item,
            "videos": [{
                "id": "v1",
                "variants": [
                    {"url": "https://video.twimg.com/v/1920x1080/hi.mp4", "bitrate": 2000, "width": 1920, "height": 1080},
                    {"url": "https://video.twimg.com/v/1280x720/mid.mp4", "bitrate": 1000, "width": 1280, "height": 720},
                ],
            }],
        }
        self.assertEqual(media.select_video_urls(item, "720"), ["https://video.twimg.com/v/1280x720/mid.mp4"])
        self.assertEqual(media.select_video_urls(item, "1080"), ["https://video.twimg.com/v/1920x1080/hi.mp4"])
        self.assertEqual(media.select_video_urls(item, "best"), ["https://video.twimg.com/v/1920x1080/hi.mp4"])

    def test_migration_preserves_all_paths_delivery_and_backup(self):
        old={'1':{'status':'downloaded','file':'/old/a.mp4','delivered':'old batch','attempts':1}}
        state.atomic(self.data/'state.json',old)
        upgraded=state.load(migrate=True)
        self.assertEqual(state.read(self.data/'state.v1.backup.json'),old)
        self.assertTrue(upgraded['entries']['1']['media']['/old/a.mp4']['delivered'])
        self.assertEqual(upgraded['entries']['1']['accounts'],[])
        self.assertEqual(state.load(),upgraded)

    def test_migration_single_file_missing_is_visible(self):
        state.atomic(self.data/'state.json',{'1':{**self.item,'status':'downloaded','file':'/absent/movie.mp4'}})
        value=state.load(migrate=True)
        self.assertEqual(value['entries']['1']['files'],['/absent/movie.mp4'])
        row=runner.plan(self.snapshot,value,'both')[0]
        self.assertEqual(row['missing'],['/absent/movie.mp4'])
        self.assertEqual(row['status'],'本地文件缺失')

    def test_migration_failed_images_keep_completed_video(self):
        self.out.mkdir()
        path=self.out/'movie.mp4';path.write_bytes(b'video')
        state.atomic(self.data/'state.json',{'1':{**self.item,'status':'failed','file':str(path),'error':'image download failed'}})
        with patch.object(media,'video') as video:
            self.assertEqual(self.execute(),0)
        video.assert_not_called()
        self.assertTrue(state.load()['entries']['1']['video_done'])

    def test_invalid_legacy_state_is_not_replaced(self):
        state.atomic(self.data/'state.json',{'1':{}})
        before=(self.data/'state.json').read_bytes()
        with self.assertRaises(ValueError): state.load(migrate=True)
        self.assertEqual((self.data/'state.json').read_bytes(),before)

    def test_images_then_both_adds_video_once(self):
        self.item['images']=['https://pbs.twimg.com/media/p?format=jpg']
        with patch.object(media,'image',side_effect=self.image) as image,patch.object(media,'video',side_effect=self.video) as video:
            self.assertEqual(self.execute(media='images'),0)
            video.assert_not_called()
            self.assertFalse(state.load()['entries']['1']['video_done'])
            self.assertEqual(self.execute(),0)
            self.assertEqual(self.execute(),0)
        image.assert_called_once()
        video.assert_called_once()
        entry=state.load()['entries']['1']
        self.assertTrue(entry['video_done'])
        self.assertEqual(len(entry['media']),2)

    def test_partial_image_success_survives_failure_and_can_deliver(self):
        sources=['https://pbs.twimg.com/media/a?format=jpg','https://pbs.twimg.com/media/b?format=jpg']
        self.item.update(images=sources,hasVideo=False)
        def image(item,source,out,report):
            if source==sources[1]: raise OSError('network lost')
            return self.image(item,source,out,report)
        with patch.object(media,'image',side_effect=image):
            self.assertEqual(self.execute(media='images'),1)
        entry=state.load()['entries']['1']
        self.assertEqual(entry['downloaded_images'],sources[:1])
        self.assertEqual(len(entry['files']),1)
        self.assertIn('alice',entry['failures'])
        report=Reporter()
        self.assertEqual(runner.deliveries(state.load(),'alice',report),(1,None))
        report.close()
        month=time.strftime('%Y-%m')
        self.assertEqual(len(list((self.root/'nas'/'交付'/'X download'/month).glob('*.png'))+list((self.root/'nas'/'交付'/'X download'/month).glob('*.jpg'))),1)
        self.assertTrue(next(iter(state.load()['entries']['1']['media'].values()))['delivered'])

    def test_partial_video_completion_is_saved_before_failure(self):
        def video(item,out,archive,quality,report,completed,repair=False,known_files=()):
            self.video(item,out,archive,quality,report,completed)
            self.assertEqual(len(state.load()['entries']['1']['files']),1)
            raise RuntimeError('second video failed')
        with patch.object(media,'video',side_effect=video):
            self.assertEqual(self.execute(),1)
        self.assertEqual(len(state.load()['entries']['1']['files']),1)
        self.assertFalse(state.load()['entries']['1']['video_done'])

    def test_archive_retry_after_last_file_saved_finishes(self):
        self.out.mkdir()
        path = self.out / 'test-1.mp4'
        path.write_bytes(b'video')
        entry = {**self.item, 'accounts':['alice'], 'video_done':False, 'attempts':1,
                 'status':'failed', 'files':[], 'media':{},
                 'failures':{'alice':{'error':'interrupted after final file'}}}
        runner.add_file(entry, str(path), 'video')
        value = {'schema_version':2, 'entries':{'1':entry}, 'accounts':{}}
        state.atomic(self.data/'state.json', value)
        original = jobs.popen
        def child(command, **kwargs):
            return original([sys.executable, '-c',
                'print("[download] has already been recorded in the archive")'], **kwargs)
        with patch.object(jobs, 'popen', side_effect=child):
            self.assertEqual(self.execute(), 0)
        saved = state.load()['entries']['1']
        self.assertTrue(saved['video_done'])
        self.assertEqual(saved['files'], [str(path)])
        self.assertNotIn('alice', saved['failures'])

    def test_archive_without_known_local_video_is_not_success(self):
        original = jobs.popen
        def child(command, **kwargs):
            return original([sys.executable, '-c',
                'print("[download] has already been recorded in the archive")'], **kwargs)
        with patch.object(jobs, 'popen', side_effect=child):
            self.assertEqual(self.execute(), 1)
        self.assertFalse(state.load()['entries']['1']['video_done'])

    def test_retry_only_current_account_outside_snapshot(self):
        entries={}
        for key,account in [('1','alice'),('2','bob')]:
            entries[key]={**self.item,'id':key,'accounts':[account],'media':{},'files':[],
                          'images':[],'status':'failed','attempts':1,'failures':{account:{'error':'failure'}}}
        state.atomic(self.data/'state.json',{'schema_version':2,'entries':entries,'accounts':{}})
        self.snapshot['items']=[]
        with patch.object(media,'video',side_effect=self.video) as video:
            self.assertEqual(self.execute(),0)
        video.assert_called_once()
        self.assertIn('bob',state.load()['entries']['2']['failures'])

    def test_dry_run_does_not_write_business_files(self):
        self.seed()
        before={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(media,'video') as video,patch.object(media,'image') as image:
            self.assertEqual(self.execute(dry_run=True),0)
        video.assert_not_called(); image.assert_not_called()
        after={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p.name!='lock'}
        self.assertEqual(after,before)
        self.assertFalse(self.out.exists())

    def test_preview_keeps_normal_snapshot_and_state(self):
        self.seed()
        normal=runner.snapshot_path('alice').read_bytes()
        before=(self.data/'state.json').read_bytes()
        with patch.object(runner,'scrape',return_value=self.snapshot),patch.object(media,'video') as video:
            self.assertEqual(runner.preview(self.config),0)
        video.assert_not_called()
        self.assertEqual((self.data/'state.json').read_bytes(),before)
        self.assertEqual(runner.snapshot_path('alice').read_bytes(),normal)
        self.assertTrue((self.data/'preview.json').exists())

    def test_preview_confirmation_rechecks_account_then_reuses_list(self):
        state.atomic(self.data/'preview.json',{**self.snapshot,'options':self.config})
        with patch.object(runner,'scrape',return_value={'status':'ok','account':'alice','items':[]}) as scrape,patch.object(media,'video',side_effect=self.video),patch.object(runner,'deliveries',return_value=(0,None)):
            self.assertEqual(runner.run({**self.config,'use_preview':True}),0)
        scrape.assert_called_once()
        self.assertEqual(len(state.load()['entries']),1)

    def test_account_change_discards_preview(self):
        state.atomic(self.data/'preview.json',{**self.snapshot,'options':self.config})
        bob={'status':'ok','account':'bob','items':[]}
        with patch.object(runner,'scrape',side_effect=[bob,bob]) as scrape:
            self.assertEqual(runner.run({**self.config,'use_preview':True},retry='download'),0)
        self.assertEqual(scrape.call_count,2)
        self.assertEqual(state.load()['entries'],{})

    def test_unknown_account_blocks_download(self):
        self.seed()
        state.atomic(runner.snapshot_path('alice'),{'status':'ok','items':[self.item]})
        self.assertEqual(runner.run({**self.config,'skip_scrape':True}),4)

    def test_delivery_missing_file_returns_failure(self):
        value=state.fresh()
        value['entries']['1']={'accounts':['alice'],'files':['/not-here.mp4'],'media':{'/not-here.mp4':{'delivered':True}},'delivered':'old'}
        report=Reporter()
        self.assertEqual(runner.deliveries(value,'alice',report),(0,'本地文件缺失'))
        report.close()

    def test_failed_delivery_can_retry_without_redownload(self):
        self.assertEqual(self.execute(media='images'),0)
        value=state.load()
        path=self.out/'photo.jpg';path.write_bytes(b'photo')
        entry=value['entries']['1'];runner.add_file(entry,str(path),'image')
        month=time.strftime('%Y-%m',time.localtime(path.stat().st_mtime))
        clash=self.root/'nas'/'交付'/'X download'/month/'photo.jpg'
        clash.parent.mkdir(parents=True);clash.write_bytes(b'different')
        report=Reporter()
        code,error=runner.deliveries(value,'alice',report)
        self.assertEqual(code,0);self.assertIn('内容不同',error)
        self.assertFalse(entry['media'][str(path)]['delivered'])
        clash.unlink()
        self.assertEqual(runner.deliveries(value,'alice',report),(1,None))
        self.assertEqual(clash.read_bytes(),b'photo')
        self.assertTrue(entry['media'][str(path)]['delivered'])
        self.assertEqual(runner.deliveries(value,'alice',report),(0,None))
        report.close()

    def test_local_mode_and_unmounted_nas_keep_files_undelivered(self):
        self.assertEqual(self.execute(media='images'),0)
        value=state.load()
        path=self.out/'photo.jpg';path.write_bytes(b'photo')
        entry=value['entries']['1'];runner.add_file(entry,str(path),'image')
        with patch.object(jobs,'call') as call:
            report=Reporter()
            self.assertEqual(runner.deliveries(value,'alice',report,'local'),(0,None))
            with patch.object(runner,'nas_mounted',return_value=False):
                self.assertEqual(runner.deliveries(value,'alice',report,'nas'),(0,None))
            report.close()
        call.assert_not_called()
        self.assertFalse(entry['media'][str(path)]['delivered'])

    def test_delivery_setting_validated(self):
        state.save_settings({**state.DEFAULTS,'delivery':'bogus'})
        with self.assertRaises(ValueError):
            state.settings()

    def test_new_downloads_get_increasing_bookmark_prefix_old_files_keep_names(self):
        items=[{**self.item,'id':'3','url':'https://x.com/test/status/3','hasVideo':False,'images':[]},
               {**self.item,'id':'2','url':'https://x.com/test/status/2','hasVideo':False,'images':[]}]
        value=state.fresh()
        self.out.mkdir(parents=True,exist_ok=True);old=self.out/'test-9.mp4';old.write_bytes(b'v')
        value['entries']['9']={'status':'downloaded','files':[str(old)],'accounts':['alice'],'media':{str(old):{'kind':'video','downloaded':True,'delivered':True}},'video_done':True,'images':[],'downloaded_images':[]}
        state.atomic(state.DATA/'state.json',value)
        self.snapshot['items']=[*items,{**self.item,'id':'9','url':'https://x.com/test/status/9'}]
        self.seed(self.snapshot,value)
        self.assertEqual(self.execute(media='images'),0)
        entries=state.load()['entries']
        self.assertEqual((entries['2']['seq'],entries['3']['seq']),(1,2))
        self.assertNotIn('seq',entries['9'])
        self.assertEqual(media.seq_prefix({'seq':7}),'000007_')
        target,_=media.image_target({**self.item,'seq':7},'https://pbs.twimg.com/media/abc?format=jpg&name=orig',Path('/o'))
        self.assertEqual(target.name,'000007_test-1-photo-abc.jpg')

    def test_scheduled_run_skips_until_interval_elapsed_and_records_success(self):
        from datetime import date,timedelta
        today=date.today()
        state.atomic(state.DATA/'schedule.json',{'time':'07:00','every_days':3,'last_success':(today-timedelta(days=2)).isoformat()})
        with patch.object(cli,'main',return_value=0) as run:
            self.assertEqual(cli.scheduled_run(),0)
        run.assert_not_called()
        state.atomic(state.DATA/'schedule.json',{'time':'07:00','every_days':3,'last_success':(today-timedelta(days=3)).isoformat()})
        with patch.object(cli,'main',return_value=1):
            self.assertEqual(cli.scheduled_run(),1)
        self.assertNotEqual(state.read(state.DATA/'schedule.json')['last_success'],today.isoformat())
        with patch.object(cli,'main',return_value=0):
            self.assertEqual(cli.scheduled_run(),0)
        self.assertEqual(state.read(state.DATA/'schedule.json')['last_success'],today.isoformat())
        with self.assertRaises(ValueError):jobs.schedule('enable','07:00',0)

    def test_bark_notifies_on_failed_run_only(self):
        self.seed()
        with patch.object(notify,'send',return_value=None) as send:
            with patch.object(runner,'_run',return_value=0):runner.run(self.config)
            with patch.object(runner,'_run',return_value=130):runner.run(self.config)
            send.assert_not_called()
        notify.save('K'*10)
        self.assertEqual(oct(notify.path().stat().st_mode & 0o777),'0o600')
        state.atomic(state.DATA/'current.json',{'account':'alice','counts':{'failed':2},'error':'login\nNAS gone','exit_code':1})
        with patch.object(notify,'send',return_value=None) as send:
            with patch.object(runner,'_run',return_value=1):runner.run(self.config)
            with patch.object(runner,'_run',return_value=1):runner.run({**self.config,'dry_run':True})
        send.assert_called_once()
        self.assertEqual(send.call_args.args,('X 收藏下载失败','@alice · 失败 2 条 · NAS gone · 退出码 1'))

    def test_bark_send_posts_key_in_body_and_never_raises(self):
        notify.save('SECRETKEY')
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self):return b'{"code":200}'
        with patch.object(notify,'urlopen',return_value=Response()) as urlopen:
            self.assertIsNone(notify.send('t','b'))
        request=urlopen.call_args.args[0]
        self.assertEqual(request.full_url,'https://api.day.app/push')
        self.assertNotIn('SECRETKEY',request.full_url)
        self.assertEqual(json.loads(request.data)['device_key'],'SECRETKEY')
        with patch.object(notify,'urlopen',side_effect=OSError('SECRETKEY')):
            error=notify.send('t','b')
        self.assertNotIn('SECRETKEY',error)
        notify.disable()
        self.assertEqual(notify.send('t','b'),'未配置 Bark')

    def test_stalled_nas_copy_is_abandoned_and_files_stay_pending(self):
        import threading
        self.assertEqual(self.execute(media='images'),0)
        value=state.load()
        entry=value['entries']['1']
        paths=[]
        for name in ('a.jpg','b.jpg'):
            path=self.out/name;path.write_bytes(name.encode());runner.add_file(entry,str(path),'image');paths.append(str(path))
        release=threading.Event()
        real=runner.copy_verified
        def copy(source,folder,tick=lambda:None):
            if source.name=='b.jpg':
                release.wait(5)
                tick()
            return real(source,folder,tick)
        report=Reporter()
        started=time.monotonic()
        with patch.object(runner,'NAS_STALL_SECONDS',.3),patch.object(runner,'copy_verified',copy):
            self.assertEqual(runner.deliveries(value,'alice',report),(1,None))
        self.assertLess(time.monotonic()-started,3)
        release.set();report.close()
        self.assertTrue(entry['media'][paths[0]]['delivered'])
        self.assertFalse(entry['media'][paths[1]]['delivered'])
        time.sleep(.2)
        month=time.strftime('%Y-%m')
        self.assertEqual(list((self.root/'nas'/'交付'/'X download'/month).glob('b.jpg*')),[])

    def test_streaming_video_progress_arrives_before_process_exit(self):
        self.out.mkdir()
        output=self.out/'movie.mp4';output.write_bytes(b'video')
        code='import time,json;print(\'XDL_PROGRESS:{"downloaded_bytes":10,"total_bytes":20,"speed":5,"eta":2}\',flush=True);time.sleep(.6);print("XDL_FILE:"+json.dumps('+repr(str(output))+'),flush=True)'
        events=[]
        class Report:
            def emit(_,phase,message,**values): events.append((time.monotonic(),phase,message,values))
        original=jobs.popen
        def child(command,**kwargs): return original([sys.executable,'-u','-c',code],**kwargs)
        start=time.monotonic()
        with patch.object(jobs,'popen',side_effect=child):
            result=media.video(self.item,self.out,self.data/'archive.txt','1080',Report())
        self.assertEqual(result,[str(output)])
        progress=[event for event in events if 'downloaded_bytes' in event[3]][0]
        self.assertLess(progress[0]-start,.5)
        self.assertGreater(time.monotonic()-progress[0],.4)
        self.assertEqual(jobs.CHILDREN,set())

    def test_preview_includes_failures_beyond_scrape_range(self):
        entry={**self.item,'accounts':['alice'],'status':'failed','failures':{'alice':{'error':'video failed'}},'files':[],'media':{}}
        value={'schema_version':2,'accounts':{},'entries':{'1':entry}}
        state.atomic(self.data/'state.json',value)
        empty={'status':'ok','account':'alice','items':[]}
        with patch.object(runner,'scrape',return_value=empty):
            self.assertEqual(runner.preview(self.config),0)
        preview=state.read(self.data/'preview.json')
        self.assertEqual([item['id'] for item in preview['items']],['1'])

    def test_image_only_retry_preserves_pending_video_failure(self):
        value={'schema_version':2,'accounts':{},'entries':{'1':{**self.item,'accounts':['alice'],'status':'failed',
            'video_done':False,'files':[],'media':{},'failures':{'alice':{'error':'video failed'}}}}}
        state.atomic(self.data/'state.json',value)
        self.snapshot['items']=[]
        self.assertEqual(self.execute(media='images'),0)
        self.assertIn('alice',state.load()['entries']['1']['failures'])
        with patch.object(media,'video',side_effect=self.video) as video:
            self.assertEqual(self.execute(),0)
        video.assert_called_once()
        self.assertNotIn('alice',state.load()['entries']['1']['failures'])

    def test_task_output_logs_are_rotated(self):
        directory=self.data/'task-output';directory.mkdir(parents=True)
        for i in range(35): (directory/f'{i:03}.log').write_text('old')
        path=jobs.task_output_path();path.write_text('new')
        self.assertEqual(len(list(directory.glob('*.log'))),30)

    def test_stage_change_clears_stale_download_speed(self):
        record = {'run_id':'progress-check', 'status':'running'}
        report = Reporter(record)
        report.emit('下载', 'first file', downloaded_bytes=20, total_bytes=40,
                    speed=5, file_key='first.mp4')
        self.assertEqual(state.read(self.data/'current.json')['speed'], 5)
        report.emit('合并/转码', 'merging video')
        saved = state.read(self.data/'current.json')
        self.assertNotIn('downloaded_bytes', saved)
        self.assertNotIn('speed', saved)
        report.close()

    def test_video_output_parser_separates_warning_progress_path(self):
        self.assertEqual(media.parse_video_line('WARNING: 50% /tmp/no.mp4')[0],'message')
        self.assertEqual(media.parse_video_line('XDL_PROGRESS:{"downloaded_bytes":2}')[0],'progress')
        self.assertEqual(media.parse_video_line('XDL_FILE:"/tmp/real.mp4"'),('file','/tmp/real.mp4'))

    def test_image_unknown_size_uses_bytes_and_speed_and_atomic_complete(self):
        self.out.mkdir()
        response=io.BytesIO(b'image data');response.headers=Message();response.headers['Content-Type']='image/jpeg'
        class Report:
            def emit(_,phase,message,**values):
                if 'downloaded_bytes' in values:
                    self.assertIsNone(values['total_bytes'])
                    self.assertNotIn('%',message)
        with patch.object(media,'urlopen',return_value=response):
            path=media.image(self.item,'https://pbs.twimg.com/media/p?format=jpg',self.out,Report())
        self.assertEqual(Path(path).read_bytes(),b'image data')
        self.assertFalse(list(self.out.glob('*.part')))

    def test_lock_prevents_concurrent_and_dry_runs(self):
        with jobs.locked():
            with self.assertRaises(jobs.BusyError):
                runner.run({**self.config,'dry_run':True})
            with self.assertRaises(jobs.BusyError):
                runner.preview(self.config)

    def test_stale_pid_is_not_signaled(self):
        state.atomic(self.data/'current.json',{'status':'running','pid':os.getpid(),'identity':'wrong','run_id':'old'})
        with patch.object(os,'kill') as kill:
            self.assertEqual(jobs.stop(),0)
        kill.assert_not_called()
        self.assertEqual(jobs.current()['status'],'interrupted')

    def test_safe_stop_real_isolated_process_and_child(self):
        code='''import os,time,signal
from xdownload import state,jobs
p=jobs.popen(["sleep","60"])
def stop(a,b):
 jobs.terminate_children()
 state.atomic(state.DATA/"current.json",{"status":"interrupted","run_id":"isolated","pid":os.getpid(),"identity":jobs.identity(os.getpid())})
 raise SystemExit(130)
signal.signal(signal.SIGTERM,stop)
state.atomic(state.DATA/"current.json",{"status":"running","run_id":"isolated","pid":os.getpid(),"identity":jobs.identity(os.getpid()),"own_session":True,"children":[{"pid":p.pid,"identity":jobs.identity(p.pid)}]})
time.sleep(60)
'''
        proc=subprocess.Popen([sys.executable,'-c',code],env={**os.environ,'XDL_HOME':str(self.root)},start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        deadline=time.monotonic()+5
        while not (self.data/'current.json').exists() and time.monotonic()<deadline: time.sleep(.02)
        self.assertTrue((self.data/'current.json').exists())
        child=state.read(self.data/'current.json')['children'][0]
        self.assertEqual(jobs.stop(),0)
        _,errors=proc.communicate(timeout=5)
        self.assertEqual(proc.returncode,130,errors)
        self.assertIsNone(jobs.identity(child['pid']))

    def test_non_tty_default_and_menu_never_read_stdin(self):
        with patch.object(sys.stdin,'isatty',return_value=False),patch('builtins.input',side_effect=AssertionError('read stdin')):
            self.assertEqual(cli.main([]),0)
            self.assertEqual(cli.main(['menu']),0)

    def test_config_precedence(self):
        state.save_settings({**self.config,'max':100,'media':'images'})
        parsed=cli.parser().parse_args(['run','--max','2'])
        config=cli.options(parsed)
        self.assertEqual(config['max'],2)
        self.assertEqual(config['media'],'images')

    def test_logs_and_rotation_do_not_delete_media(self):
        self.out.mkdir();(self.out/'movie.mp4').write_bytes(b'video')
        for i in range(33):
            folder=self.data/'runs'/f'{i:03}'
            state.atomic(folder/'result.json',{'run_id':str(i)})
            (folder/'run.log').write_text('log')
        state.trim_runs()
        self.assertEqual(len(list((self.data/'runs').glob('*/result.json'))),30)
        self.assertEqual((self.out/'movie.mp4').read_bytes(),b'video')

    def test_schedule_time_validated_before_any_launchctl(self):
        with patch.object(subprocess,'run') as run:
            with self.assertRaises(ValueError):jobs.schedule('enable','25:30')
        run.assert_not_called()

    def test_schedule_enable_generates_absolute_project_paths(self):
        python=self.root/'.venv/bin/python';python.parent.mkdir(parents=True);python.touch()
        path=self.root/'LaunchAgents'/('test.plist')
        with patch.object(jobs,'schedule_path',return_value=path),patch.object(jobs,'schedule_status',return_value={'loaded':False}),patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            self.assertEqual(jobs.schedule('enable','09:45'),0)
        import plistlib
        value=plistlib.loads(path.read_bytes())
        self.assertEqual(value['ProgramArguments'],[str(python),'-m','xdownload','run','--scheduled'])
        self.assertEqual(value['WorkingDirectory'],str(self.root))
        self.assertEqual(run.call_args.args[0][1],'bootstrap')


class KnownIdsTest(unittest.TestCase):
    def test_known_ids_only_fully_handled_per_account(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'a.jpg'; f.write_text('x')
            entries={
                '1':{'status':'downloaded','accounts':['me'],'images':['u'],'downloaded_images':['u'],'video_done':True,'files':[str(f)]},
                '2':{'status':'failed','accounts':['me'],'images':[],'video_done':False,'files':[]},
                '3':{'status':'downloaded','accounts':['me'],'images':[],'video_done':True,'files':[str(Path(d)/'gone.mp4')]},
                '4':{'status':'no_video','accounts':['me'],'images':[],'video_done':True,'files':[],'failures':{'me':{}}},
            }
            from xdownload import runner
            self.assertEqual(runner.known_ids({'entries':entries},'both'),{'me':['1']})


if __name__=='__main__':unittest.main()
