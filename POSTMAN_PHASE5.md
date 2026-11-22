# Phase 5 Postman examples

Authenticate in Postman by first calling the sign-in endpoint and retaining the
session cookie. Include `X-CSRF-Token` on every POST request.

## Retrieve approved knowledge cases

**Request**

```http
POST /api/v1/projects/project-alpha/knowledge/retrieve
Content-Type: application/json
X-CSRF-Token: <csrf-token>
```

```json
{
  "text": "Login returns 500 after the authentication timeout"
}
```

**Success response — 200**

```json
{
  "cases": [
    {
      "id": "66e2b...",
      "defect_id": 104,
      "title": "Login timeout",
      "final": {
        "root_cause": {"code": "CONFIGURATION", "label": "Configuration"},
        "rationale": "Session timeout configuration was not propagated"
      },
      "score": 0.812345
    }
  ]
}
```

## Approve an RCA

**Request**

```http
POST /api/v1/projects/project-alpha/rcas/<rca-id>/review
Content-Type: application/json
X-CSRF-Token: <csrf-token>
```

```json
{
  "action": "APPROVE"
}
```

**Success response — 200**

```json
{
  "id": "<rca-id>",
  "status": "APPROVED",
  "project_id": "project-alpha",
  "defect": {"ado_id": 104},
  "final": {
    "root_cause": {"code": "CONFIGURATION", "label": "Configuration"},
    "sdlc_phase": {"code": "DEPLOYMENT", "label": "Deployment"},
    "testing_gap": {"code": "CONFIGURATION", "label": "Configuration"}
  }
}
```

## Correct an RCA

**Request**

```json
{
  "action": "CORRECT",
  "correction": {
    "root_cause_code": "DATA",
    "sdlc_phase_code": "INTEGRATION",
    "testing_gap_code": "TEST_DATA_GAP",
    "rationale": "The integration used stale reference data.",
    "confidence": 0.95,
    "other_proposal": null
  }
}
```

**Success response — 200**

```json
{
  "id": "<rca-id>",
  "status": "CORRECTED",
  "project_id": "project-alpha",
  "final": {
    "root_cause": {"code": "DATA", "label": "Data"},
    "sdlc_phase": {"code": "INTEGRATION", "label": "Integration"},
    "testing_gap": {"code": "TEST_DATA_GAP", "label": "Test data gap"}
  }
}
```

Taxonomy codes in the correction must exist in the project taxonomy.

## Reopen an RCA

**Request**

```http
POST /api/v1/projects/project-alpha/rcas/<rca-id>/reopen
Content-Type: application/json
X-CSRF-Token: <csrf-token>
```

```json
{}
```

**Success response — 200**

```json
{
  "id": "<rca-id>",
  "status": "AI_PROPOSED",
  "project_id": "project-alpha",
  "final": null
}
```
