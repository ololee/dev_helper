import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from desktop import control
except ImportError:
    import control


class ControlTests(unittest.TestCase):
    def state(self):
        return {'pid': 12345, 'port': 8876, 'host': '0.0.0.0',
                'argv': [str(control.PYTHON), '-u', str(control.SERVER), '--port', '8876'],
                'processStarted': 'Mon Oct  5 18:00:00 2026'}

    def test_stale_pid_is_status_only_and_does_not_signal(self):
        state = self.state()
        with patch.object(control, 'read_state', return_value=state), patch.object(control, 'health', return_value=None), \
             patch.object(control, 'process_identity', return_value=None), patch.object(control.os, 'kill') as kill:
            current = control.status()
        self.assertEqual(current['state'], 'stopped')
        self.assertTrue(current['staleRecord'])
        kill.assert_not_called()

    def test_stop_rejects_pid_reuse_without_signal(self):
        state = self.state()
        with patch.object(control, 'read_state', return_value=state), \
             patch.object(control, 'process_identity', return_value=('Tue Oct  6 18:00:00 2026', 'unrelated process')), \
             patch.object(control.os, 'kill') as kill:
            with self.assertRaises(control.ControlError):
                control.stop()
        kill.assert_not_called()

    def test_stop_rejects_different_health_pid_without_signal(self):
        state = self.state()
        identity = (state['processStarted'], ' '.join(state['argv']))
        with patch.object(control, 'read_state', return_value=state), \
             patch.object(control, 'process_identity', return_value=identity), \
             patch.object(control, 'health', return_value={'appId': control.APP_ID, 'pid': 54321}), \
             patch.object(control.os, 'kill') as kill:
            with self.assertRaises(control.ControlError):
                control.stop()
        kill.assert_not_called()

    def test_unmanaged_service_is_not_claimed(self):
        with patch.object(control, 'read_state', return_value=None), \
             patch.object(control, 'health', return_value={'appId': control.APP_ID, 'pid': 54321, 'port': 8876}), \
             patch.object(control.os, 'kill') as kill:
            current = control.status()
        self.assertEqual(current['state'], 'running')
        self.assertFalse(current['managed'])
        kill.assert_not_called()

    def test_state_reader_rejects_other_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'state.json'
            state = self.state()
            state['argv'] = ['/bin/sleep', '600']
            file.write_text(json.dumps(state))
            with self.assertRaises(control.ControlError):
                control.read_state(file)

    def test_framework_runtime_can_reexec_but_all_owned_arguments_must_match(self):
        state = self.state()
        runtime = str(getattr(control.sys, '_base_executable', control.sys.executable))
        suffix = ' ' + ' '.join(state['argv'][1:])
        self.assertTrue(control.authorized_command(state['argv'], runtime + suffix))
        self.assertFalse(control.authorized_command(state['argv'], '/bin/sleep' + suffix))
        self.assertFalse(control.authorized_command(state['argv'], runtime + suffix + ' --port 9999'))


if __name__ == '__main__':
    unittest.main()
