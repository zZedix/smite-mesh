"""Utility functions for address parsing and validation"""
import ipaddress
import re
from typing import Tuple, Optional


def parse_address_port(address_str: str) -> Tuple[str, Optional[int], bool]:
    """
    Parse an address:port string, handling both IPv4 and IPv6 addresses.
    
    Supports formats:
    - IPv4: "127.0.0.1:8080" -> ("127.0.0.1", 8080, False)
    - IPv6: "[2001:db8::1]:8080" -> ("2001:db8::1", 8080, True)
    - IPv6: "2001:db8::1" -> ("2001:db8::1", None, True)
    - Hostname: "example.com:8080" -> ("example.com", 8080, False)
    
    Args:
        address_str: Address string in format "host:port" or "[ipv6]:port"
        
    Returns:
        Tuple of (host, port, is_ipv6) where port is None if not specified
    """
    if not address_str:
        return ("", None, False)
    
    address_str = address_str.strip()
    
    # Check for IPv6 address in brackets: [2001:db8::1]:8080
    ipv6_bracket_match = re.match(r'^\[([^\]]+)\](?::(\d+))?$', address_str)
    if ipv6_bracket_match:
        host = ipv6_bracket_match.group(1)
        port_str = ipv6_bracket_match.group(2)
        port = int(port_str) if port_str else None
        return (host, port, True)
    
    # Check if it's a bare IPv6 address (no brackets, no port)
    # IPv6 addresses contain colons, so we need to check if it's a valid IPv6
    try:
        ipaddress.IPv6Address(address_str)
        # It's a valid IPv6 address without port
        return (address_str, None, True)
    except (ValueError, ipaddress.AddressValueError):
        pass
    
    # For IPv4 or hostname with port, split on last colon
    if ":" in address_str:
        # Try to split on last colon (handles IPv6 addresses that might have been passed without brackets)
        parts = address_str.rsplit(":", 1)
        if len(parts) == 2:
            host_part = parts[0]
            port_str = parts[1]
            
            # Check if host_part is actually an IPv6 address
            try:
                ipaddress.IPv6Address(host_part)
                # It's an IPv6 address, return as-is with port
                return (host_part, int(port_str), True)
            except (ValueError, ipaddress.AddressValueError):
                # It's IPv4 or hostname
                try:
                    port = int(port_str)
                    return (host_part, port, False)
                except ValueError:
                    # Port is not a number, treat entire string as host
                    return (address_str, None, False)
    
    # No port specified
    return (address_str, None, False)


def format_address_port(host: str, port: Optional[int] = None) -> str:
    """
    Format host and port into address:port string, handling IPv6 addresses.
    
    Args:
        host: Host address (IPv4, IPv6, or hostname)
        port: Port number (optional)
        
    Returns:
        Formatted string: "host:port" or "[ipv6]:port" or "host"
    """
    if not host:
        return ""
    
    # Check if host is an IPv6 address
    try:
        ipaddress.IPv6Address(host)
        # IPv6 address needs brackets if port is specified
        if port is not None:
            return f"[{host}]:{port}"
        return host
    except (ValueError, ipaddress.AddressValueError):
        # IPv4 or hostname
        if port is not None:
            return f"{host}:{port}"
        return host


def is_valid_ip_address(address: str) -> bool:
    """
    Check if a string is a valid IP address (IPv4 or IPv6).
    
    Args:
        address: String to validate
        
    Returns:
        True if valid IP address, False otherwise
    """
    try:
        ipaddress.ip_address(address)
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False


def is_valid_ipv6_address(address: str) -> bool:
    """
    Check if a string is a valid IPv6 address.
    
    Args:
        address: String to validate
        
    Returns:
        True if valid IPv6 address, False otherwise
    """
    try:
        ipaddress.IPv6Address(address)
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False

