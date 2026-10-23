import threading
import unittest
from unittest.mock import Mock, patch

from model_gate.control import Controller
from model_gate.gate import AdmissionGate
from model_gate.panel import Actions
from model_gate.proxy import Backend, Lifecycle, LifecycleError, ProcessManager


class ControlTests(unittest.TestCase):
    def test_panel_control_only_uses_gate_api(self):
        actions = Actions('http://127.0.0.1:9000', '')
        actions._run = lambda label, call: call()
        with patch('model_gate.panel.post_json', return_value={}) as post, patch('model_gate.panel.subprocess.Popen') as spawn:
            actions.restart_gate()
            actions.stop_gate()
            actions.emergency()
            actions.resume()
        self.assertEqual([c.args[0].split('/admin/')[1] for c in post.call_args_list],
                         ['shutdown', 'shutdown', 'halt', 'resume'])
        spawn.assert_not_called()

    def test_native_backend_stops_through_unload_hook(self):
        backend = Backend('omlx', 'http://127.0.0.1:1', 1,
                          unload_url='http://127.0.0.1:1/unload/{model}', model_status_path=None)
        gate = AdmissionGate({})
        processes = Mock()
        lifecycle = Lifecycle({'omlx': backend}, processes=processes)
        lifecycle._post = Mock()
        lifecycle.activate('omlx', 'model')
        control = Controller(gate, lifecycle, processes, {'omlx': backend}, Mock())
        control.stop('omlx')
        lifecycle._post.assert_called_once_with('http://127.0.0.1:1/unload/model')
        processes.stop.assert_not_called()
        self.assertEqual(lifecycle.snapshot(), {'omlx': []})

    def test_halt_closes_admission_before_waiting_for_startup(self):
        backend = Backend('ds4', 'http://127.0.0.1:1', 1, autostart_command=['server'], requires_unload=False)
        gate = AdmissionGate({})
        processes = Mock()
        lifecycle = Lifecycle({'ds4': backend}, processes=processes)
        entered, release = threading.Event(), threading.Event()
        errors = []
        def ready(*args, cancelled):
            entered.set()
            release.wait(2)
            if cancelled():
                raise LifecycleError('cancelled')
        processes.ensure_ready.side_effect = ready
        def cancel_all():
            self.assertTrue(gate.snapshot()['halted'])
            release.set()
        control = Controller(gate, lifecycle, processes, {'ds4': backend}, cancel_all)
        def load():
            try:
                control.activate('ds4', 'one', cancelled=lambda: gate.snapshot()['halted'])
            except LifecycleError as error:
                errors.append(error)
        thread = threading.Thread(target=load)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(control.snapshot()['states']['ds4']['phase'], 'loading')
        result = control.halt()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result['ok'])
        self.assertTrue(errors)
        self.assertEqual(control.snapshot()['states']['ds4']['phase'], 'unloaded')
        with self.assertRaises(RuntimeError):
            gate.acquire('ds4', 'one', timeout=0)
        control.resume()
        gate.acquire('ds4', 'one', timeout=0).release()

    def test_managed_backend_never_adopts_external_ready_server(self):
        backend = Backend('ds4', 'http://127.0.0.1:1', 1, autostart_command=['server'])
        manager = ProcessManager({'ds4': backend})
        manager._ready = Mock(return_value=True)
        with patch('model_gate.proxy.subprocess.Popen') as spawn, self.assertRaises(LifecycleError):
            manager.ensure_ready('ds4', 'one')
        spawn.assert_not_called()
