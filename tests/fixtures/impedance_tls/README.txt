These certificates and the unencrypted server key are PUBLIC TEST FIXTURES.
They must never be used outside loopback tests. The CA private key is not kept.

The server certificate has only the DNS:localhost subject alternative name.
It deliberately does not cover 127.0.0.1, allowing hostname-verification tests.
The CA's OpenSSL subject hash is 7ff866ca, used by the hashed-directory test.
Certificates have a long validity period to avoid time-dependent test failures.
The v3 CA has critical CA:TRUE basic constraints, critical keyCertSign/cRLSign
key usage, and a subject key identifier. The server has a matching authority
key identifier, CA:FALSE, serverAuth, and digitalSignature/keyEncipherment usage.
The chain passes OpenSSL's -x509_strict validation. The loopback regressions
explicitly enable VERIFY_X509_STRICT on older Python versions too, matching the
stricter default verification used by Python 3.13 and later.
Runtime TLS uses Python's standard ssl library and the shipped WebSocket client;
no external process, provider service, or additional Python dependency is needed.
