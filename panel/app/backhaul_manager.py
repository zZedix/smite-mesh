"""Backhaul server management for panel"""
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Any


logger = logging.getLogger(__name__)


class BackhaulManager:
    """Manages Backhaul server processes on the panel"""

    SERVER_OPTION_KEYS = [
        "token",
        "nodelay",
        "keepalive_period",
        "channel_size",
        "log_level",
        "pprof",
        "mux_session",
        "mux_version",
        "mux_framesize",
        "mux_recievebuffer",
        "mux_streambuffer",
        "sniffer",
        "web_port",
        "sniffer_log",
        "tls_cert",
        "tls_key",
        "heartbeat",
        "mux_con",
        "accept_udp",
        "skip_optz",
        "mss",
        "so_rcvbuf",
        "so_sndbuf",
        "proxy_protocol",
    ]

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        binary_path: Optional[Path] = None,
    ):
        resolved_config = config_dir or Path(
            os.environ.get("SMITE_BACKHAUL_CONFIG_DIR", "/app/data/backhaul")
        )
        self.config_dir = Path(resolved_config)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.processes: Dict[str, subprocess.Popen] = {}
        self.log_handles: Dict[str, Any] = {}
        default_binary = binary_path or Path(
            os.environ.get("BACKHAUL_SERVER_BINARY", "/usr/local/bin/backhaul")
        )
        self.binary_candidates = [
            Path(default_binary),
            Path("backhaul"),
        ]

    def start_server(self, tunnel_id: str, spec: dict) -> bool:
        """Start a Backhaul server for a tunnel"""
        config_path = self.config_dir / f"{tunnel_id}.toml"
        log_path = self.config_dir / f"backhaul_{tunnel_id}.log"

        config_content = self._build_server_config(spec or {})
        if not config_content.strip():
            raise ValueError("Backhaul config is empty")

        config_path.write_text(config_content, encoding="utf-8")

        if tunnel_id in self.processes:
            self.stop_server(tunnel_id)

        binary_path = self._resolve_binary_path()

        log_fh = log_path.open("w", buffering=1)
        log_fh.write(f"Starting Backhaul server for tunnel {tunnel_id}\n")
        log_fh.write(config_content)
        log_fh.flush()

        try:
            proc = subprocess.Popen(
                [str(binary_path), "-c", str(config_path)],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=str(self.config_dir),
                start_new_session=True,
            )
        except Exception:
            log_fh.close()
            raise

        self.processes[tunnel_id] = proc
        self.log_handles[tunnel_id] = log_fh

        time.sleep(1.0)
        if proc.poll() is not None:
            error_output = ""
            try:
                error_output = log_path.read_text(encoding="utf-8")[-1000:]
            except Exception:
                pass
            self._cleanup_process(tunnel_id)
            raise RuntimeError(
                f"Backhaul server failed to start (exit code {proc.returncode}). "
                f"Log tail: {error_output}"
            )

        logger.info("Started Backhaul server for tunnel %s using config %s", tunnel_id, config_path)
        return True

    def stop_server(self, tunnel_id: str):
        """Stop Backhaul server for a tunnel"""
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            except Exception as exc:
                logger.warning("Error stopping Backhaul server for tunnel %s: %s", tunnel_id, exc)
            finally:
                self._cleanup_process(tunnel_id)

        config_path = self.config_dir / f"{tunnel_id}.toml"
        if config_path.exists():
            try:
                config_path.unlink()
            except Exception as exc:
                logger.warning("Failed to remove Backhaul config %s: %s", config_path, exc)

    def is_running(self, tunnel_id: str) -> bool:
        """Return True if server process is running"""
        proc = self.processes.get(tunnel_id)
        return proc is not None and proc.poll() is None

    def cleanup_all(self):
        """Stop all Backhaul servers"""
        for tunnel_id in list(self.processes.keys()):
            self.stop_server(tunnel_id)

    def get_active_servers(self) -> List[str]:
        """Return active Backhaul tunnel IDs"""
        active = []
        for tunnel_id, proc in list(self.processes.items()):
            if proc.poll() is None:
                active.append(tunnel_id)
            else:
                self._cleanup_process(tunnel_id)
        return active

    def _cleanup_process(self, tunnel_id: str):
        if tunnel_id in self.processes:
            del self.processes[tunnel_id]
        if tunnel_id in self.log_handles:
            try:
                self.log_handles[tunnel_id].close()
            except Exception:
                pass
            del self.log_handles[tunnel_id]

    def _build_server_config(self, spec: dict) -> str:
        transport = (spec.get("transport") or spec.get("type") or "tcp").lower()
        server_options = dict(spec.get("server_options") or {})

        # UDP over TCP helper toggle
        accept_udp = spec.get("accept_udp", server_options.get("accept_udp", False))
        if transport in {"tcp", "tcpmux"} and accept_udp:
            server_options["accept_udp"] = True

        bind_addr = spec.get("bind_addr")
        if not bind_addr:
            control_port = spec.get("control_port")
            if not control_port:
                control_port = spec.get("listen_port")
            try:
                control_port = int(control_port)
            except (TypeError, ValueError):
                control_port = 3080
            # Get IPv6 preference from spec
            use_ipv6 = spec.get("use_ipv6", False)
            if use_ipv6:
                bind_ip = spec.get("bind_ip", "::")
            else:
                bind_ip = spec.get("bind_ip", "0.0.0.0")
            bind_addr = f"{bind_ip}:{control_port}"

        ports = self._build_ports(spec)

        server_config: Dict[str, Any] = {
            "bind_addr": bind_addr,
            "transport": transport,
            "ports": ports,
        }

        # Token persisted at top-level or inside server_options
        token = spec.get("token") or server_options.get("token")
        if token:
            server_config["token"] = token

        for key in self.SERVER_OPTION_KEYS:
            value = server_options.get(key)
            if value is None or value == "":
                continue
            server_config[key] = value

        tls_cert = spec.get("tls_cert") or spec.get("tls_cert_path")
        if tls_cert:
            server_config["tls_cert"] = tls_cert
        tls_key = spec.get("tls_key") or spec.get("tls_key_path")
        if tls_key:
            server_config["tls_key"] = tls_key

        return self._render_toml({"server": server_config})

    def _build_ports(self, spec: dict) -> List[str]:
        ports_spec = spec.get("ports")
        if isinstance(ports_spec, list) and ports_spec:
            return [str(item) for item in ports_spec if str(item).strip()]

        listen_port = spec.get("public_port") or spec.get("listen_port")
        target_addr = spec.get("target_addr")
        if not target_addr:
            target_host = spec.get("target_host", "127.0.0.1")
            target_port = spec.get("target_port") or listen_port
            if target_port is None:
                return []
            # Use format_address_port to properly handle IPv6 addresses
            from app.utils import format_address_port
            target_addr = format_address_port(target_host, target_port)
        listen_ip = spec.get("listen_ip", spec.get("public_ip", "0.0.0.0"))

        if listen_port is None:
            return []

        try:
            listen_port = int(listen_port)
        except (TypeError, ValueError):
            return []

        if listen_ip and listen_ip not in {"0.0.0.0", "::", ""}:
            listen_part = f"{listen_ip}:{listen_port}"
        else:
            listen_part = str(listen_port)

        entry = f"{listen_part}={target_addr}" if target_addr else listen_part
        return [entry]

    def _render_toml(self, data: Dict[str, Dict[str, Any]]) -> str:
        lines: List[str] = []

        def format_value(value: Any) -> str:
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, list):
                if not value:
                    return "[]"
                rendered = ",\n  ".join(f"\"{str(item)}\"" for item in value)
                return "[\n  " + rendered + "\n]"
            value_str = str(value)
            value_str = value_str.replace("\\", "\\\\").replace('"', '\\"')
            return f"\"{value_str}\""

        for section, values in data.items():
            lines.append(f"[{section}]")
            for key, value in values.items():
                if value is None:
                    continue
                lines.append(f"{key} = {format_value(value)}")
            lines.append("")

        return "\n".join(lines).strip() + "\n"

    def _resolve_binary_path(self) -> Path:
        for candidate in self.binary_candidates:
            if candidate.exists():
                return candidate

        resolved = shutil.which("backhaul")
        if resolved:
            return Path(resolved)

        raise FileNotFoundError(
            "Backhaul binary not found. Expected at BACKHAUL_SERVER_BINARY, '/usr/local/bin/backhaul', or in PATH."
        )


backhaul_manager = BackhaulManager()


