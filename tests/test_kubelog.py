"""kubectl-free log access: kubeconfig token, API URL and node files."""

import os
import tempfile
import unittest

from macpm.modellog import find_pod_log, kube_log_url, kubeconfig_credentials


class KubeConfigTests(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        handle.write(text)
        handle.close()
        return handle.name

    def test_server_and_token_are_read(self):
        path = self.write(
            "apiVersion: v1\nclusters:\n- cluster:\n    server: https://10.0.0.1:6443\n"
            "users:\n- user:\n    token: abc123\n"
        )
        try:
            self.assertEqual(kubeconfig_credentials(path),
                             ("https://10.0.0.1:6443", "abc123"))
        finally:
            os.unlink(path)

    def test_missing_file(self):
        self.assertEqual(kubeconfig_credentials("/nope"), ("", ""))

    def test_log_url_carries_the_bearer_token(self):
        os.environ["K8S_APISERVER"] = "https://api:6443"
        os.environ["K8S_TOKEN"] = "secret"
        try:
            url, headers = kube_log_url("pod-1")
        finally:
            os.environ.pop("K8S_APISERVER")
            os.environ.pop("K8S_TOKEN")

        self.assertIn("/api/v1/namespaces/default/pods/pod-1/log", url)
        self.assertEqual(headers["Authorization"], "Bearer secret")


class NodeLogTests(unittest.TestCase):
    def test_newest_pod_log_is_chosen(self):
        root = tempfile.mkdtemp()
        try:
            older = os.path.join(root, "default_app-1_uid", "vllm")
            newer = os.path.join(root, "default_app-2_uid", "vllm")
            os.makedirs(older)
            os.makedirs(newer)
            first = os.path.join(older, "0.log")
            second = os.path.join(newer, "0.log")
            open(first, "w").close()
            open(second, "w").close()
            os.utime(first, (1000, 1000))
            os.utime(second, (2000, 2000))

            self.assertEqual(find_pod_log("app", root), second)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
