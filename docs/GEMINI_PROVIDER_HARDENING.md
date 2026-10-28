# Gemini Provider Hardening

## Immediate security action

A previous live-test log displayed the Gemini API key inside the request URL.
Treat that key as exposed and rotate/revoke it before continuing.

The provider now sends the key in the `x-goog-api-key` header and never includes
it in the URL or error messages.

## Configuration

```bat
set USE_MOCK_AI=false
set AI_PROVIDER=gemini
set GEMINI_MODEL=gemini-3.6-flash
set GEMINI_API_VERSION=v1
```

Verify without printing the key:

```bat
if defined GEMINI_API_KEY (echo GEMINI_API_KEY is configured) else (echo GEMINI_API_KEY is missing)
```

## Why the 503 happened

HTTP 503 means the Gemini service returned a temporary service-unavailable
response. It is not an authentication failure. The provider now retries
transient 429/5xx responses while avoiding secret leakage.

If a model is unavailable, the provider returns an actionable model/version
error instead of exposing the request URL.
