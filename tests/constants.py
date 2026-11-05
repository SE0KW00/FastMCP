"""Values shared by more than one test module."""

from __future__ import annotations

BASE_URL = "http://backend.test"

#: Credential headers a caller is expected to send with every request.
AUTHORIZATION = "Bearer caller-token"
API_KEY = "caller-api-key"
CREDENTIAL_HEADERS = {"authorization": AUTHORIZATION, "x-api-key": API_KEY}
