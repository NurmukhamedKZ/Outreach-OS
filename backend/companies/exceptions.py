from core.errors import DomainError


class InvalidCityError(DomainError):
    """The city is not one 2GIS is collected in."""

    code = "errors.cityInvalid"
    status_code = 422


class InvalidRubricError(DomainError):
    """The rubric id is not a 2GIS id: digits only."""

    code = "errors.rubricInvalid"
    status_code = 422


class UnexpectedPageError(Exception):
    """A 2GIS page has no state or not the expected shape.

    Not a DomainError: the collection turns it into the segment's failure reason.
    """


class SubstitutedPageError(UnexpectedPageError):
    """2GIS answered the first list page with another page."""

    def __init__(self, current: int | None) -> None:
        super().__init__(f"page {current} instead of the first")
        self.current = current
