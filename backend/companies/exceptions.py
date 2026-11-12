from core.errors import DomainError


class InvalidCityError(DomainError):
    """The city is not one 2GIS is collected in."""

    code = "errors.cityInvalid"
    status_code = 422


class InvalidRubricError(DomainError):
    """The rubric id is not a 2GIS id: digits only."""

    code = "errors.rubricInvalid"
    status_code = 422
