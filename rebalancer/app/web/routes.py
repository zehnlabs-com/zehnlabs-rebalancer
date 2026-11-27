"""Thin HTML route adapters for :class:`RebalancerApplication`."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ..accounts import AccountConfig
from ..application import RebalancerApplication
from ..results import RunOutcome, RunSource, WorkAction
from ..services.account_manager import ReadinessError
from ..services.work_manager import UnknownAccountError
from ..state.models import WorkSource
from ..state.sqlite import OutstandingWorkError, StateError
from .auth import AuthenticationService, SessionContext


RUNS_PAGE_SIZE = 50


@dataclass(frozen=True)
class AccountFormRow:
    account_id: str
    selected: bool
    enabled: bool
    strategy_name: str
    replacement_set: Optional[str]
    discovered: bool


@dataclass(frozen=True)
class SubmittedAccountForm:
    rows: tuple[AccountFormRow, ...]
    accounts: tuple[AccountConfig, ...]
    field_errors: dict[str, dict[str, str]]
    error_summary: tuple[str, ...]


def build_router(
    application: RebalancerApplication,
    auth: AuthenticationService,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter()

    def page(
        request: Request,
        name: str,
        context: dict[str, Any],
        session: SessionContext,
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        response = templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "csrf_token": session.csrf_token,
                "mode": application.settings.trading_mode.upper(),
                **context,
            },
            status_code=status_code,
        )
        auth.set_cookie(response, session, request)
        return response

    def authenticated(request: Request) -> Optional[SessionContext]:
        return auth.get_session(request, require_authenticated=True)

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    def forbidden() -> HTMLResponse:
        return HTMLResponse("Request rejected.", status_code=403)

    def work_conflict(
        request: Request,
        session: SessionContext,
        error: OutstandingWorkError,
    ) -> HTMLResponse:
        return page(
            request,
            "error.html",
            {
                "message": (
                    "This Account already has outstanding work for that action."
                ),
                "link_href": f"/work/{error.existing.work_id}",
                "link_text": "View existing Work Item",
            },
            session,
            status_code=409,
        )

    async def submitted_accounts(request: Request) -> SubmittedAccountForm:
        form = await request.form()
        selected = {str(value) for value in form.getlist("selected_account_id")}
        ids = selected | {
            str(key)[len("strategy_") :]
            for key in form.keys()
            if str(key).startswith("strategy_")
        }
        rows: list[AccountFormRow] = []
        accounts: list[AccountConfig] = []
        field_errors: dict[str, dict[str, str]] = {}
        summary: list[str] = []
        labels = {
            "account_id": "Account",
            "strategy_name": "Strategy",
            "replacement_set": "Replacement Set",
            "enabled": "Enabled",
        }
        for account_id in sorted(ids):
            strategy = str(form.get(f"strategy_{account_id}", ""))
            replacement = str(form.get(f"replacement_{account_id}", "")) or None
            row = AccountFormRow(
                account_id=account_id,
                selected=account_id in selected,
                enabled=form.get(f"enabled_{account_id}") == "on",
                strategy_name=strategy,
                replacement_set=replacement,
                discovered=False,
            )
            rows.append(row)
            if not row.selected:
                continue
            try:
                accounts.append(
                    AccountConfig(
                        account_id=row.account_id,
                        type=application.settings.trading_mode,
                        enabled=row.enabled,
                        strategy_name=row.strategy_name,
                        replacement_set=row.replacement_set,
                    )
                )
            except ValidationError as exc:
                errors = field_errors.setdefault(account_id, {})
                for detail in exc.errors(include_url=False):
                    field = str(detail["loc"][0]) if detail.get("loc") else "account_id"
                    message = str(detail.get("msg") or "Invalid value.")
                    errors[field] = message
                    summary.append(
                        f"Account {account_id or '(blank)'} — {labels.get(field, field)}: {message}"
                    )
        return SubmittedAccountForm(
            rows=tuple(rows),
            accounts=tuple(accounts),
            field_errors=field_errors,
            error_summary=tuple(summary),
        )

    def account_context(
        *,
        discovery=None,
        error: Optional[str] = None,
        message: Optional[str] = None,
        onboarding: bool = False,
        rows_override: Optional[tuple[AccountFormRow, ...]] = None,
        field_errors: Optional[dict[str, dict[str, str]]] = None,
        error_summary: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        stored = {account.account_id: account for account in application.list_accounts()}
        discovered = set(discovery.accounts) if discovery is not None else set()
        rows = list(rows_override or ())
        if rows_override is None:
            for account_id in sorted(set(stored) | discovered):
                account = stored.get(account_id)
                rows.append(
                    AccountFormRow(
                        account_id=account_id,
                        selected=account is not None,
                        enabled=account.enabled if account is not None else True,
                        strategy_name=account.strategy_name if account is not None else "",
                        replacement_set=account.replacement_set if account is not None else None,
                        discovered=account_id in discovered,
                    )
                )
        return {
            "rows": rows,
            "discovery": discovery,
            "error": error,
            "message": message,
            "onboarding": onboarding,
            "field_errors": field_errors or {},
            "error_summary": error_summary,
            "initial_confirmations": (
                getattr(application, "onboarding_confirmations", lambda: [])()
                if onboarding
                else []
            ),
            "live_ack_required": (
                onboarding and application.settings.trading_mode == "live"
            ),
            "save_action": "/onboarding/save" if onboarding else "/accounts/save",
            "preview_token": secrets.token_urlsafe(32),
            "finish_token": secrets.token_urlsafe(32),
        }

    @router.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        existing = authenticated(request)
        if existing is not None:
            return RedirectResponse("/", status_code=303)
        session = auth.get_session(request, ensure=True)
        assert session is not None
        return page(request, "login.html", {"error": None}, session)

    @router.post("/login", response_class=HTMLResponse)
    async def login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        csrf_token: str = Form(""),
    ) -> Response:
        session = auth.validate_anonymous_post(request, csrf_token)
        if session is None:
            return forbidden()
        logged_in, status = auth.login(
            session,
            username=username,
            password=password,
            client_key=auth.client_key(request),
        )
        if status == 429:
            response = page(
                request,
                "login.html",
                {"error": "Too many login attempts. Try again later."},
                session,
                status_code=429,
            )
            response.headers["Retry-After"] = "900"
            return response
        if logged_in is None:
            return page(
                request,
                "login.html",
                {"error": "Invalid username or password."},
                session,
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        auth.set_cookie(response, logged_in, request)
        return response

    @router.post("/logout")
    async def logout(request: Request, csrf_token: str = Form("")) -> Response:
        session = auth.require_post(request, csrf_token)
        if session is None:
            return forbidden()
        auth.logout(session)
        response = RedirectResponse("/login", status_code=303)
        auth.clear_cookie(response, request)
        return response

    @router.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        session = authenticated(request)
        if session is None:
            return login_redirect()
        status = await application.status()
        return page(request, "status.html", {"status": status}, session)

    @router.get("/onboarding", response_class=HTMLResponse)
    async def onboarding(request: Request) -> Response:
        session = authenticated(request)
        if session is None:
            return login_redirect()
        status = await application.status()
        return page(
            request,
            "onboarding.html",
            {
                "status": status,
                "accounts": application.list_accounts(),
                "discovery": None,
                "error": None,
                "preview_token": secrets.token_urlsafe(32),
                "finish_token": secrets.token_urlsafe(32),
                "initial_confirmations": getattr(
                    application, "onboarding_confirmations", lambda: []
                )(),
                "live_ack_required": application.settings.trading_mode == "live",
            },
            session,
        )

    @router.post("/onboarding/discover", response_class=HTMLResponse)
    @router.post("/accounts/discover", response_class=HTMLResponse)
    async def discover(request: Request, csrf_token: str = Form("")) -> Response:
        session = auth.require_post(request, csrf_token)
        if session is None:
            return forbidden()
        try:
            discovery = await application.discover()
            error = None
        except ReadinessError as exc:
            discovery = None
            error = str(exc)
        except Exception:
            discovery = None
            error = "Account or Strategy discovery is unavailable. Check Gateway authentication and Zehnlabs readiness."
        return page(
            request,
            "accounts.html",
            account_context(
                discovery=discovery,
                error=error,
                onboarding=request.url.path.startswith("/onboarding"),
            ),
            session,
            status_code=503 if error else 200,
        )

    @router.post("/onboarding/save", response_class=HTMLResponse)
    @router.post("/accounts/save", response_class=HTMLResponse)
    async def save_accounts(request: Request, csrf_token: str = Form("")) -> Response:
        session = auth.require_post(request, csrf_token)
        if session is None:
            return forbidden()
        submission = await submitted_accounts(request)
        if submission.field_errors:
            return page(
                request,
                "accounts.html",
                account_context(
                    error="Correct the highlighted Account fields and submit again.",
                    onboarding=request.url.path.startswith("/onboarding"),
                    rows_override=submission.rows,
                    field_errors=submission.field_errors,
                    error_summary=submission.error_summary,
                ),
                session,
                status_code=422,
            )
        try:
            result = await application.replace_accounts(submission.accounts)
        except (ValueError, ValidationError) as exc:
            field_errors: dict[str, dict[str, str]] = {}
            message = str(exc)
            relevant_field = None
            if "Strategy" in message or "strategy" in message:
                relevant_field = "strategy_name"
            elif "Replacement Set" in message or "replacement_set" in message:
                relevant_field = "replacement_set"
            elif "Discover managed" in message or "Account" in message:
                relevant_field = "account_id"
            if relevant_field is not None:
                for row in submission.rows:
                    if row.selected:
                        field_errors.setdefault(row.account_id, {})[relevant_field] = message
            return page(
                request,
                "accounts.html",
                account_context(
                    error=message,
                    onboarding=request.url.path.startswith("/onboarding"),
                    rows_override=submission.rows,
                    field_errors=field_errors,
                    error_summary=(message,),
                ),
                session,
                status_code=422,
            )
        return page(
            request,
            "accounts.html",
            account_context(
                error=result.degraded_error,
                message="Account configuration saved.",
                onboarding=request.url.path.startswith("/onboarding"),
            ),
            session,
        )

    @router.post("/onboarding/preview")
    async def preview_onboarding(
        request: Request,
        csrf_token: str = Form(""),
        request_token: str = Form(""),
    ) -> Response:
        session = auth.require_post(request, csrf_token)
        if session is None:
            return forbidden()
        try:
            receipt = application.preview_all(request_token)
        except OutstandingWorkError as exc:
            return work_conflict(request, session, exc)
        except (ValueError, StateError) as exc:
            return page(request, "error.html", {"message": str(exc)}, session, status_code=409)
        except Exception:
            return page(
                request,
                "error.html",
                {"message": "Preview work could not be created. Try again."},
                session,
                status_code=409,
            )
        return RedirectResponse(
            f"/work/submissions/{receipt.submission_id}", status_code=303
        )

    @router.post("/onboarding/finish")
    async def finish_onboarding(
        request: Request,
        csrf_token: str = Form(""),
        request_token: str = Form(""),
        live_acknowledgement: Optional[str] = Form(None),
    ) -> Response:
        session = auth.require_post(request, csrf_token)
        if session is None:
            return forbidden()
        if (
            application.settings.trading_mode == "live"
            and live_acknowledgement != "on"
        ):
            return page(
                request,
                "error.html",
                {
                    "message": (
                        "Finishing live onboarding requires the explicit "
                        "order-placement acknowledgement shown with the Account actions "
                        "and execution times."
                    )
                },
                session,
                status_code=422,
            )
        try:
            initialization = application.finish_onboarding(request_token)
        except OutstandingWorkError as exc:
            return work_conflict(request, session, exc)
        except (ValueError, StateError) as exc:
            return page(request, "error.html", {"message": str(exc)}, session, status_code=409)
        except Exception:
            return page(
                request,
                "error.html",
                {"message": "Initial rebalance work could not be created. Try again."},
                session,
                status_code=409,
            )
        return RedirectResponse(
            f"/work/submissions/{initialization.receipt.submission_id}",
            status_code=303,
        )

    @router.get("/accounts", response_class=HTMLResponse)
    async def accounts(request: Request) -> Response:
        session = authenticated(request)
        if session is None:
            return login_redirect()
        return page(
            request,
            "accounts.html",
            account_context(),
            session,
        )

    @router.post("/accounts/import", response_class=HTMLResponse)
    async def import_accounts(
        request: Request,
        csrf_token: str = Form(""),
        file: UploadFile = None,
    ) -> Response:
        session = auth.require_post(request, csrf_token)
        if session is None:
            return forbidden()
        try:
            if file is None:
                raise ValueError("Choose an Account YAML file.")
            document = (await file.read(1_000_001)).decode("utf-8")
            if len(document.encode("utf-8")) > 1_000_000:
                raise ValueError("Account import is too large.")
            await application.import_accounts(document)
        except (ValueError, ValidationError) as exc:
            return page(
                request,
                "accounts.html",
                account_context(error=str(exc)),
                session,
                status_code=422,
            )
        except Exception:
            return page(
                request,
                "accounts.html",
                {
                    **account_context(),
                    "error": "Account import could not be completed. Check the document and try again.",
                },
                session,
                status_code=422,
            )
        return RedirectResponse("/accounts", status_code=303)

    @router.get("/accounts/export")
    async def export_accounts(request: Request) -> Response:
        if authenticated(request) is None:
            return login_redirect()
        return PlainTextResponse(
            application.export_accounts(),
            media_type="application/yaml",
            headers={"Content-Disposition": 'attachment; filename="accounts.yaml"'},
        )

    async def confirmation(
        request: Request,
        account_id: str,
        action: WorkAction,
        schedule: bool,
    ) -> Response:
        session = authenticated(request)
        if session is None:
            return login_redirect()
        try:
            confirmation = application.work_confirmation(
                account_id,
                action,
                schedule_next_open=schedule,
            )
        except ValueError:
            return HTMLResponse("Not found.", status_code=404)
        except RuntimeError as exc:
            return page(
                request,
                "error.html",
                {"message": str(exc)},
                session,
                status_code=503,
            )
        return page(
            request,
            "confirm.html",
            {
                "confirmation": confirmation,
                "request_token": secrets.token_urlsafe(32),
            },
            session,
        )

    @router.get("/accounts/{account_id}/preview", response_class=HTMLResponse)
    async def preview_confirm(request: Request, account_id: str) -> Response:
        return await confirmation(request, account_id, WorkAction.PREVIEW, False)

    @router.get("/accounts/{account_id}/rebalance", response_class=HTMLResponse)
    async def rebalance_confirm(request: Request, account_id: str) -> Response:
        return await confirmation(request, account_id, WorkAction.REBALANCE, False)

    @router.get("/accounts/{account_id}/schedule", response_class=HTMLResponse)
    async def schedule_confirm(request: Request, account_id: str) -> Response:
        return await confirmation(request, account_id, WorkAction.REBALANCE, True)

    async def submit_account_action(
        request: Request,
        account_id: str,
        action: WorkAction,
        schedule: bool,
        csrf_token: str,
        request_token: str,
        live_acknowledgement: Optional[str],
    ) -> Response:
        session = auth.require_post(request, csrf_token)
        if session is None:
            return forbidden()
        if (
            application.settings.trading_mode == "live"
            and action == WorkAction.REBALANCE
            and live_acknowledgement != "on"
        ):
            return page(
                request,
                "error.html",
                {"message": "A live rebalance requires the unchecked order-placement acknowledgement."},
                session,
                status_code=422,
            )
        try:
            creation = application.submit_work(
                account_id,
                action,
                request_token,
                schedule_next_open=schedule,
            )
        except UnknownAccountError:
            return HTMLResponse("Not found.", status_code=404)
        except OutstandingWorkError as exc:
            return work_conflict(request, session, exc)
        except (ValueError, RuntimeError) as exc:
            return page(request, "error.html", {"message": str(exc)}, session, status_code=422)
        return RedirectResponse(f"/work/{creation.item.work_id}", status_code=303)

    @router.post("/accounts/{account_id}/preview")
    async def preview_submit(
        request: Request, account_id: str, csrf_token: str = Form(""), request_token: str = Form("")
    ) -> Response:
        return await submit_account_action(
            request, account_id, WorkAction.PREVIEW, False, csrf_token, request_token, None
        )

    @router.post("/accounts/{account_id}/rebalance")
    async def rebalance_submit(
        request: Request,
        account_id: str,
        csrf_token: str = Form(""),
        request_token: str = Form(""),
        live_acknowledgement: Optional[str] = Form(None),
    ) -> Response:
        return await submit_account_action(
            request, account_id, WorkAction.REBALANCE, False, csrf_token,
            request_token, live_acknowledgement
        )

    @router.post("/accounts/{account_id}/schedule")
    async def schedule_submit(
        request: Request,
        account_id: str,
        csrf_token: str = Form(""),
        request_token: str = Form(""),
        live_acknowledgement: Optional[str] = Form(None),
    ) -> Response:
        return await submit_account_action(
            request, account_id, WorkAction.REBALANCE, True, csrf_token,
            request_token, live_acknowledgement
        )

    @router.get("/work", response_class=HTMLResponse)
    async def work_list(request: Request) -> Response:
        session = authenticated(request)
        if session is None:
            return login_redirect()
        return page(request, "work.html", {"items": application.list_work()}, session)

    @router.get("/work/submissions/{submission_id}", response_class=HTMLResponse)
    async def work_submission(request: Request, submission_id: str) -> Response:
        session = authenticated(request)
        if session is None:
            return login_redirect()
        receipt = application.get_work_submission(submission_id)
        if receipt is None:
            return HTMLResponse("Not found.", status_code=404)
        return page(
            request,
            "work_submission.html",
            {"receipt": receipt},
            session,
        )

    @router.get("/work/{work_id}", response_class=HTMLResponse)
    async def work_detail(request: Request, work_id: str) -> Response:
        session = authenticated(request)
        if session is None:
            return login_redirect()
        item = application.get_work(work_id)
        if item is None:
            return HTMLResponse("Not found.", status_code=404)
        linked = application.get_run(item.run_id) if item.run_id else None
        return page(request, "work_detail.html", {"item": item, "run": linked}, session)

    @router.post("/work/{work_id}/cancel")
    async def cancel_work(
        request: Request, work_id: str, csrf_token: str = Form("")
    ) -> Response:
        session = auth.require_post(request, csrf_token)
        if session is None:
            return forbidden()
        item = application.cancel_work(work_id)
        if item is None:
            return HTMLResponse("Not found or no longer pending.", status_code=404)
        return RedirectResponse(f"/work/{work_id}", status_code=303)

    @router.get("/runs", response_class=HTMLResponse)
    async def run_list(
        request: Request,
        account_id: Optional[str] = None,
        outcome: Optional[str] = None,
        source: Optional[str] = None,
        since: Optional[date] = None,
        page_number: int = Query(1, alias="page", ge=1),
    ) -> Response:
        session = authenticated(request)
        if session is None:
            return login_redirect()
        try:
            selected = application.list_runs(
                account_id=account_id or None,
                outcome=RunOutcome(outcome) if outcome else None,
                source=RunSource(source) if source else None,
                since=since,
                limit=RUNS_PAGE_SIZE + 1,
                offset=(page_number - 1) * RUNS_PAGE_SIZE,
            )
        except ValueError:
            selected = []

        rows = selected[:RUNS_PAGE_SIZE]
        filters = {
            "account_id": account_id or "",
            "outcome": outcome or "",
            "source": source or "",
            "since": since.isoformat() if since else "",
        }

        def navigation_url(destination: int) -> str:
            query = {name: value for name, value in filters.items() if value}
            query["page"] = str(destination)
            return f"/runs?{urlencode(query)}"

        context = {
            "runs": rows,
            "filters": filters,
            "outcomes": tuple(value.value for value in RunOutcome),
            "sources": (
                (RunSource.SIGNAL.value, "Signal"),
                (RunSource.DASHBOARD.value, "Dashboard"),
                (RunSource.INITIAL.value, "Initial setup"),
                (RunSource.SCHEDULED.value, "Scheduled"),
                (RunSource.RETRY.value, "Next-open retry"),
            ),
            "page_number": page_number,
            "previous_url": navigation_url(page_number - 1) if page_number > 1 else None,
            "next_url": (
                navigation_url(page_number + 1)
                if len(selected) > RUNS_PAGE_SIZE
                else None
            ),
        }
        return page(request, "runs.html", context, session)

    @router.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: str) -> Response:
        session = authenticated(request)
        if session is None:
            return login_redirect()
        run = application.get_run(run_id)
        if run is None:
            return HTMLResponse("Not found.", status_code=404)
        return page(request, "run_detail.html", {"run": run}, session)

    return router
