"""
Masternode Network Manager

Manages multiple dashd instances for masternode network generation.
Handles node lifecycle, peer connections, and DKG cycle mining.
"""

import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .dashd_manager import dashd_preexec_fn
from .rpc_client import DashRPCClient


class MasternodeNode:
    """A single dashd node in the masternode network."""

    def __init__(self, name, dashd_path, datadir, rpc_port, p2p_port, extra_args=None):
        self.name = name
        self.dashd_path = dashd_path
        self.datadir = Path(datadir)
        self.rpc_port = rpc_port
        self.p2p_port = p2p_port
        self.extra_args = extra_args or []
        self.process = None
        self.rpc = None

    def start(self):
        """Start the dashd process."""
        regtest_dir = self.datadir / "regtest"
        regtest_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.dashd_path,
            "-regtest",
            f"-datadir={self.datadir}",
            f"-port={self.p2p_port}",
            f"-rpcport={self.rpc_port}",
            "-server=1",
            "-daemon=0",
            "-fallbackfee=0.00001",
            "-rpcbind=127.0.0.1",
            "-rpcallowip=127.0.0.1",
            "-listen=1",
            "-txindex=0",
            "-addressindex=0",
            "-spentindex=0",
            "-timestampindex=0",
        ]
        cmd.extend(self.extra_args)

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=str(self.datadir),
            preexec_fn=dashd_preexec_fn,
        )

        # Derive dash-cli path
        dashd_bin = Path(self.dashd_path)
        if dashd_bin.is_absolute():
            dashcli_path = str(dashd_bin.parent / "dash-cli")
        else:
            dashcli_path = "dash-cli"

        self.rpc = DashRPCClient(
            dashcli_path=dashcli_path,
            datadir=str(self.datadir),
            rpc_port=self.rpc_port,
        )

        # Wait for RPC to become ready
        if not self._wait_for_ready(timeout=60):
            self.stop()
            raise RuntimeError(f"Node {self.name} failed to start within 60 seconds")

        print(f"    {self.name} started (PID: {self.process.pid}, RPC: {self.rpc_port}, P2P: {self.p2p_port})")

    def _wait_for_ready(self, timeout=60):
        """Wait for dashd to accept RPC calls."""
        start = time.time()
        while time.time() - start < timeout:
            if self.process.poll() is not None:
                if self.process.stderr:
                    stderr = self.process.stderr.read().decode("utf-8", errors="replace").strip()
                    if stderr:
                        print(f"    {self.name} exited with error: {stderr}")
                return False
            try:
                self.rpc.call("getblockcount")
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def stop(self):
        """Stop the dashd process gracefully via RPC, falling back to SIGTERM."""
        if self.process:
            # Try RPC stop first for clean shutdown (flushes evoDB, quorum snapshots)
            if self.rpc:
                try:
                    self.rpc.call("stop")
                    self.process.wait(timeout=30)
                    self.process = None
                    return
                except Exception:
                    pass
            # Fallback to SIGTERM
            try:
                self.process.terminate()
                self.process.wait(timeout=15)
            except Exception:
                try:
                    self.process.kill()
                    self.process.wait()
                except Exception:
                    pass
            self.process = None

    def force_finish_mnsync(self, attempts=20, poll=0.5):
        """Force mnsync completion.

        Masternodes reject connections until they have finished mnsync, so
        this must be driven explicitly after start-up before peer connections
        are issued.
        """
        for _ in range(attempts):
            try:
                status = self.rpc.call("mnsync", "status")
                if status.get("IsSynced", False):
                    return
                self.rpc.call("mnsync", "next")
                time.sleep(poll)
            except Exception:
                time.sleep(poll)

    def set_mocktime(self, mocktime, seconds=None):
        """Set mocktime on this node and optionally tick the scheduler.

        When `seconds` is given, also runs `mockscheduler` so scheduled tasks
        (DKG session processing, etc.) fire at the advanced time.
        """
        try:
            self.rpc.call("setmocktime", mocktime)
            if seconds is not None:
                self.rpc.call("mockscheduler", seconds)
        except Exception:
            # Nodes may be briefly unresponsive during DKG work; tolerate.
            pass


def find_free_port(start=19000, attempts=100):
    """Find an available TCP port."""
    for port in range(start, start + attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No free port found in range {start}-{start + attempts - 1}")


class MasternodeNetwork:
    """Manages a multi-node masternode network for test data generation.

    Topology: 1 controller + N masternode nodes, all connected.
    """

    def __init__(self, dashd_path, num_masternodes=4, base_extra_args=None):
        self.dashd_path = dashd_path
        self.num_masternodes = num_masternodes
        self.base_extra_args = base_extra_args or []
        self.controller = None
        self.masternodes = []
        self.temp_dirs = []
        self.masternode_info = []  # BLS keys, addresses, proTxHashes
        self.mn_p2p_ports = []  # Pre-allocated P2P ports for MN registration
        self.fund_address = None  # Address holding mining rewards (set in bootstrap)
        self.mocktime = 0  # Shared mock time, advanced via set/bump_mocktime

    def all_nodes(self):
        """Return [controller, *masternodes], skipping any that are not started."""
        return [n for n in ([self.controller] + self.masternodes) if n is not None]

    def set_mocktime(self, mocktime):
        """Set mocktime on all running nodes and update the tracked value."""
        self.mocktime = mocktime
        for node in self.all_nodes():
            node.set_mocktime(mocktime)

    def bump_mocktime(self, seconds=1):
        """Advance mocktime by `seconds` on all running nodes and tick the scheduler."""
        self.mocktime += seconds
        for node in self.all_nodes():
            node.set_mocktime(self.mocktime, seconds=seconds)

    def move_blocks(self, count):
        """Bump mocktime, mine `count` blocks on the controller, then sync masternodes."""
        if count <= 0:
            return
        self.bump_mocktime(1)
        self.generate_blocks(count)
        self.wait_for_sync()

    def allocate_mn_ports(self):
        """Pre-allocate P2P ports for masternodes (needed for protx registration)."""
        base = 19950
        for i in range(self.num_masternodes):
            p2p_port = find_free_port(base + i * 10)
            self.mn_p2p_ports.append(p2p_port)
        return self.mn_p2p_ports

    def start_controller(self, extra_args=None):
        """Start the controller node from a fresh temp directory."""
        temp_dir = Path(tempfile.mkdtemp(prefix="dash-mn-controller-"))
        self.temp_dirs.append(temp_dir)

        rpc_port = find_free_port(19900)
        p2p_port = find_free_port(rpc_port + 1)

        all_args = list(self.base_extra_args)
        if extra_args:
            all_args.extend(extra_args)
        # Block filter index for SPV testing
        all_args.extend(["-blockfilterindex=1", "-peerblockfilters=1"])

        self.controller = MasternodeNode(
            name="controller",
            dashd_path=self.dashd_path,
            datadir=temp_dir,
            rpc_port=rpc_port,
            p2p_port=p2p_port,
            extra_args=all_args,
        )
        self.controller.start()
        return self.controller

    def start_masternode_nodes(self):
        """Start masternode nodes from a copy of the controller's datadir.

        Each node gets a unique BLS private key. Connection and mnsync
        must be handled by the caller after this method returns.
        Must be called after masternodes have been registered on the controller.
        """
        print("\n  Starting masternode nodes...")
        assert self.mn_p2p_ports, "allocate_mn_ports() must be called before start_masternode_nodes()"

        # Stop controller briefly to copy its datadir
        controller_rpc_port = self.controller.rpc_port
        controller_p2p_port = self.controller.p2p_port
        controller_extra = list(self.controller.extra_args)
        controller_dir = self.controller.datadir

        self.controller.stop()
        time.sleep(2)

        # Restart controller with current mocktime baked into the command line.
        restart_args = [a for a in controller_extra if not a.startswith("-mocktime=")]
        restart_args.append(f"-mocktime={self.mocktime}")
        self.controller = MasternodeNode(
            name="controller",
            dashd_path=self.dashd_path,
            datadir=controller_dir,
            rpc_port=controller_rpc_port,
            p2p_port=controller_p2p_port,
            extra_args=restart_args,
        )
        self.controller.start()

        for i, mn_info in enumerate(self.masternode_info):
            mn_name = f"mn{i + 1}"
            temp_dir = Path(tempfile.mkdtemp(prefix=f"dash-{mn_name}-"))
            self.temp_dirs.append(temp_dir)

            # Copy controller's regtest data (blockchain, chainstate, evodb, llmq)
            src = controller_dir / "regtest"
            dst = temp_dir / "regtest"
            shutil.copytree(src, dst)
            # Remove stale network state from the copy
            for stale_file in ["peers.dat", "banlist.json", "mempool.dat", ".lock"]:
                stale_path = dst / stale_file
                if stale_path.exists():
                    stale_path.unlink()

            p2p_port = self.mn_p2p_ports[i]
            rpc_port = find_free_port(p2p_port + 1)

            mn_args = list(self.base_extra_args)
            mn_args.extend(
                [
                    "-blockfilterindex=1",
                    "-peerblockfilters=1",
                    "-txindex=1",
                    f"-masternodeblsprivkey={mn_info['bls_private_key']}",
                    f"-mocktime={self.mocktime}",
                ]
            )

            node = MasternodeNode(
                name=mn_name,
                dashd_path=self.dashd_path,
                datadir=temp_dir,
                rpc_port=rpc_port,
                p2p_port=p2p_port,
                extra_args=mn_args,
            )
            node.start()
            self.masternodes.append(node)

    def connect_all(self):
        """Establish the full controller↔MN and MN↔MN peer mesh.

        Following Dash Core's test framework, masternode threads are disabled
        during connection to prevent interference with the P2P handshake, and
        the direct MN↔MN links ensure DKG contributions propagate without
        waiting for the quorum manager to build them lazily.
        """

        def try_addnode(from_node, target_addr, label):
            try:
                from_node.rpc.call("addnode", target_addr, "onetry")
            except Exception as e:
                print(f"    Warning: addnode {label} failed: {e}")

        # Disable MN threads during connection (prevents handshake interference)
        for mn in self.masternodes:
            try:
                mn.rpc.call("setmnthreadactive", False)
            except Exception:
                pass

        controller_addr = f"127.0.0.1:{self.controller.p2p_port}"
        for mn in self.masternodes:
            try_addnode(mn, controller_addr, f"{mn.name}->controller")
            try_addnode(self.controller, f"127.0.0.1:{mn.p2p_port}", f"controller->{mn.name}")

        # Direct MN<->MN links: DIP-0024 quorums (minSize=4) need contributions
        # from every member, so seeding the mesh avoids phase-2 starvation.
        for i, mn_a in enumerate(self.masternodes):
            for mn_b in self.masternodes[i + 1 :]:
                try_addnode(mn_a, f"127.0.0.1:{mn_b.p2p_port}", f"{mn_a.name}->{mn_b.name}")

        # Re-enable MN threads
        for mn in self.masternodes:
            try:
                mn.rpc.call("setmnthreadactive", True)
            except Exception:
                pass

        # Wait for connections to establish
        peer_count = 0
        for _ in range(15):
            time.sleep(2)
            peer_count = len(self.controller.rpc.call("getpeerinfo"))
            if peer_count >= len(self.masternodes):
                break
        print(f"    Controller has {peer_count} peers connected")

    def stop_all(self):
        """Stop all nodes."""
        for mn in self.masternodes:
            mn.stop()
        if self.controller:
            self.controller.stop()

    def cleanup(self):
        """Stop all nodes and remove temp directories."""
        self.stop_all()
        for temp_dir in self.temp_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)
        self.temp_dirs.clear()

    def generate_blocks(self, count, address=None):
        """Mine blocks on the controller node."""
        rpc = self.controller.rpc
        if address is None:
            address = self.fund_address or rpc.call("getnewaddress")
        return rpc.call("generatetoaddress", count, address)

    def wait_for_sync(self, timeout=30):
        """Wait for all nodes to reach the same block height as controller."""
        target = self.controller.rpc.call("getblockcount")
        start = time.time()
        while time.time() - start < timeout:
            all_synced = True
            for mn in self.masternodes:
                height = mn.rpc.call("getblockcount")
                if height < target:
                    all_synced = False
                    break
            if all_synced:
                return True
            time.sleep(0.5)
        return False
