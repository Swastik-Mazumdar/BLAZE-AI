"""TLS/CA helpers.

Developer Python installs and PyInstaller bundles on macOS often ship without a
usable CA store, so :mod:`urllib` and Hugging Face downloads fail with
``CERTIFICATE_VERIFY_FAILED``. Blaze points OpenSSL, requests and curl at the
certifi bundle (which is collected into the app at build time) before any
network call.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import ssl
import sys
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

_CONFIGURED = False
_CERT_FILE = ""

_SYSTEM_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/usr/local/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl/cert.pem",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/opt/homebrew/etc/openssl/cert.pem",
)


def _certifi_bundle() -> str:
    try:
        import certifi

        path = Path(certifi.where())
        if path.is_file():
            return str(path)
    except Exception:
        pass
    return ""


def _system_bundle() -> str:
    if os.name == "nt":
        return ""
    for candidate in _SYSTEM_BUNDLES:
        if Path(candidate).is_file():
            return candidate
    return ""


def ensure_ca_bundle() -> str:
    """Point network stacks at a CA bundle and return its path.

    Existing user overrides (``SSL_CERT_FILE``) win. Returns an empty string
    when no bundle can be found, in which case Python's platform defaults are
    used.
    """
    global _CONFIGURED, _CERT_FILE
    if _CONFIGURED:
        return _CERT_FILE

    existing = os.environ.get("SSL_CERT_FILE", "")
    if existing and Path(existing).is_file():
        bundle = existing
    else:
        bundle = _certifi_bundle() or _system_bundle()

    if bundle:
        os.environ.setdefault("SSL_CERT_FILE", bundle)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
        os.environ.setdefault("CURL_CA_BUNDLE", bundle)
        if os.name == "nt":
            os.environ.setdefault("AWS_CA_BUNDLE", bundle)
    _CERT_FILE = bundle
    _CONFIGURED = True
    return bundle


def ssl_context() -> ssl.SSLContext:
    """Return a verified TLS context that works in developer and frozen apps."""
    bundle = ensure_ca_bundle()
    if bundle:
        return ssl.create_default_context(cafile=bundle)
    context = ssl.create_default_context()
    # Windows/macOS Python installers normally populate the platform store.
    try:
        context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    except Exception:
        pass
    return context


def friendly_network_error(exc: BaseException) -> str:
    """Return an actionable message for common HTTPS/download failures."""
    text = str(exc)
    lowered = text.lower()
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        bundle = ensure_ca_bundle()
        where = f" Using {bundle}." if bundle else ""
        return (
            "TLS certificate verification failed. Blaze could not reach a trusted "
            f"CA bundle.{where} Install certifi (pip install certifi) or set "
            "SSL_CERT_FILE to a valid cacert.pem, then retry."
        )
    if "name or service not known" in lowered or "nodename nor servname" in lowered:
        return "No internet connection: this device could not resolve huggingface.co."
    if "timed out" in lowered or "timeout" in lowered:
        return "The Hugging Face request timed out. Check the connection and retry."
    if "connection reset" in lowered or "connection aborted" in lowered:
        return "The connection to Hugging Face was interrupted. Retry the download."
    if "401" in text or "403" in text:
        return (
            "Hugging Face refused this model. It may be gated: sign in on "
            "huggingface.co, accept the model license, and set HF_TOKEN."
        )
    if "404" in text:
        return "That repository was not found on Hugging Face. Check the repo id."
    return f"Network error on {sys.platform}: {text}"


_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "instance-data",
    "metadata",
    "169.254.169.254",
    "255.255.255.255",
    "0.0.0.0",
    "ipv4-localhost",
    "ipv6-localhost",
}

_SEARCH_ENGINES = {
    "google": ("https://www.google.com/search?q={q}", "Google"),
    "duckduckgo": ("https://duckduckgo.com/?q={q}", "DuckDuckGo"),
    "ddg": ("https://duckduckgo.com/?q={q}", "DuckDuckGo"),
    "bing": ("https://www.bing.com/search?q={q}", "Bing"),
    "yahoo": ("https://search.yahoo.com/search?p={q}", "Yahoo"),
    "wikipedia": ("https://en.wikipedia.org/wiki/Special:Search?search={q}", "Wikipedia"),
    "wiki": ("https://en.wikipedia.org/wiki/Special:Search?search={q}", "Wikipedia"),
    "youtube": ("https://www.youtube.com/results?search_query={q}", "YouTube"),
    "github": ("https://github.com/search?q={q}", "GitHub"),
    "reddit": ("https://www.reddit.com/search/?q={q}", "Reddit"),
}


def web_search_url(query: str, engine: str = "google") -> tuple[str, str]:
    """Return a URL and display name for a web search query."""
    engine_key = (engine or "google").strip().lower()
    template, label = _SEARCH_ENGINES.get(engine_key, _SEARCH_ENGINES["google"])
    url = template.format(q=urllib.parse.quote_plus(query.strip()))
    return url, label


def is_safe_public_url(url: str) -> tuple[bool, str]:
    """Verify that a URL is a valid, public HTTP/HTTPS URL and not an SSRF or internal target."""
    text = (url or "").strip()
    if not text:
        return False, "No URL supplied."
    try:
        parsed = urllib.parse.urlparse(text)
    except Exception as exc:
        return False, f"Invalid URL: {exc}"
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return False, f"Unsupported scheme “{scheme}”. Only HTTP/HTTPS URLs are allowed."
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False, "Missing hostname in URL."
    if hostname in _BLOCKED_HOSTS or hostname.endswith(".localhost") or hostname.endswith(".local"):
        return False, f"Access to “{hostname}” is blocked for security (internal/local hostname)."
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        return False, f"Could not resolve host “{hostname}”: {exc}"
    if not addr_infos:
        return False, f"Could not resolve any network addresses for “{hostname}”."
    for _, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return False, f"Access to IP {ip_str} ({hostname}) is blocked for security (private or internal network address)."
        except ValueError:
            return False, f"Invalid resolved IP address: {ip_str}"
    return True, ""


def sanitize_download_filename(filename: str, default_name: str = "download.bin") -> str:
    """Sanitize a filename from a URL or header to prevent path traversal or special char attacks."""
    name = (filename or "").strip()
    name = urllib.parse.unquote(name)
    name = re.sub(r'[\r\n\0\\/:"*?<>|]+', "_", name)
    name = re.sub(r"\.\.+", ".", name)
    name = name.strip(". ")
    if not name or len(name) > 200:
        name = default_name
    return name


class _PublicOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Validate every redirect hop, not only the user supplied starting URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe, reason = is_safe_public_url(newurl)
        if not safe:
            raise urllib.error.HTTPError(
                req.full_url, code, f"Blocked unsafe redirect: {reason}", headers, fp
            )
        old_scheme = urllib.parse.urlsplit(req.full_url).scheme.lower()
        new_scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if old_scheme == "https" and new_scheme != "https":
            raise urllib.error.HTTPError(
                req.full_url, code, "Blocked HTTPS downgrade redirect", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _public_download_target(folder: Path, name: str) -> Path:
    """Choose a contained, currently unused name; never overwrite a file."""
    safe_name = sanitize_download_filename(name)
    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
    for index in range(10_000):
        candidate_name = safe_name if index == 0 else f"{stem}_{index}{suffix}"
        candidate = (folder / candidate_name).resolve()
        if not candidate.is_relative_to(folder.resolve()):
            raise ValueError("Destination path escapes the download directory.")
        if not candidate.exists():
            return candidate
    raise OSError("Could not choose an unused download filename.")


def _verify_response_peer(response) -> None:
    """Check the connected socket too, preventing DNS-rebinding SSRF races."""
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        raise OSError("Could not verify the remote server address; download blocked.")
    peer = sock.getpeername()
    try:
        address = ipaddress.ip_address(peer[0])
    except (ValueError, TypeError, IndexError) as exc:
        raise OSError("The remote server returned an invalid network address.") from exc
    if not address.is_global:
        raise OSError("The download connected to a private or reserved network address.")


def default_download_dir() -> Path:
    """Default location for downloads (~/Downloads/BlazeDownloads)."""
    target = Path.home() / "Downloads" / "BlazeDownloads"
    target.mkdir(parents=True, exist_ok=True)
    return target


def download_file_safely(
    url: str,
    destination_dir: Path | str | None = None,
    custom_filename: str = "",
    max_bytes: int = 5 * 1024 * 1024 * 1024,  # 5 GB safety cap
    timeout: int = 60,
    max_seconds: int = 30 * 60,
) -> tuple[bool, str, Path | None, int]:
    """Safely download a file from a public URL with SSRF protection and streaming size checks.

    Returns:
        (success: bool, message: str, saved_path: Path | None, bytes_downloaded: int)
    """
    safe, reason = is_safe_public_url(url)
    if not safe:
        return False, reason, None, 0
    if max_bytes <= 0 or timeout <= 0 or max_seconds <= 0:
        return False, "Download limits and timeout must be positive.", None, 0

    dest_folder = Path(destination_dir).expanduser() if destination_dir else default_download_dir()
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest_folder_resolved = dest_folder.resolve()

    # Determine filename
    if custom_filename:
        safe_name = sanitize_download_filename(custom_filename)
    else:
        parsed = urllib.parse.urlparse(url)
        path_name = Path(parsed.path).name
        safe_name = sanitize_download_filename(path_name, default_name=f"blaze_download_{int(socket.gettime()) if hasattr(socket, 'gettime') else 1}.bin")

    try:
        target_path = _public_download_target(dest_folder_resolved, safe_name)
    except (OSError, ValueError) as exc:
        return False, str(exc), None, 0
    temp_path: Path | None = None
    ctx = ssl_context()
    headers = {"User-Agent": "BlazeAI/1.1 (Desktop Assistant; safe download client)"}
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx), _PublicOnlyRedirect()
    )

    downloaded = 0
    deadline = time.monotonic() + max_seconds
    try:
        with opener.open(request, timeout=timeout) as response:
            _verify_response_peer(response)
            # Check content-length header if provided
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    total_expected = int(content_length)
                    if total_expected > max_bytes:
                        return False, f"File size ({total_expected} bytes) exceeds maximum limit ({max_bytes} bytes).", None, 0
                except (ValueError, TypeError):
                    pass

            # Check Content-Disposition if safe_name was generic
            if not custom_filename and (safe_name.startswith("blaze_download") or not ext):
                disp = response.headers.get("Content-Disposition", "")
                disp_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', disp, re.I)
                if disp_match:
                    new_name = sanitize_download_filename(disp_match.group(1))
                    if new_name and new_name != safe_name:
                        target_path = _public_download_target(dest_folder_resolved, new_name)
                        safe_name = new_name

            fd, temp_name = tempfile.mkstemp(
                prefix=".blaze-download-", suffix=".part", dir=dest_folder_resolved
            )
            temp_path = Path(temp_name)
            with os.fdopen(fd, "wb") as out_file:
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Download exceeded its maximum duration.")
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        out_file.close()
                        temp_path.unlink(missing_ok=True)
                        return False, f"Download aborted: file exceeded maximum size ({max_bytes} bytes).", None, downloaded
                    out_file.write(chunk)

        # A hard link publishes the complete file without replacing a target
        # created after the earlier availability check (no clobber race).
        for _attempt in range(100):
            try:
                os.link(temp_path, target_path)
                break
            except FileExistsError:
                target_path = _public_download_target(dest_folder_resolved, safe_name)
        else:
            raise OSError("Could not publish the download without overwriting a file.")
        temp_path.unlink()
        return True, "Download completed successfully.", target_path, downloaded
    except Exception as exc:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return False, friendly_network_error(exc), None, downloaded

