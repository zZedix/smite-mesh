"""iptables-based traffic tracking for tunnels on panel"""
import subprocess
import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Chain name for Smite traffic tracking
CHAIN_NAME = "SMITE_TRACK"


def _run_iptables(args: list, check: bool = True) -> subprocess.CompletedProcess:
    """Run iptables command"""
    cmd = ["iptables"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            timeout=5
        )
        return result
    except subprocess.TimeoutExpired:
        logger.error(f"iptables command timed out: {' '.join(cmd)}")
        raise
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else (e.stdout if e.stdout else str(e))
        if check:
            logger.warning(f"iptables command failed: {' '.join(cmd)}: {error_msg}")
        raise


def _run_ip6tables(args: list, check: bool = True) -> subprocess.CompletedProcess:
    """Run ip6tables command for IPv6"""
    cmd = ["ip6tables"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            timeout=5
        )
        return result
    except subprocess.TimeoutExpired:
        logger.error(f"ip6tables command timed out: {' '.join(cmd)}")
        raise
    except subprocess.CalledProcessError as e:
        logger.warning(f"ip6tables command failed: {' '.join(cmd)}: {e.stderr}")
        raise


def ensure_chain_exists():
    """Ensure the tracking chain exists"""
    # Check if chain exists for IPv4
    result = _run_iptables(["-L", CHAIN_NAME], check=False)
    if result.returncode != 0:
        # Chain doesn't exist, create it
        try:
            _run_iptables(["-N", CHAIN_NAME])
            logger.info(f"Created iptables chain {CHAIN_NAME}")
            # Insert rule to jump to chain from INPUT and OUTPUT (only if not already there)
            input_check = _run_iptables(["-C", "INPUT", "-j", CHAIN_NAME], check=False)
            if input_check.returncode != 0:
                _run_iptables(["-I", "INPUT", "-j", CHAIN_NAME], check=False)
            output_check = _run_iptables(["-C", "OUTPUT", "-j", CHAIN_NAME], check=False)
            if output_check.returncode != 0:
                _run_iptables(["-I", "OUTPUT", "-j", CHAIN_NAME], check=False)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create iptables chain {CHAIN_NAME}: {e.stderr if hasattr(e, 'stderr') else str(e)}")
            raise
    
    # Same for IPv6
    result = _run_ip6tables(["-L", CHAIN_NAME], check=False)
    if result.returncode != 0:
        try:
            _run_ip6tables(["-N", CHAIN_NAME])
            logger.info(f"Created ip6tables chain {CHAIN_NAME}")
            input_check = _run_ip6tables(["-C", "INPUT", "-j", CHAIN_NAME], check=False)
            if input_check.returncode != 0:
                _run_ip6tables(["-I", "INPUT", "-j", CHAIN_NAME], check=False)
            output_check = _run_ip6tables(["-C", "OUTPUT", "-j", CHAIN_NAME], check=False)
            if output_check.returncode != 0:
                _run_ip6tables(["-I", "OUTPUT", "-j", CHAIN_NAME], check=False)
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to create ip6tables chain {CHAIN_NAME}: {e.stderr if hasattr(e, 'stderr') else str(e)}")
            # IPv6 might not be available, that's okay


def add_tracking_rule(tunnel_id: str, port: int, is_ipv6: bool = False):
    """
    Add iptables rule to track traffic on a local listen port (for GOST)
    The rule only COUNTS traffic, doesn't block or modify it
    For local listen ports, we track INPUT (traffic coming TO the port)
    """
    ensure_chain_exists()
    
    rule_comment = f"smite-{tunnel_id}"
    cmd = is_ipv6 and _run_ip6tables or _run_iptables
    
    try:
        # Check if rule already exists
        result = cmd(["-L", CHAIN_NAME, "-n", "-v", "--line-numbers"], check=False)
        if rule_comment in result.stdout:
            logger.debug(f"Tracking rule for tunnel {tunnel_id} port {port} already exists")
            return
        
        # Add TCP INPUT rule (traffic coming TO this port)
        cmd([
            "-A", CHAIN_NAME,
            "-p", "tcp",
            "--dport", str(port),
            "-m", "comment", "--comment", f"{rule_comment}-tcp-in",
            "-j", "ACCEPT"
        ])
        
        # Add UDP INPUT rule
        cmd([
            "-A", CHAIN_NAME,
            "-p", "udp",
            "--dport", str(port),
            "-m", "comment", "--comment", f"{rule_comment}-udp-in",
            "-j", "ACCEPT"
        ])
        
        logger.info(f"Added iptables tracking rules for tunnel {tunnel_id} on port {port} (IPv6={is_ipv6}) - INPUT only")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to add tracking rule for tunnel {tunnel_id}: {e}")
        raise


def remove_tracking_rule(tunnel_id: str, port: int, is_ipv6: bool = False):
    """Remove iptables tracking rule"""
    rule_comment = f"smite-{tunnel_id}"
    cmd = is_ipv6 and _run_ip6tables or _run_iptables
    
    try:
        # Find and delete rules by comment
        result = cmd(["-L", CHAIN_NAME, "-n", "-v", "--line-numbers"], check=False)
        lines = result.stdout.split('\n')
        
        # Get line numbers of rules with this comment
        line_nums = []
        for i, line in enumerate(lines, 1):
            if rule_comment in line:
                # Extract line number (first field)
                match = re.match(r'^\s*(\d+)', line)
                if match:
                    line_nums.append(int(match.group(1)))
        
        # Delete in reverse order to maintain line numbers
        for line_num in sorted(line_nums, reverse=True):
            cmd(["-D", CHAIN_NAME, str(line_num)], check=False)
        
        if line_nums:
            logger.info(f"Removed iptables tracking rule for tunnel {tunnel_id} port {port} (IPv6={is_ipv6})")
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to remove tracking rule for tunnel {tunnel_id}: {e}")


def get_traffic_bytes(tunnel_id: str, port: int, is_ipv6: bool = False) -> int:
    """Get total bytes for a tunnel tracked by port"""
    rule_comment = f"smite-{tunnel_id}"
    cmd = is_ipv6 and _run_ip6tables or _run_iptables
    
    try:
        result = cmd(["-L", CHAIN_NAME, "-n", "-v", "-x"], check=False)
        total_bytes = 0
        found_rules = 0
        
        # Sum bytes from all rules matching our comment
        for line in result.stdout.split('\n'):
            if rule_comment in line:
                found_rules += 1
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        bytes_val = int(parts[1])
                        total_bytes += bytes_val
                        logger.debug(f"Found rule for {tunnel_id}: {bytes_val} bytes")
                    except (ValueError, IndexError) as e:
                        logger.warning(f"Failed to parse bytes from line: {line}: {e}")
        
        if found_rules == 0:
            logger.warning(f"No iptables rules found for tunnel {tunnel_id} (comment: {rule_comment})")
        else:
            logger.debug(f"Tunnel {tunnel_id}: Found {found_rules} rules, total {total_bytes} bytes")
        
        return total_bytes
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to read iptables counters for tunnel {tunnel_id}: {e}")
        return 0
    except Exception as e:
        logger.error(f"Unexpected error reading iptables for tunnel {tunnel_id}: {e}", exc_info=True)
        return 0

