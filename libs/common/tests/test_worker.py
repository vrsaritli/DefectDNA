from libs.common.service_auth import verify_service_token


def test_worker_service_identity_token_rejected_after_expiry() -> None:
    from libs.common.service_auth import create_service_token

    token = create_service_token("test-secret", "worker", ttl_seconds=-1)
    assert verify_service_token(token, "test-secret") is None
