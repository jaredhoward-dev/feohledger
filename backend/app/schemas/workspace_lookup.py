from pydantic import BaseModel, Field


class WorkspaceLookupRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    # hCaptcha response token from the widget (required when the deployment
    # sets `FEOH_HCAPTCHA_SECRET`, exactly like `/signup/start`).
    captcha_token: str | None = None
    # Email-copy language when the account has no saved preference: the form
    # sends the visitor's UI locale. Unsupported / absent falls back to English.
    locale: str | None = Field(default=None, max_length=16)


class WorkspaceLookupResponse(BaseModel):
    # One answer for every address, known or not (decisions §518). English, for
    # API clients; the page states its own confirmation in the visitor's
    # language and does not render this.
    status: str = "accepted"
    message: str = (
        "If that address has an account, we've emailed it a link to its workspace's sign-in page."
    )
