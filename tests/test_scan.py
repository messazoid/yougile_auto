import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import random
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from email import policy
from email.parser import BytesParser

from src import scan as app


def make_wav(path, seconds=26.8, rate=8000):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b'\x01\x00' * round(seconds * rate))


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'source.wav'
        make_wav(self.audio)
        self.db = sqlite3.connect(self.root / 'scan.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        with self.audio.open('rb') as source:
            self.info = app.inspect_audio(source)
        self.config = {'audio': self.info, 'host': app.DEFAULT_HOST}
        app.initialize_db(self.db, self.config)
        self.sent = []

    def transport(self, host, sample, key, secret):
        self.assertTrue(sample.startswith(b'fixture-fingerprint:'))
        self.sent.append(float(sample.split(b':')[1]))
        return 200, b'{"status":{"code":1001}}'

    def fingerprint(self, sample):
        with wave.open(io.BytesIO(sample), 'rb') as w:
            seconds = w.getnframes() / w.getframerate()
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
        return f'fixture-fingerprint:{seconds}'.encode()

    def run_scan(self, transport=None, retry=False, budget=600, sleeper=lambda value: None,
                 max_retries=0):
        with wave.open(str(self.audio), 'rb') as reader, contextlib.redirect_stdout(io.StringIO()):
            return app.scan(self.db, reader, self.config, 'fixture-key', 'fixture-secret',
                            budget, retry, transport=transport or self.transport,
                            clock=lambda: 0.0, sleeper=sleeper, fingerprinter=self.fingerprint,
                            max_retries=max_retries, retry_base_seconds=0,
                            retry_max_seconds=0)

    def test_production_schedule_and_last_window(self):
        frames = round(5626.815986 * 44100)
        schedule = list(app.windows(frames, 44100))
        self.assertEqual(len(schedule), 563)
        self.assertEqual(schedule[0], (0, 0, 529200))
        self.assertEqual(schedule[-1][1] / 44100, 5620)
        self.assertAlmostEqual(schedule[-1][2] / 44100, 6.815986, places=5)

    def test_covers_full_file_preserves_tail_no_repeat_on_resume(self):
        self.assertEqual(self.run_scan(), 3)
        self.assertEqual(self.sent, [12, 12, 6.8])
        self.assertEqual(self.run_scan(), 3)
        self.assertEqual(len(self.sent), 3)

    def test_rate_limit_waits_between_requests(self):
        waits = []
        self.run_scan(sleeper=waits.append)
        self.assertEqual(waits, [1.05, 1.05])

    def test_unlimited_mode_keeps_rate_limit_and_processes_all_windows(self):
        waits = []
        self.run_scan(budget=None, sleeper=waits.append)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(waits, [1.05, 1.05])

    def test_signature_multipart_and_sample_length(self):
        import base64, hmac, hashlib
        sample = b'RIFF\x00\xfftest'
        body, content_type = app.request_body(sample, 'key', 'secret', 1700000000)
        msg = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
        fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                  for part in msg.iter_parts()}
        self.assertEqual(fields['sample'], sample)
        self.assertEqual(fields['sample_bytes'], str(len(sample)).encode())
        self.assertEqual(fields['data_type'], b'fingerprint')
        expected = base64.b64encode(hmac.new(b'secret', b'POST\n/v1/identify\nkey\nfingerprint\n1\n1700000000', hashlib.sha1).digest())
        self.assertEqual(fields['signature'], expected)

    def test_transport_uses_verified_https_without_redirect_following(self):
        from unittest.mock import MagicMock
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 302
        response.read.return_value = b'redirect'
        with patch.object(app.http.client, 'HTTPSConnection', return_value=conn) as create:
            self.assertEqual(app.send_sample(app.DEFAULT_HOST, b'wav', 'key', 'secret'), (302, b'redirect'))
        self.assertTrue(create.call_args.kwargs['context'].check_hostname)
        conn.request.assert_called_once()
        self.assertEqual(conn.request.call_args.args[:2], ('POST', '/v1/identify'))
        conn.close.assert_called_once()

    def test_success_keeps_all_candidates_and_works(self):
        music = [{'title': 'fixture', 'acrid': 'one', 'score': 100,
                  'artists': [{'name': 'artist'}], 'album': {'name': 'album'},
                  'label': 'label',
                  'external_ids': {'isrc': 'XX0000000001'},
                  'external_metadata': {'spotify': {'track': {'id': 'spotify-id'}}},
                  'works': [{'iswc': 'T0000000001'}]}, {'title': 'alternate'}]
        raw = json.dumps({'status': {'code': 0}, 'metadata': {'music': music}}).encode()
        self.run_scan(lambda *args: (200, raw))
        summary = app.export_results(self.db, self.root, 3)
        self.assertTrue(summary['complete'])
        self.assertEqual(summary['candidate_rows'], 6)
        records = [json.loads(line) for line in (self.root / 'matches.jsonl').read_text().splitlines()]
        self.assertEqual(records[0]['music'], music[0])
        self.assertEqual(records[0]['verification'], 'not_reviewed')
        self.assertEqual(records[0]['title'], 'fixture')
        self.assertEqual(records[0]['artists'], ['artist'])
        self.assertEqual(records[0]['album'], 'album')
        self.assertEqual(records[0]['label'], 'label')
        self.assertEqual(records[0]['isrc'], 'XX0000000001')
        self.assertEqual(records[0]['score'], 100)
        self.assertEqual(records[0]['external_ids']['isrc'], 'XX0000000001')
        self.assertEqual(
            records[0]['external_metadata']['spotify']['track']['id'], 'spotify-id'
        )
        self.assertEqual(self.db.execute('SELECT response_raw FROM attempts LIMIT 1').fetchone()[0], raw)

    def test_quota_error_stops_without_auto_retry(self):
        with self.assertRaises(app.ScanStop):
            self.run_scan(lambda *args: (200, b'{"status":{"code":3003}}'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
        with self.assertRaises(app.ScanStop):
            self.run_scan()
        self.assertEqual(self.sent, [])
        self.run_scan(retry=True)
        self.assertEqual(len(self.sent), 3)

    def test_retryable_http_and_acr_responses_are_bounded_and_then_resume(self):
        fixtures = [
            (429, b'{"status":{"code":1001}}'),
            (503, b'{"status":{"code":1001}}'),
            (200, b'{"status":{"code":3003}}'),
        ]
        for first_response in fixtures:
            with self.subTest(first_response=first_response):
                self.db.execute("DELETE FROM attempts")
                self.db.execute("DELETE FROM local_windows")
                self.db.commit()
                calls = []

                def transient(*args):
                    calls.append(1)
                    if len(calls) == 1:
                        return first_response
                    return 200, b'{"status":{"code":1001}}'

                self.assertEqual(self.run_scan(transient, max_retries=2), 3)
                self.assertEqual(len(calls), 4)
                rows = self.db.execute(
                    "SELECT state,retryable,retry_number FROM attempts "
                    "WHERE window_index=0 ORDER BY id"
                ).fetchall()
                self.assertEqual([row['state'] for row in rows], ['error', 'done'])
                self.assertEqual(rows[0]['retryable'], 1)
                self.assertEqual([row['retry_number'] for row in rows], [0, 1])

    def test_network_errors_retry_at_most_configured_times(self):
        calls = []

        def unavailable(*args):
            calls.append(1)
            raise TimeoutError('secret fixture text')

        with self.assertRaises(app.ScanStop):
            self.run_scan(unavailable, max_retries=2)
        self.assertEqual(len(calls), 3)
        rows = self.db.execute("SELECT state,retryable FROM attempts ORDER BY id").fetchall()
        self.assertEqual([row['state'] for row in rows], ['uncertain'] * 3)
        self.assertTrue(all(row['retryable'] for row in rows))

    def test_authorization_error_is_not_retried(self):
        calls = []

        def unauthorized(*args):
            calls.append(1)
            return 200, b'{"status":{"code":3001}}'

        with self.assertRaises(app.ScanStop):
            self.run_scan(unauthorized, max_retries=2)
        self.assertEqual(len(calls), 1)
        row = self.db.execute("SELECT state,retryable,acr_code FROM attempts").fetchone()
        self.assertEqual((row['state'], row['retryable'], row['acr_code']), ('error', 0, 3001))

    def test_restart_resumes_a_durable_sending_window_within_retry_limit(self):
        self.db.execute(
            "INSERT INTO attempts "
            "(window_index,start_seconds,duration_seconds,started_utc,state,"
            "fingerprint_bytes,fingerprint_sha256) VALUES (0,0,12,'fixture','sending',4,'sha')"
        )
        self.db.commit()
        self.assertEqual(self.run_scan(max_retries=2), 3)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM attempts WHERE window_index=0").fetchone()[0], 2
        )

    def test_unknown_result_is_durable_and_explicit_retry_required(self):
        def broken(*args):
            raise TimeoutError('fixture-secret')
        with self.assertRaises(app.ScanStop) as err:
            self.run_scan(broken)
        self.assertNotIn('fixture-secret', str(err.exception))
        self.assertEqual(self.db.execute('SELECT state FROM attempts').fetchone()[0], 'uncertain')
        with self.assertRaises(app.ScanStop):
            self.run_scan()
        self.run_scan(retry=True)
        self.assertEqual(len(self.sent), 3)

    def test_crash_after_sending_marker_does_not_silently_retry(self):
        self.db.execute("INSERT INTO attempts (window_index,start_seconds,duration_seconds,started_utc,state,fingerprint_bytes,fingerprint_sha256) VALUES (0,0,12,'fixture','sending',4,'fixture-sha')")
        self.db.commit()
        with self.assertRaises(app.ScanStop):
            self.run_scan()
        self.assertEqual(self.sent, [])

    def test_budget_persists_across_invocations(self):
        with self.assertRaises(app.ScanStop):
            self.run_scan(budget=1)
        self.assertEqual(len(self.sent), 1)
        with self.assertRaises(app.ScanStop):
            self.run_scan(budget=1)
        self.assertEqual(len(self.sent), 1)
        self.run_scan(budget=3)
        self.assertEqual(len(self.sent), 3)

    def test_resume_mismatch_refused(self):
        with self.assertRaises(app.ScanStop):
            app.initialize_db(self.db, {'other': 'source'})

    def test_http_or_malformed_errors_are_not_no_match(self):
        for http, raw in [(429, b'{"status":{"code":1001}}'),
                          (503, b'bad gateway'), (200, b'[]'),
                          (200, b'{"status":{"code":false}}'),
                          (200, b'{"status":{"code":"1001"}}')]:
            with self.subTest(http=http, raw=raw):
                self.assertFalse(app.classify(http, raw)[0])
        self.assertTrue(app.classify(200, b'{"status":{"code":1001}}')[0])

    def test_interrupt_retains_attempt(self):
        def interrupted(*args):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_scan(interrupted)
        self.assertEqual(self.db.execute('SELECT state FROM attempts').fetchone()[0], 'uncertain')

    def test_truncated_wav_is_rejected_preflight(self):
        with self.audio.open('r+b') as source:
            source.truncate(self.audio.stat().st_size - 20)
        with self.audio.open('rb') as source, self.assertRaises(app.ScanStop):
            app.inspect_audio(source)

    def test_dry_run_never_contacts_network_or_prompts(self):
        out = self.root / 'dryrun-output'
        with patch.object(app, 'send_sample', side_effect=AssertionError('network')), \
                patch.object(app, 'load_sdk', return_value=(None, {'package':'fixture', 'version':'1.0.12'})), \
                patch.object(app, 'prepare_window', return_value=(b'wav', b'fp', None)), \
                patch.object(app, 'hidden_credential', side_effect=AssertionError('prompt')), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(app.main(['--audio', str(self.audio), '--output', str(out)]), 0)
        self.assertFalse(out.exists())
        self.assertIn('windows: 3', stdout.getvalue())

    def test_completed_scan_execute_needs_no_credentials(self):
        out = self.root / 'completed'
        # Reuse main's actual configuration without permitting any network.
        original_scan = app.scan
        def injected(*args):
            return original_scan(*args, transport=self.transport, sleeper=lambda _: None, fingerprinter=self.fingerprint)
        with patch.object(app, 'scan', side_effect=injected), \
                patch.object(app, 'load_sdk', return_value=(None, {'package':'fixture', 'version':'1.0.12'})), \
                patch.object(app, 'hidden_credential', return_value='fixture'), \
                contextlib.redirect_stdout(io.StringIO()):
            app.main(['--audio', str(self.audio), '--output', str(out), '--execute', '--max-requests', '3'])
        with patch.object(app, 'hidden_credential', side_effect=AssertionError('prompt')), \
                patch.object(app, 'load_sdk', return_value=(None, {'package':'fixture', 'version':'1.0.12'})), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--audio', str(self.audio), '--output', str(out), '--execute']), 0)
        self.assertEqual(len(self.sent), 3)

    def test_execute_uses_environment_credentials_without_prompt(self):
        out = self.root / 'environment-credentials'
        received = []

        def injected(db, reader, config, key, secret, *args, **kwargs):
            received.append((key, secret))
            return 0

        with patch.dict(app.os.environ, {'ACR_ACCESS_KEY': 'fixture-key',
                                         'ACR_SECRET_KEY': 'fixture-secret'}, clear=False), \
                patch.object(app, 'scan', side_effect=injected), \
                patch.object(app, 'load_sdk', return_value=(None, {'package':'fixture', 'version':'1.0.12'})), \
                patch.object(app, 'hidden_credential', side_effect=AssertionError('prompt')), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(app.main(['--audio', str(self.audio), '--output', str(out), '--execute']), 0)
        self.assertEqual(received, [('fixture-key', 'fixture-secret')])
        self.assertIn('credentials from environment', stdout.getvalue())

    def test_partial_environment_credentials_are_rejected(self):
        with patch.dict(app.os.environ, {'ACR_ACCESS_KEY': 'fixture-key'}, clear=True):
            with self.assertRaisesRegex(app.ScanStop, 'must be set together'):
                app.credentials_from_environment()

    def test_credential_file_is_used_without_prompt(self):
        out = self.root / 'file-credentials'
        credentials = self.root / 'acr.env'
        credentials.write_text('ACR_ACCESS_KEY=fixture-key\nACR_SECRET_KEY=fixture-secret\n')
        received = []

        def injected(db, reader, config, key, secret, *args, **kwargs):
            received.append((key, secret))
            return 0

        with patch.object(app, 'scan', side_effect=injected), \
                patch.object(app, 'load_sdk', return_value=(None, {'package':'fixture', 'version':'1.0.12'})), \
                patch.object(app, 'hidden_credential', side_effect=AssertionError('prompt')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--audio', str(self.audio), '--output', str(out),
                                       '--execute', '--env-file', str(credentials)]), 0)
        self.assertEqual(received, [('fixture-key', 'fixture-secret')])

    def test_empty_fingerprints_are_local_not_api_no_match(self):
        with wave.open(str(self.audio), 'rb') as reader, contextlib.redirect_stdout(io.StringIO()):
            processed=app.scan(self.db,reader,self.config,'key','secret',1,False,
                               fingerprinter=lambda _:b'',transport=lambda *a:self.fail('network'))
        self.assertEqual(processed,3)
        summary=app.export_results(self.db,self.root,3)
        self.assertEqual((summary['completed_windows'],summary['attempts'],summary['no_match_windows']),(0,0,0))
        self.assertEqual(summary['locally_skipped_windows'],3)
        self.assertTrue(summary['complete'])
        self.assertFalse(summary['all_windows_submitted'])
        self.assertEqual(len((self.root/'local_windows.jsonl').read_text().splitlines()),3)
        with wave.open(str(self.audio),'rb') as reader:
            app.scan(self.db,reader,self.config,'key','secret',1,False,
                     fingerprinter=lambda _:self.fail('already processed'),transport=lambda *a:self.fail('network'))

    def test_very_short_tail_is_recorded_without_sdk_or_http(self):
        make_wav(self.audio,seconds=20.1)
        with self.audio.open('rb') as f:self.config['audio']=app.inspect_audio(f)
        self.run_scan()
        self.assertEqual(len(self.sent),2)
        row=self.db.execute('SELECT * FROM local_windows').fetchone()
        self.assertEqual(row['window_index'],2)
        self.assertEqual(row['reason'],'tail_under_1_second')
        self.assertAlmostEqual(row['duration_seconds'],0.1)

    def test_sdk_failure_stops_before_attempt(self):
        for value in [None,'not bytes',bytearray(b'bad')]:
            with self.subTest(value=value),wave.open(str(self.audio),'rb') as reader,self.assertRaises(app.ScanStop):
                app.scan(self.db,reader,self.config,'key','secret',1,False,
                         fingerprinter=lambda _:value,transport=lambda *a:self.fail('network'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM local_windows').fetchone()[0],0)

    def test_fingerprint_hash_and_size_saved_per_request(self):
        self.run_scan()
        row=self.db.execute('SELECT * FROM attempts ORDER BY id LIMIT 1').fetchone()
        fp=b'fixture-fingerprint:12.0'
        self.assertEqual(row['fingerprint_bytes'],len(fp))
        self.assertEqual(row['fingerprint_sha256'],hashlib.sha256(fp).hexdigest())

    def test_check_all_is_local_only(self):
        out=self.root/'all-local'
        with patch.object(app,'load_sdk',return_value=(None,{'package':'fixture','version':'1.0.12'})), \
                patch.object(app,'prepare_window',return_value=(b'wav',b'fp',None)) as fp, \
                patch.object(app,'hidden_credential',side_effect=AssertionError('credential')), \
                patch.object(app.http.client,'HTTPSConnection',side_effect=AssertionError('network')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--audio',str(self.audio),'--output',str(out),'--check-all']),0)
        self.assertEqual(fp.call_count,3)
        self.assertFalse(out.exists())

    def test_cannot_combine_check_all_and_execute(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as err:
            app.main(['--audio',str(self.audio),'--check-all','--execute'])
        self.assertEqual(err.exception.code,2)

    def test_old_audio_database_rejected_without_changes(self):
        out=self.root/'old-audio'
        out.mkdir()
        dbpath=out/'scan.sqlite3'
        with sqlite3.connect(dbpath) as db:
            db.execute('CREATE TABLE metadata (id INTEGER PRIMARY KEY,config TEXT NOT NULL)')
            db.execute('INSERT INTO metadata VALUES (1,?)',(json.dumps({'version':1,'data_type':'audio'}),))
        before=dbpath.read_bytes()
        with patch.object(app,'load_sdk',return_value=(None,{'package':'fixture','version':'1.0.12'})), \
                patch.object(app,'hidden_credential',side_effect=AssertionError('credential')), \
                self.assertRaises(app.ScanStop):
            app.main(['--audio',str(self.audio),'--output',str(out),'--execute'])
        self.assertEqual(dbpath.read_bytes(),before)
        self.assertEqual([p.name for p in out.iterdir()],['scan.sqlite3'])

    def test_default_execution_budget_is_one(self):
        out=self.root/'budget-default'
        original_scan=app.scan
        def injected(*args):return original_scan(*args,transport=self.transport,
                                                 fingerprinter=self.fingerprint,sleeper=lambda _:None)
        with patch.object(app,'scan',side_effect=injected), \
                patch.object(app,'load_sdk',return_value=(None,{'package':'fixture','version':'1.0.12'})), \
                patch.object(app,'hidden_credential',return_value='fixture'), \
                contextlib.redirect_stdout(io.StringIO()),self.assertRaises(app.ScanStop):
            app.main(['--audio',str(self.audio),'--output',str(out),'--execute'])
        self.assertEqual(len(self.sent),1)
        self.assertEqual(json.loads((out/'summary.json').read_text())['attempts'],1)

    def test_zero_execution_budget_is_unlimited(self):
        out=self.root/'budget-unlimited'
        seen=[]
        def injected(*args):
            seen.append(args[5])
            return 0
        with patch.object(app,'scan',side_effect=injected), \
                patch.object(app,'load_sdk',return_value=(None,{'package':'fixture','version':'1.0.12'})), \
                patch.object(app,'hidden_credential',return_value='fixture'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--audio',str(self.audio),'--output',str(out),
                                       '--execute','--max-requests','0']),0)
        self.assertEqual(seen,[None])

    def test_native_errors_do_not_echo_exception_text(self):
        from unittest.mock import MagicMock
        sdk=MagicMock()
        sdk.create_fingerprint_by_filebuffer.side_effect=RuntimeError('sensitive-fixture')
        with patch.object(app,'load_sdk',return_value=(sdk,{})),self.assertRaises(app.ScanStop) as err:
            app.fingerprint_sample(b'fixture')
        self.assertNotIn('sensitive-fixture',str(err.exception))


@unittest.skipUnless(importlib.util.find_spec('acrcloud') is not None,'SDK not installed in this interpreter')
class NativeSDKTests(unittest.TestCase):
    def make_sample(self,seconds,noise):
        buf=io.BytesIO()
        with wave.open(buf,'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(44100)
            size=round(seconds*44100)*2
            w.writeframes(random.Random(7).randbytes(size) if noise else bytes(size))
        return buf.getvalue()

    def test_native_sdk_accepts_partial_last_wav(self):
        fp=app.fingerprint_sample(self.make_sample(6.815986,True))
        self.assertIsInstance(fp,bytes)
        self.assertGreater(len(fp),0)

    def test_native_silence_returns_empty_not_http(self):
        with patch.object(app.http.client,'HTTPSConnection',side_effect=AssertionError('network')):
            self.assertEqual(app.fingerprint_sample(self.make_sample(12,False)),b'')


if __name__ == '__main__':
    unittest.main(verbosity=2)
