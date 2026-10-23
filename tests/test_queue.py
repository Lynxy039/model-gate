import threading
import unittest

from model_gate.gate import AdmissionGate


class QueueContractTests(unittest.TestCase):
    def test_halt_rejects_new_requests_until_resume(self):
        gate = AdmissionGate({})
        gate.halt()
        with self.assertRaises(RuntimeError):
            gate.acquire('ds4', 'model', timeout=0)
        self.assertTrue(gate.snapshot()['halted'])
        gate.resume()
        gate.acquire('ds4', 'model', timeout=0).release()

    def test_waiting_request_has_id_and_can_be_cancelled(self):
        gate = AdmissionGate({})
        lease = gate.acquire('ds4', 'one')
        errors = []
        ready = threading.Event()
        def worker():
            ready.set()
            try:
                gate.acquire('ds4', 'two', request_id=42)
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=worker)
        thread.start()
        ready.wait(1)
        with gate._condition:
            gate._condition.wait_for(lambda: gate.snapshot()['waiting'] == 1, timeout=1)
        try:
            self.assertEqual(gate.snapshot()['queue'][0]['id'], 42)
            self.assertTrue(gate.cancel(42))
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
        finally:
            lease.release()
            thread.join(1)

    def test_disconnected_queued_client_is_not_admitted(self):
        gate = AdmissionGate({})
        with self.assertRaises(RuntimeError):
            gate.acquire('ds4', 'one', cancelled=lambda: True)
        self.assertEqual(gate.snapshot()['active'], 0)
