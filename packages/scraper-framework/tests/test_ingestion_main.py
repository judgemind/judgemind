"""Tests for the ingestion __main__.py entrypoint — error handling and exit codes.

Verifies that InfrastructureError and unhandled exceptions are logged via
structlog before the process exits with non-zero status.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import psycopg
import pytest

from ingestion.worker import InfrastructureError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_env() -> dict[str, str]:
    """Return minimal environment variables required by main()."""
    return {
        "DATABASE_URL": "postgresql://localhost/test",
        "REDIS_URL": "redis://localhost:6379",
        "OPENSEARCH_URL": "https://localhost:9200",
        "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
    }


# ---------------------------------------------------------------------------
# main() error handling tests
# ---------------------------------------------------------------------------


@patch("ingestion.__main__.IngestionWorker")
@patch("ingestion.__main__.make_s3_client")
@patch("ingestion.__main__.make_opensearch_client")
@patch("ingestion.__main__.redis.Redis")
@patch.dict("os.environ", _make_env(), clear=False)
def test_main_logs_infrastructure_error_and_exits(
    mock_redis_cls: MagicMock,
    mock_opensearch_cls: MagicMock,
    mock_make_s3: MagicMock,
    mock_worker_cls: MagicMock,
) -> None:
    """InfrastructureError from worker.run() is logged via structlog and exits with code 1."""
    from ingestion.__main__ import main

    cause = psycopg.OperationalError("connection refused")
    infra_err = InfrastructureError(cause)
    mock_worker = MagicMock()
    mock_worker.run.side_effect = infra_err
    mock_worker_cls.return_value = mock_worker

    mock_redis_instance = MagicMock()
    mock_redis_cls.from_url.return_value = mock_redis_instance

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1


@patch("ingestion.__main__.IngestionWorker")
@patch("ingestion.__main__.make_s3_client")
@patch("ingestion.__main__.make_opensearch_client")
@patch("ingestion.__main__.redis.Redis")
@patch.dict("os.environ", _make_env(), clear=False)
def test_main_logs_unhandled_exception_and_exits(
    mock_redis_cls: MagicMock,
    mock_opensearch_cls: MagicMock,
    mock_make_s3: MagicMock,
    mock_worker_cls: MagicMock,
) -> None:
    """Unhandled exceptions from worker.run() are logged and exit with code 1."""
    from ingestion.__main__ import main

    mock_worker = MagicMock()
    mock_worker.run.side_effect = RuntimeError("unexpected failure")
    mock_worker_cls.return_value = mock_worker

    mock_redis_instance = MagicMock()
    mock_redis_cls.from_url.return_value = mock_redis_instance

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1


@patch("ingestion.__main__.IngestionWorker")
@patch("ingestion.__main__.make_s3_client")
@patch("ingestion.__main__.make_opensearch_client")
@patch("ingestion.__main__.redis.Redis")
@patch.dict("os.environ", _make_env(), clear=False)
def test_main_infrastructure_error_includes_cause_in_log(
    mock_redis_cls: MagicMock,
    mock_opensearch_cls: MagicMock,
    mock_make_s3: MagicMock,
    mock_worker_cls: MagicMock,
) -> None:
    """The logged InfrastructureError includes the underlying cause."""
    from ingestion.__main__ import logger, main

    cause = psycopg.OperationalError("relation does not exist")
    infra_err = InfrastructureError(cause)
    mock_worker = MagicMock()
    mock_worker.run.side_effect = infra_err
    mock_worker_cls.return_value = mock_worker

    mock_redis_instance = MagicMock()
    mock_redis_cls.from_url.return_value = mock_redis_instance

    with patch.object(logger, "critical") as mock_log:
        with pytest.raises(SystemExit):
            main()

        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        # The first positional arg is the message
        assert "Infrastructure error" in call_kwargs[0][0]
        # Keyword args should include cause
        assert call_kwargs[1]["cause"] == "relation does not exist"
        assert "relation does not exist" in call_kwargs[1]["error"]


@patch("ingestion.__main__.IngestionWorker")
@patch("ingestion.__main__.make_s3_client")
@patch("ingestion.__main__.make_opensearch_client")
@patch("ingestion.__main__.redis.Redis")
@patch.dict("os.environ", _make_env(), clear=False)
def test_main_normal_run_does_not_exit(
    mock_redis_cls: MagicMock,
    mock_opensearch_cls: MagicMock,
    mock_make_s3: MagicMock,
    mock_worker_cls: MagicMock,
) -> None:
    """When worker.run() returns normally, main() does not call sys.exit."""
    from ingestion.__main__ import main

    mock_worker = MagicMock()
    mock_worker.run.return_value = None  # normal return
    mock_worker_cls.return_value = mock_worker

    mock_redis_instance = MagicMock()
    mock_redis_cls.from_url.return_value = mock_redis_instance

    # Should not raise SystemExit
    main()

    mock_worker.run.assert_called_once()


@patch("ingestion.__main__.IngestionWorker")
@patch("ingestion.__main__.make_s3_client")
@patch("ingestion.__main__.make_opensearch_client")
@patch("ingestion.__main__.redis.Redis")
@patch.dict("os.environ", _make_env(), clear=False)
def test_main_redis_connection_failure_exits(
    mock_redis_cls: MagicMock,
    mock_opensearch_cls: MagicMock,
    mock_make_s3: MagicMock,
    mock_worker_cls: MagicMock,
) -> None:
    """Startup-time Redis connection failure is logged and exits with code 1."""
    from ingestion.__main__ import main

    mock_redis_instance = MagicMock()
    mock_redis_instance.ping.side_effect = ConnectionError("Connection refused")
    mock_redis_cls.from_url.return_value = mock_redis_instance

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    # worker.run() should never be reached
    mock_worker_cls.return_value.run.assert_not_called()
