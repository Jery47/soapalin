import tempfile
import unittest
from unittest import mock

from soaplun import jobs as jobs_module
from soaplun.config import Config


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(self.tmp.name)
        self.store = jobs_module.JobStore(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_list_and_stop(self):
        spec = self.store.create(["--create-user", "--list-user", "users.csv"])
        self.assertTrue(spec["id"])
        states = self.store.list()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["status"], "pending")
        state = self.store.request_stop(spec["id"])
        self.assertTrue(state["stop_requested"])
        self.assertTrue(self.store.stop_requested(spec["id"]))

    def test_launch_sets_running_state(self):
        spec = self.store.create(["--import-user", "mail"])

        class Process:
            pid = 1234

        with mock.patch.object(
            jobs_module.subprocess, "Popen", return_value=Process()
        ):
            self.store.launch(spec)
        state = self.store.state(spec["id"])
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["pid"], 1234)

    def test_selected_by_prefix(self):
        first = self.store.create(["--import-user", "mail"])
        second = self.store.create(["--import-user", "share"])
        self.assertEqual(self.store.selected(first["id"])[0]["id"], first["id"])
        self.assertEqual(len(self.store.selected("all")), 2)
        self.assertEqual(self.store.selected(second["id"])[0]["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
