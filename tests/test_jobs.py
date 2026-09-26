import tempfile
import time
import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
import jobs


class JobTests(unittest.TestCase):
    def test_completed_failed_and_stale_states_survive_reopening(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            jobs.write_json(folder/'run.json',{'status':'running','pid':99999999,'result':str(folder/'result')})
            self.assertEqual(jobs.inspect_run(folder)['display_status'],'Interrupted / incomplete')
            result=folder/'result'; result.mkdir()
            (result/'Skeleton_full.xml').write_text('test')
            (result/'START HERE.txt').write_text('test')
            jobs.write_json(result/'manifest.json',{'warnings':['download missing']})
            self.assertEqual(jobs.inspect_run(folder)['display_status'],'Completed — review notes')
            jobs.write_json(folder/'run.json',{'status':'failed','result':str(result)})
            self.assertEqual(jobs.inspect_run(folder)['display_status'],'Failed — see log')

    def test_completed_state_recognized_with_the_current_file_layout(self):
        # START HERE.txt lives at the project root and manifest.json under Timeline/Data/
        # (not inside Timeline/ itself, unlike the older layout the test above covers).
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            result=folder/'Timeline'; result.mkdir()
            (result/'Skeleton_full.xml').write_text('test')
            (folder/'START HERE.txt').write_text('test')
            jobs.write_json(result/'Data'/'manifest.json',{'warnings':[]})
            jobs.write_json(folder/'run.json',{'status':'completed','result':str(result)})
            self.assertEqual(jobs.inspect_run(folder)['display_status'],'Completed')

    def test_worker_survives_launcher_exit_and_saves_failure_log(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); (root/'.cache').mkdir()
            (root/'skeleton_builder.py').write_text('import time\ntime.sleep(.3)\nprint("visible failure", flush=True)\nraise SystemExit(7)\n')
            run=root/'run'; run.mkdir()
            (root/'a.docx').write_text('doc'); (root/'c.mp3').write_text('audio')
            jobs.write_json(run/'run.json',{'status':'starting','result':str(run/'Timeline'),'config':{'docx':str(root/'a.docx'),'audio':str(root/'c.mp3'),'videos':False}})
            code=f'import job_worker; from pathlib import Path; job_worker.ROOT=Path({str(root)!r}); job_worker.run({str(run)!r})'
            # A short-lived launcher spawns the independent supervisor then exits.
            launch=f'import subprocess,sys; f=open({str(run/"run.log")!r},"w"); subprocess.Popen([sys.executable,"-c",{code!r}],stdin=subprocess.DEVNULL,stdout=f,stderr=f,start_new_session=True)'
            subprocess.run([sys.executable,'-c',launch],check=True)
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                state=jobs.read_json(run/'run.json')
                if state.get('status')=='failed': break
                time.sleep(.1)
            self.assertEqual(state.get('exit_code'),7)
            self.assertIn('visible failure',(run/'run.log').read_text())

    def test_reaps_orphaned_builder_when_job_worker_pid_is_dead(self):
        # Simulates the exact failure this guards against - and does it with real processes,
        # not a fabricated pid, because the fix is platform-sensitive in a way that's easy to
        # get subtly wrong: on POSIX, the builder subprocess never gets its own process group
        # (job_worker.py spawns it with a plain subprocess.Popen, no start_new_session), so it
        # inherits job_worker's original group - which remains a valid killpg target by that
        # original pid even after job_worker itself has exited, for as long as the builder is
        # still in it. A "job_worker" launcher is spawned as its own session/group leader,
        # itself spawns a "builder" child that outlives it, then exits - leaving a real orphan
        # in a real, still-valid process group, exactly like the production crash scenario.
        with tempfile.TemporaryDirectory() as t:
            folder = Path(t)
            launcher_code = ("import subprocess, sys; "
                              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                              "print(p.pid, flush=True)")
            launcher = subprocess.Popen([sys.executable, '-c', launcher_code],
                                        stdout=subprocess.PIPE, text=True, start_new_session=True)
            builder_pid = int(launcher.stdout.readline().strip())
            job_worker_pid = launcher.pid
            launcher.wait(timeout=5)  # the launcher exits almost immediately; the builder outlives it
            launcher.stdout.close()
            try:
                self.assertFalse(jobs.alive(job_worker_pid))
                self.assertTrue(jobs.alive(builder_pid))
                jobs.write_json(folder/'run.json', {'status': 'running', 'pid': job_worker_pid,
                                                     'builder_pid': builder_pid, 'result': str(folder/'result')})
                info = jobs.inspect_run(folder)
                self.assertEqual(info['display_status'], 'Failed — see log')
                deadline = time.monotonic()+5
                while jobs.alive(builder_pid) and time.monotonic() < deadline:
                    time.sleep(.1)
                self.assertFalse(jobs.alive(builder_pid), 'orphaned builder process was not killed')
            finally:
                jobs._kill_orphaned_builder({'pid': job_worker_pid, 'builder_pid': builder_pid})

    def test_run_folders_are_named_from_the_title_with_a_common_layout(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            (root/'My_ Script_').mkdir()
            folder=jobs.unique_folder(root,'My: Script?')
            self.assertEqual(folder.name,'My_ Script_ (2)')
            layout=jobs.run_layout(folder)
            self.assertEqual(sorted(layout),['audio','media','script','timeline'])
            self.assertEqual(layout['media'],folder/'Media')
            self.assertEqual(jobs.run_layout(folder,root/'shared')['media'],root/'shared'/folder.name)

    def test_active_job_blocks_duplicate_launch(self):
        with patch('jobs.runs',return_value=[{'display_status':'Running'}]):
            with self.assertRaisesRegex(ValueError,'already working'):
                jobs.start_job({})

    def test_queue_is_first_in_first_out_and_supports_removal(self):
        with tempfile.TemporaryDirectory() as t:
            with patch('jobs.QUEUE',Path(t)/'queue.json'):
                self.assertEqual(jobs.read_queue(),[])
                self.assertIsNone(jobs.dequeue_next())
                first=jobs.enqueue({'title':'First'})
                jobs.enqueue({'title':'Second'})
                third=jobs.enqueue({'title':'Third'})
                self.assertEqual([i['title'] for i in jobs.read_queue()],['First','Second','Third'])
                jobs.remove_from_queue(third)
                self.assertEqual([i['title'] for i in jobs.read_queue()],['First','Second'])
                popped=jobs.dequeue_next()
                self.assertEqual(popped['id'],first)
                self.assertEqual(popped['config']['title'],'First')
                self.assertEqual([i['title'] for i in jobs.read_queue()],['Second'])

    def test_moving_a_location_merges_files_and_handles_collisions(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); old,new=root/'old',root/'new'
            (old/'RunA').mkdir(parents=True); (old/'RunA'/'run.json').write_text('{}')
            (old/'RunB.txt').write_text('hello')
            new.mkdir(); (new/'RunC').mkdir()
            self.assertEqual(jobs.transfer_folder_contents(old,new),[])
            self.assertEqual(sorted(p.name for p in new.iterdir()),['RunA','RunB.txt','RunC'])
            self.assertFalse(old.exists())
            self.assertTrue((new/'RunA'/'run.json').exists())
            # same directory: a harmless no-op
            self.assertEqual(jobs.transfer_folder_contents(new,new),[])
