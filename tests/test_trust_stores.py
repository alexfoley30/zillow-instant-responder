"""eMudhra CA key compromise (2026-10-17, CVE-2026-111943): no trust store this
service ships may still trust the root behind the compromised intermediates.
Checks the real installed bundles, so a downgrade of certifi or grpcio fails
here instead of shipping."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import certifi  # noqa: E402
import grpc  # noqa: E402

import gmail_client  # noqa: E402

DISTRUSTED = "emSign ECC Root CA - G3"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_certifi_bundle_drops_emudhra_g3():
    assert DISTRUSTED not in _read(certifi.where())


def test_grpc_bundled_roots_drop_emudhra_g3():
    roots = os.path.join(os.path.dirname(grpc.__file__),
                         "_cython", "_credentials", "roots.pem")
    assert DISTRUSTED not in _read(roots)


def test_outbound_calls_verify_against_certifi(monkeypatch):
    seen = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"successful": true, "success": true}'

    def fake_urlopen(req, timeout=None, context=None):
        seen.append(context)
        return _Resp()

    monkeypatch.setattr(gmail_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gmail_client, "POKE_API_KEY", "")
    monkeypatch.setattr(gmail_client, "POKE_ENDPOINT", "https://example.invalid/p")
    gmail_client.composio_execute("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", {})
    gmail_client.poke_ping("hi")
    assert seen and all(c is gmail_client.TLS_CONTEXT for c in seen)
    assert gmail_client.TLS_CONTEXT.verify_mode.name == "CERT_REQUIRED"
    assert gmail_client.TLS_CONTEXT.check_hostname
