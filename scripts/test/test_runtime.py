"""Exercise experiment worker reuse and shutdown without paid models or games."""
from contextlib import redirect_stdout
import copy
from io import StringIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runner.artifacts import read_json, write_json
from rsi.tools.llm import ExperimentRuntime


ROOT = Path(__file__).resolve().parents[2]

SDK_WORKER = r'''
import json
import os
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['OPENHANDS_SUPPRESS_BANNER'] = '1'
from unittest.mock import patch
from openhands.sdk.llm import Message, MessageToolCall, TextContent
from openhands.sdk.testing import TestLLM
import rsi.tools.llm as worker

def scripted_llm(job):
    marker = job['prompt']
    calls = [
        ('file_editor', dict(command='create', path='bot/components/tasks/created.py', file_text=marker)),
        ('terminal', dict(command='python -B -c "from pathlib import Path; Path(\'terminal.txt\').write_text(Path(\'marker.txt\').read_text())"', timeout=15)),
        ('finish', dict(message=marker)),
    ]
    messages = [Message(role='assistant', content=[TextContent(text='Offline fixture')], tool_calls=[
        MessageToolCall(id=str(i), name=name, arguments=json.dumps(arguments), origin='completion')])
        for i, (name, arguments) in enumerate(calls)]
    return TestLLM.from_messages(messages)

worker._create_llm = scripted_llm
with patch('openhands.sdk.agent.base.has_vision_profile_available', return_value=False):
    worker.serve()
'''

FILE_WORKER = '''
import json
import sys
import time
from pathlib import Path, PurePosixPath
root = Path(sys.argv[1])
for line in sys.stdin:
    value = json.loads(line)
    path = root / 'sessions' / PurePosixPath(value).relative_to('/sessions') if value.startswith('/sessions/') else Path(value)
    job = json.loads(path.read_text(encoding='utf-8'))
    (path.parent / 'usage.json').write_text(json.dumps(dict(tokens=7, cost=None)), encoding='utf-8')
    if job.get('stall'):
        time.sleep(30)
    (path.parent / 'response.json').write_text(json.dumps(dict(status='ok', value='done', usage=dict(tokens=7, cost=None))), encoding='utf-8')
    (path.parent / 'done').touch()
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = ExperimentRuntime(ROOT, self.root / 'runtime')
        self.addCleanup(self.runtime.close)
        self.popen = subprocess.Popen

    def start_file_worker(self, args, **kwargs):
        return self.popen([sys.executable, '-u', '-B', '-c', FILE_WORKER, str(self.runtime.root)], **kwargs)

    def test_real_sdk_worker_reuses_pid_and_rebinds_candidate_tools(self):
        """Use two independent SDK conversations, terminals and file editors in one process."""
        def start(args, **kwargs):
            return self.popen([sys.executable, '-u', '-B', '-c', SDK_WORKER], **kwargs)

        settings = read_json(ROOT / 'rsi/config.json')['model']
        directories = []
        with patch('rsi.tools.llm.subprocess.Popen', side_effect=start) as start_worker, \
                redirect_stdout(StringIO()):
            for marker in ('FIRST_CANDIDATE', 'SECOND_CANDIDATE'):
                candidate = self.runtime.root / 'work' / marker / 'code'
                (candidate / 'bot/components/tasks').mkdir(parents=True)
                (candidate / 'marker.txt').write_text(marker, encoding='utf-8')
                directory = self.runtime.root / 'sessions' / marker
                job = dict(role='editor', workspace=str(candidate), prompt=marker,
                           model=dict(settings, wall_timeout=120))
                result = self.runtime.call_session(job, directory)
                self.assertEqual(result['status'], 'ok', result.get('reason'))
                self.assertEqual(result['value'], marker)
                self.assertEqual((candidate / 'bot/components/tasks/created.py').read_text(encoding='utf-8'), marker)
                self.assertEqual((candidate / 'terminal.txt').read_text(encoding='utf-8'), marker)
                self.assertIn('Command result: exit_code=0', (directory / 'stdout.txt').read_text(encoding='utf-8'))
                self.assertIsNone(self.runtime.workers['local'].poll())
                directories.append(directory)
            self.assertEqual(start_worker.call_count, 1)
            self.assertEqual(read_json(directories[0] / 'worker.json'), read_json(directories[1] / 'worker.json'))
            histories = [next((directory / 'conversation').iterdir()) for directory in directories]
            self.assertNotEqual(histories[0].name, histories[1].name)
            second_history = ''.join(path.read_text(encoding='utf-8') for path in histories[1].rglob('*.json'))
            self.assertNotIn('FIRST_CANDIDATE', second_history)
            process = self.runtime.workers['local']
            self.runtime.close()
            self.assertEqual(process.poll(), 0)
            self.runtime.close()
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                self.runtime.call_session(job, self.root / 'after_close')

    def test_timeout_terminates_worker_and_preserves_partial_usage(self):
        directory = self.runtime.root / 'sessions/timeout'
        def stop(process):
            process.kill()
            process.wait(timeout=5)

        with patch('rsi.tools.llm.subprocess.Popen', side_effect=self.start_file_worker), \
                patch('rsi.tools.llm.stop_process', side_effect=stop) as terminate:
            result = self.runtime.call_session(dict(stall=True, model=dict(wall_timeout=2)), directory)
        terminate.assert_called_once_with(self.runtime.workers['local'])
        self.assertEqual(result['reason'], 'model_session_wall_timeout')
        self.assertEqual(result['usage'], dict(tokens=7, cost=None))
        self.assertIsNotNone(self.runtime.workers['local'].poll())
        self.assertEqual(read_json(directory / 'response.json'), result)
        with patch('rsi.tools.llm.subprocess.Popen') as start:
            result = self.runtime.call_session(dict(model=dict(wall_timeout=2)), self.runtime.root / 'sessions/next')
        self.assertEqual(result['status'], 'error')
        start.assert_not_called()

    def test_docker_worker_reuses_container_and_mounts_across_candidates(self):
        refs = self.root / 'refs'
        refs.mkdir()
        evidence = self.root / 'investigation.json'
        write_json(evidence, dict(queries=[]))
        original = dict(role='editor', model=dict(wall_timeout=10, api_key_env='RSI_TEST_API_KEY'),
                        context=str(refs), execution=dict(mode='docker', image='test:1'),
                        task=dict(paths=dict(evidence=str(evidence))))
        with patch('rsi.tools.llm.subprocess.Popen', side_effect=self.start_file_worker) as start, \
                patch('rsi.tools.sandbox._docker') as docker:
            docker.return_value.returncode = 0
            for number in (1, 2):
                job = copy.deepcopy(original)
                candidate = self.runtime.root / 'work' / str(number) / 'code'
                candidate.mkdir(parents=True)
                job['workspace'] = str(candidate)
                directory = self.runtime.root / 'sessions' / str(number)
                result = self.runtime.call_session(job, directory)
                self.assertEqual(result['status'], 'ok', result.get('reason'))
                mapped = read_json(directory / 'container_job.json')
                self.assertEqual(mapped['workspace'], f'/work/{number}/code')
                self.assertEqual(mapped['task']['paths']['evidence'], f'/inputs/{number}.json')
            self.assertEqual(start.call_count, 1)
            command = start.call_args.args[0]
            self.assertEqual(command[:2], ['docker', 'run'])
            name = command[command.index('--name') + 1]
            docker.assert_not_called()
            self.runtime.close()
            docker.assert_called_once_with(['rm', '--force', name])

    def test_container_is_removed_if_worker_start_fails(self):
        refs = self.root / 'refs'
        refs.mkdir()
        candidate = self.runtime.root / 'work/candidate/code'
        candidate.mkdir(parents=True)
        evidence = self.root / 'investigation.json'
        write_json(evidence, {})
        job = dict(role='editor', workspace=str(candidate), context=str(refs),
                   model=dict(wall_timeout=1, api_key_env='RSI_TEST_API_KEY'),
                   execution=dict(mode='docker', image='test:1'), task=dict(paths=dict(evidence=str(evidence))))
        with patch('rsi.tools.llm.subprocess.Popen', side_effect=OSError('fixture start failure')), \
                patch('rsi.tools.sandbox._docker') as docker:
            docker.return_value.returncode = 0
            with self.runtime:
                result = self.runtime.call_session(job, self.runtime.root / 'sessions/failure')
            self.assertEqual(result['status'], 'error')
            self.assertIn('fixture start failure', result['reason'])
            self.assertEqual(docker.call_count, 1)
            self.assertEqual(docker.call_args.args[0][:2], ['rm', '--force'])


if __name__ == '__main__':
    unittest.main()
