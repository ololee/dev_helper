"""Execution-queue regressions use fake operators, never user files or clouds."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_library import AssetLibraryError
import file_operations


class FileOperationsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = SimpleNamespace(root=self.root, library=object(), public_error=lambda error: 'Generated safe failure')
        self.local, self.netdisk = Mock(), Mock()
        for operator in (self.local, self.netdisk):
            operator.plan.side_effect = lambda operation, items, **kwargs: {
                'operation': operation, 'items': [{'name': 'generated.txt', 'source': 'generated',
                    '_fingerprint': 'private-item-value', 'nested': {'visible': True, '_key': 'private-nested-value'}}],
                '_secret': 'private-plan-value', 'fileCount': 1, 'bytes': 3}
            operator.execute.return_value = {'items': [{'state': 'completed', '_receipt': 'private-result-value'}],
                                             'state': 'completed', '_token': 'private-token-value'}
        self.local.restore.return_value = {'state': 'completed', 'trashId': 'generated-trash'}
        self.local.list_trash.return_value = {'items': [{'trashId': 'generated-trash', 'name': 'generated.txt'}]}
        self.local_patch = patch.object(file_operations, 'LocalFileOperator', return_value=self.local)
        self.cloud_patch = patch.object(file_operations, 'NetdiskFileOperator', return_value=self.netdisk)
        self.local_patch.start(); self.cloud_patch.start()
        self.service = file_operations.FileOperationsService(self.workspace)

    def tearDown(self):
        self.service.close()
        self.local_patch.stop(); self.cloud_patch.stop()
        self.temp.cleanup()

    def plan(self, scope='local', operation='copy'):
        return self.service.plan(scope, operation, [{'path': 'generated.txt'}], destination={'path': 'target'})

    def execute(self, plan, scope=None):
        return self.service.execute(plan['planId'], plan['planDigest'], scope)

    def wait_job(self, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.job(identifier)
            if job['state'] not in ('queued', 'running'):
                return job
            time.sleep(.005)
        self.fail('Generated queued operation did not settle')

    def test_public_plan_and_persisted_result_recursively_filter_private_values(self):
        plan = self.plan()
        serialized = json.dumps(plan)
        for secret in ('private-item-value', 'private-nested-value', 'private-plan-value'):
            self.assertNotIn(secret, serialized)
        self.assertTrue(plan['items'][0]['nested']['visible'])
        internal = self.service.plans[plan['planId']]['internal']
        self.assertEqual(internal['_secret'], 'private-plan-value')
        job = self.wait_job(self.execute(plan)['jobId'])
        self.assertEqual(job['state'], 'completed')
        for secret in ('private-result-value', 'private-token-value'):
            self.assertNotIn(secret, json.dumps(job))
            self.assertNotIn(secret, self.service.path.read_text())
        self.local.execute.assert_called_once_with(internal)

    def test_digest_expiry_and_unknown_plan_reject_without_operator_work(self):
        plan = self.plan()
        for identifier, digest in [(plan['planId'], '0' * 64), (plan['planId'], None), (str(uuid.uuid4()), plan['planDigest'])]:
            with self.assertRaises(AssetLibraryError) as caught:
                self.service.execute(identifier, digest)
            self.assertEqual(caught.exception.status, 409)
        with patch.object(file_operations.time, 'time', return_value=plan['expiresAt'] + 1):
            with self.assertRaises(AssetLibraryError) as caught:
                self.execute(plan)
            self.assertEqual(caught.exception.status, 409)
        self.local.execute.assert_not_called()

    def test_same_plan_concurrent_execute_runs_exactly_once(self):
        entered, release = threading.Event(), threading.Event()
        def work(plan):
            entered.set()
            self.assertTrue(release.wait(3))
            return {'items': [{'state': 'completed'}]}
        self.local.execute.side_effect = work
        plan = self.plan()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = list(pool.map(lambda _: self.execute(plan), range(2)))
            self.assertEqual(first['jobId'], second['jobId'])
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.local.execute.call_count, 1)
        finally:
            release.set()
        self.assertEqual(self.wait_job(first['jobId'])['state'], 'completed')

    def test_scope_isolation_applies_to_execute_job_and_list(self):
        plan = self.plan('netdisk')
        with self.assertRaises(AssetLibraryError) as caught:
            self.execute(plan, 'local')
        self.assertEqual(caught.exception.status, 403)
        self.netdisk.execute.assert_not_called()
        job = self.wait_job(self.execute(plan, 'netdisk')['jobId'])
        with self.assertRaises(AssetLibraryError) as caught:
            self.service.job(job['jobId'], 'local')
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.service.list_jobs('local')['items'], [])
        self.assertEqual(self.service.list_jobs('netdisk')['items'][0]['jobId'], job['jobId'])
        with self.assertRaises(AssetLibraryError):
            self.plan('invalid')
        self.local.execute.assert_not_called()

    def test_partial_unknown_skipped_and_nonfinal_provider_results_are_not_success(self):
        examples = [
            (['completed', 'failed'], 'completed-with-errors', 1, 1),
            (['completed', 'unknown'], 'completed-with-errors', 1, 1),
            (['unknown'], 'failed', 0, 1),
            (['skipped'], 'failed', 0, 0),
        ]
        for states, expected, completed, failed in examples:
            with self.subTest(states=states):
                self.local.execute.return_value = {'state': 'completed', 'items': [{'state': state} for state in states]}
                job = self.wait_job(self.execute(self.plan())['jobId'])
                self.assertEqual(job['state'], expected)
                self.assertEqual(job['completedCount'], completed)
                self.assertEqual(job['failedCount'], failed)
        self.local.execute.return_value = {'state': 'queued', 'items': []}
        self.assertEqual(self.wait_job(self.execute(self.plan())['jobId'])['state'], 'failed')

    def test_restart_marks_queued_running_interrupted_without_replaying_work(self):
        self.service.close()
        initial = {str(uuid.uuid4()): {'jobId': str(uuid.uuid4()), 'scope': 'local', 'operation': 'copy',
                    'state': state, 'items': [], 'createdAt': 1, 'updatedAt': 1}
                   for state in ('queued', 'running', 'completed')}
        self.service.path.write_text(json.dumps(initial))
        self.service = file_operations.FileOperationsService(self.workspace)
        states = [item['state'] for item in self.service.list_jobs()['items']]
        self.assertEqual(states.count('interrupted'), 2)
        self.assertEqual(states.count('completed'), 1)
        self.assertEqual(self.service.plans, {})
        self.local.execute.assert_not_called()
        self.netdisk.execute.assert_not_called()
        self.assertEqual(sum(x['state'] == 'interrupted' for x in json.loads(self.service.path.read_text()).values()), 2)

    def test_close_waits_for_running_work_but_never_starts_queued_mutation(self):
        entered, release = threading.Event(), threading.Event()
        def work(plan):
            entered.set()
            release.wait(3)
            return {'items': [{'state': 'completed'}]}
        self.local.execute.side_effect = work
        first = self.execute(self.plan())
        self.assertTrue(entered.wait(2))
        second = self.execute(self.plan())
        closing = threading.Thread(target=self.service.close)
        closing.start()
        deadline = time.monotonic() + 2
        while not self.service.closed and time.monotonic() < deadline:
            time.sleep(.001)
        release.set()
        closing.join(3)
        self.assertFalse(closing.is_alive())
        self.assertEqual(self.local.execute.call_count, 1)
        self.assertEqual(self.service.job(first['jobId'])['state'], 'completed')
        self.assertEqual(self.service.job(second['jobId'])['state'], 'interrupted')
        with self.assertRaises(AssetLibraryError):
            self.plan()

    def test_restore_and_local_trash_are_wired_to_the_local_operator(self):
        self.assertEqual(self.service.trash()['items'][0]['trashId'], 'generated-trash')
        self.local.list_trash.assert_called_once_with()
        job = self.wait_job(self.service.restore('generated-trash')['jobId'])
        self.assertEqual(job['scope'], 'local')
        self.assertEqual(job['operation'], 'restore')
        self.assertEqual(job['state'], 'completed')
        self.local.restore.assert_called_once_with('generated-trash')
        self.netdisk.restore.assert_not_called()

    def test_errors_are_safe_and_public_plan_copies_cannot_change_internal_work(self):
        plan = self.plan()
        modified = copy.deepcopy(plan)
        modified['items'][0]['name'] = 'unexpected'
        self.assertEqual(self.service.plans[plan['planId']]['public']['items'][0]['name'], 'generated.txt')
        self.local.execute.side_effect = RuntimeError('not public: generated fixture detail')
        job = self.wait_job(self.execute(plan)['jobId'])
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['error'], 'Generated safe failure')
        self.assertNotIn('not public', json.dumps(job))


if __name__ == '__main__':
    unittest.main()
