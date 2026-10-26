#!/usr/bin/env python3
"""A fresh checkout must reserve port blocks owned by other checkouts."""
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from embabel_setup import dockerlib, settings  # noqa: E402

with tempfile.TemporaryDirectory() as root:
    other = os.path.join(root, "other-checkout")
    os.mkdir(other)
    ui_env = os.path.join(other, ".env.ui-review")
    with open(ui_env, "w") as f:
        f.write("EMBABEL_PORT_BASE=11058\n")
    deleted_env = os.path.join(root, "deleted", ".env.photoquest-dogfood")

    def docker(*argv, **_kwargs):
        if argv[:2] == ("ps", "-a"):
            format_string = argv[argv.index("--format") + 1]
            if "project.environment_file" in format_string:
                output = (
                    f"ui-id\tembabel-ui-review\t.env.ui-review\t{other}\n"
                    f"dogfood-id\tembabel-photoquest-dogfood\t{deleted_env}\t{os.path.dirname(deleted_env)}\n"
                    f"current-id\tembabel-new-review\t\t{root}\n"
                )
            else:
                output = "embabel-ui-review\nembabel-photoquest-dogfood\nembabel-new-review\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        if argv and argv[0] == "inspect":
            bindings = {
                "ui-id": '{}',
                "dogfood-id": '{"11075/tcp":[{"HostIp":"127.0.0.1","HostPort":"11075"}]}',
                "current-id": '{"11091/tcp":[{"HostIp":"127.0.0.1","HostPort":"11091"}]}',
            }
            output = "\n".join(bindings[container_id] for container_id in argv[3:]) + "\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        raise AssertionError(f"unexpected docker call: {argv}")

    original_dir = settings.APPLIANCE_DIR
    original_instance = settings.instance()
    try:
        settings.APPLIANCE_DIR = root
        settings.use_instance("new-review")
        # The host's own listeners are a separate question, answered below; this
        # machine's real ports must not decide what the instance check sees.
        with patch.object(dockerlib, "_docker", side_effect=docker), \
                patch.object(settings, "_port_taken", return_value=False):
            actual = settings.free_port_base()
            assert actual == 11090, (
                f"allocator chose {actual} instead of preserving the occupied blocks"
            )
        # A process outside Docker holding one port of the next free block (a proxy on
        # 11099, its persisted-Bolt offset) moves the allocation to the block after it.
        with patch.object(dockerlib, "_docker", side_effect=docker), \
                patch.object(settings, "_port_taken", side_effect=lambda port: port == 11099):
            actual = settings.free_port_base()
            assert actual == 11106, (
                f"allocator chose {actual}, a block a host process already listens in"
            )
    finally:
        settings.APPLIANCE_DIR = original_dir
        settings.use_instance(original_instance)

print("ok: cross-checkout appliance port blocks, and blocks a host process listens in, remain reserved")
