"""Model names recovered from the launcher's cmdline via the parent chain."""

import os
import shutil
import tempfile
import unittest

from macpm.linux_backend.modelname import extract, resolve, reset_model_cache, set_proc_root


def build_proc(root, pid, cmdline, ppid=1, comm=""):
    directory = os.path.join(root, str(pid))
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "cmdline"), "wb") as handle:
        handle.write(b"\0".join(part.encode() for part in cmdline))
    with open(os.path.join(directory, "stat"), "w", encoding="utf-8") as handle:
        handle.write(f"{pid} ({comm or cmdline[0].split('/')[-1]}) S {ppid} {pid} 0 0\n")
    with open(os.path.join(directory, "comm"), "w", encoding="utf-8") as handle:
        handle.write((comm or cmdline[0].split("/")[-1]) + "\n")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="macpm-proc-")
        build_proc(
            self.root, 900,
            "/usr/bin/python3 /usr/bin/vllm serve /data/Qwen3.6-35B-A3B "
            "--served-model-name Qwen3.6-35B-A3B --port 8000 --trust-remote-code"
            .split(" "),
            ppid=550, comm="python3",
        )
        build_proc(self.root, 550, ["/usr/bin/containerd-shim-runc-v2", "-id", "abc"],
                   ppid=1, comm="containerd-shim-runc-v2")
        build_proc(self.root, 800, ["VLLM::EngineCore"], ppid=900, comm="VLLM::EngineCore")
        build_proc(self.root, 700, ["VLLM::Worker_TP0"], ppid=800, comm="VLLM::Worker_TP0")
        # k8s-style wrapper: the whole command hides inside one argument
        build_proc(self.root, 600, ["bash", "-c",
                                    "vllm serve /models/Llama-3.1-8B "
                                    "--served-model-name llama8"],
                   ppid=1, comm="bash")
        build_proc(self.root, 500, ["python3", "/opt/train.py"], ppid=1, comm="python3")
        set_proc_root(self.root)
        reset_model_cache()

    def tearDown(self):
        set_proc_root("/proc")
        reset_model_cache()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_served_name_is_found_two_hops_up(self):
        self.assertEqual(resolve(700), "Qwen3.6-35B-A3B")

    def test_bash_wrapper_is_unwrapped(self):
        self.assertEqual(resolve(600), "llama8")

    def test_walk_stops_at_the_container_shim(self):
        # 550's parent is PID 1, so nothing beyond the shim is inspected.
        self.assertEqual(resolve(550), "")

    def test_unknown_process_returns_empty(self):
        self.assertEqual(resolve(500), "")

    def test_result_is_cached(self):
        self.assertEqual(resolve(700), "Qwen3.6-35B-A3B")
        build_proc(self.root, 900, ["/usr/bin/vllm", "serve", "/data/Other-Model"],
                   ppid=550, comm="python3")
        self.assertEqual(resolve(700), "Qwen3.6-35B-A3B")
        reset_model_cache()
        self.assertEqual(resolve(700), "Other-Model")

    def test_missing_proc_tree_is_harmless(self):
        set_proc_root(os.path.join(self.root, "does-not-exist"))
        self.assertEqual(resolve(700), "")


class ExtractTests(unittest.TestCase):
    def test_sglang_model_path(self):
        self.assertEqual(
            extract("python3 -m sglang.launch_server --model-path "
                    "/data/DeepSeek-V4-Flash-0731 --page-size 256"),
            "DeepSeek-V4-Flash-0731",
        )

    def test_api_server_with_hf_id(self):
        self.assertEqual(
            extract("python3 -m vllm.entrypoints.openai.api_server --model "
                    "Qwen/Qwen2.5-72B-Instruct --served-model-name qwen72b,alias"),
            "qwen72b",
        )

    def test_tgi_model_id(self):
        self.assertEqual(
            extract("text-generation-launcher --model-id "
                    "mistralai/Mistral-7B-Instruct-v0.3"),
            "Mistral-7B-Instruct-v0.3",
        )

    def test_llama_cpp_gguf(self):
        self.assertEqual(
            extract("/usr/bin/llama-server -m /models/gguf/qwen2.5-7b-q4.gguf"),
            "qwen2.5-7b-q4",
        )

    def test_vllm_serve_positional(self):
        self.assertEqual(extract("vllm serve /data/MiniMax-M2.5-AWQ --port 8000"),
                         "MiniMax-M2.5-AWQ")

    def test_unrelated_command(self):
        self.assertEqual(extract("/usr/bin/nginx -g daemon off;"), "")


if __name__ == "__main__":
    unittest.main()
