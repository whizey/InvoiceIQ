"""A large client body must be stopped before API parsing or model work."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.request_limits import (
    MAX_VOICE_TURN_BODY_BYTES,
    MAX_WRITE_BODY_BYTES,
    RequestBodyLimitMiddleware,
)


def test_write_body_limit_and_normal_request():
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)

    @app.post("/echo")
    async def echo():
        return {"accepted": True}

    with TestClient(app) as client:
        assert client.post("/echo", content=b"ok").json() == {"accepted": True}
        response = client.post("/echo", content=b"x" * (MAX_WRITE_BODY_BYTES + 1))
        assert response.status_code == 413
        assert response.json() == {"detail": "Request body too large"}
        streamed = client.post(
            "/echo",
            content=iter((b"x" * MAX_WRITE_BODY_BYTES, b"x")),
        )
        assert streamed.status_code == 413


def test_voice_turn_has_bounded_audio_allowance():
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)

    @app.post("/recovery-cases/example/voice/turn")
    async def voice_turn():
        return {"accepted": True}

    with TestClient(app) as client:
        path = "/recovery-cases/example/voice/turn"
        assert client.post(path, content=b"x" * (MAX_WRITE_BODY_BYTES + 1)).status_code == 200
        assert client.post(path, content=b"x" * (MAX_VOICE_TURN_BODY_BYTES + 1)).status_code == 413


def test_real_ml_endpoint_stops_large_body_before_validation():
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/ml/payment-risk/predict", content=b"x" * (MAX_WRITE_BODY_BYTES + 1))
    assert response.status_code == 413
