import pytest
from fastapi import Body, FastAPI
from fastapi.testclient import TestClient

from utils.http_security import RequestBodyLimitMiddleware


def _test_app(limit: int = 32) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=limit)

    @app.post("/echo")
    async def echo(payload: bytes = Body()):
        return {"size": len(payload)}

    return app


def test_request_body_within_limit_is_accepted():
    with TestClient(_test_app()) as client:
        response = client.post(
            "/echo",
            content=b"safe payload",
            headers={"content-type": "application/octet-stream"},
        )

    assert response.status_code == 200
    assert response.json() == {"size": 12}


def test_content_length_over_limit_is_rejected_before_parsing():
    with TestClient(_test_app()) as client:
        response = client.post(
            "/echo",
            content=b"x" * 33,
            headers={"content-type": "application/octet-stream"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


def test_streamed_body_over_limit_is_rejected_without_content_length():
    def chunks():
        yield b"x" * 20
        yield b"y" * 20

    with TestClient(_test_app()) as client:
        response = client.post(
            "/echo",
            content=chunks(),
            headers={"content-type": "application/octet-stream"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


def test_body_limit_must_be_positive():
    with pytest.raises(ValueError, match="max_body_size must be at least 1"):
        RequestBodyLimitMiddleware(object(), max_body_size=0)


def test_main_application_installs_the_global_body_limit():
    from main import app

    configured_limits = [
        middleware.kwargs["max_body_size"]
        for middleware in app.user_middleware
        if middleware.cls is RequestBodyLimitMiddleware
    ]

    assert configured_limits == [64 * 1024]
