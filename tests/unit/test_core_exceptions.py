"""Unit tests for core exceptions."""

from imednet import errors


def test_api_error_str_includes_details() -> None:
    """Test that api error str includes details."""
    err = errors.ApiError({"msg": "bad"}, status_code=400)
    text = str(err)
    assert "Status Code: 400" in text
    assert "msg" in text


def test_exception_hierarchy() -> None:
    """Test that exception hierarchy."""
    assert issubclass(errors.AuthenticationError, errors.ApiError)
    assert issubclass(errors.AuthorizationError, errors.ApiError)
    assert issubclass(errors.NotFoundError, errors.ApiError)
    assert issubclass(errors.RateLimitError, errors.ApiError)
    assert issubclass(errors.ServerError, errors.ApiError)
    assert issubclass(errors.ValidationError, errors.ApiError)

    # New check
    assert issubclass(errors.ClientError, errors.ImednetError)
    assert issubclass(errors.ValidationError, errors.ClientError)
    assert issubclass(errors.ValidationError, errors.ApiError)
    assert issubclass(errors.UnknownVariableTypeError, errors.ClientError)
    assert issubclass(errors.UnknownVariableTypeError, errors.ApiError)
    assert issubclass(errors.BadRequestError, errors.ValidationError)
    assert issubclass(errors.PathTraversalValidationError, errors.ClientError)
    assert issubclass(errors.OrchestratorError, errors.ImednetError)
    assert issubclass(errors.FilterConflictError, errors.OrchestratorError)


def test_filter_conflict_error_keeps_conflicting_filters() -> None:
    """Test that filter conflict error keeps conflicting filters."""
    whitelist = {"STUDY-A"}
    blacklist = {"STUDY-B"}

    err = errors.FilterConflictError(whitelist=whitelist, blacklist=blacklist)

    assert err.whitelist == whitelist
    assert err.blacklist == blacklist
    assert "mutually exclusive" in str(err)
